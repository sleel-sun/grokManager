"""OpenAI-compatible API router (/v1/*)."""

import asyncio
import base64
import binascii
import mimetypes
from io import BytesIO
from typing import Annotated, AsyncGenerator, AsyncIterable, Literal

import orjson
from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse

from app.control.account.state_machine import is_manageable
from app.platform.auth.middleware import verify_api_key
from app.platform.errors import AppError, ValidationError
from app.platform.logging.logger import logger
from app.platform.storage import image_files_dir, video_files_dir
from app.control.model import registry as model_registry
from app.control.model.spec import ModelSpec
from app.products._upstream_headers import build_upstream_response_headers
from .schemas import (
    ChatCompletionRequest,
    ImageGenerationRequest,
    VideoConfig,
    ImageConfig,
    ResponsesCreateRequest,
)
from .chat import completions as chat_completions

router = APIRouter(prefix="/v1")
_POOL_ID_TO_NAME = {0: "basic", 1: "super", 2: "heavy"}
_TAG_MODELS = "OpenAI - Models"
_TAG_CHAT = "OpenAI - Chat"
_TAG_RESPONSES = "OpenAI - Responses"
_TAG_IMAGES = "OpenAI - Images"
_TAG_VIDEOS = "OpenAI - Videos"
_TAG_FILES = "OpenAI - Files"
_STANDALONE_IMAGE_RESPONSE_FORMAT_DEFAULT = "b64_json"


async def _available_pools(request: Request) -> frozenset[str]:
    repo = getattr(request.app.state, "repository", None)
    if repo is None:
        return frozenset()

    snapshot = await repo.runtime_snapshot()
    pools = {record.pool for record in snapshot.items if is_manageable(record)}
    return frozenset(pools)


def _model_available_for_pools(spec: ModelSpec, pools: frozenset[str]) -> bool:
    if not spec.enabled:
        return False
    candidates = {_POOL_ID_TO_NAME[pool_id] for pool_id in spec.pool_candidates()}
    return bool(candidates & pools)


# ---------------------------------------------------------------------------
# /v1/models
# ---------------------------------------------------------------------------


def _is_anthropic_client(anthropic_version: str | None) -> bool:
    """Anthropic SDKs always send ``anthropic-version`` on every request.

    OpenAI SDKs never do. Using header presence as a content-negotiation hint
    lets us serve Anthropic-format model listings on the shared ``/v1/models``
    path without breaking existing OpenAI clients.
    """
    return bool(anthropic_version and anthropic_version.strip())


def _is_anthropic_visible_model(spec: ModelSpec) -> bool:
    """Only expose text-capable models on Anthropic's Messages surface."""
    return spec.enabled and spec.is_chat()


def _model_capability_names(spec: ModelSpec) -> list[str]:
    capabilities: list[str] = []
    if spec.is_chat():
        capabilities.append("chat")
    if spec.is_image():
        capabilities.append("image")
    if spec.is_image_edit():
        capabilities.append("image_edit")
    if spec.is_video():
        capabilities.append("video")
    if spec.is_voice():
        capabilities.append("voice")
    return capabilities


def _model_primary_type(spec: ModelSpec) -> str:
    if spec.is_image():
        return "image"
    if spec.is_image_edit():
        return "image_edit"
    if spec.is_video():
        return "video"
    if spec.is_voice():
        return "voice"
    if spec.is_chat():
        return "chat"
    return "unknown"


def _model_generation_metadata(spec: ModelSpec) -> dict:
    if spec.is_image() and spec.is_image_edit():
        input_modalities = ["text", "image"]
        output_modalities = ["image"]
        methods = ["chat.completions", "images.generations", "images.edits"]
        endpoints = [
            "/v1/chat/completions",
            "/v1/images/generations",
            "/v1/images/edits",
        ]
        modality = "text(+image)->image"
    elif spec.is_image_edit():
        input_modalities = ["text", "image"]
        output_modalities = ["image"]
        methods = ["chat.completions", "images.edits"]
        endpoints = ["/v1/chat/completions", "/v1/images/edits"]
        modality = "text+image->image"
    elif spec.is_image():
        input_modalities = ["text"]
        output_modalities = ["image"]
        methods = ["chat.completions", "images.generations"]
        endpoints = ["/v1/chat/completions", "/v1/images/generations"]
        modality = "text->image"
    elif spec.is_video():
        input_modalities = ["text", "image"]
        output_modalities = ["video"]
        methods = ["chat.completions", "videos.create"]
        endpoints = ["/v1/chat/completions", "/v1/videos"]
        modality = "text+image->video"
    elif spec.is_voice():
        input_modalities = ["text", "audio"]
        output_modalities = ["audio"]
        methods = ["chat.completions"]
        endpoints = ["/v1/chat/completions"]
        modality = "text+audio->audio"
    else:
        input_modalities = ["text"]
        output_modalities = ["text"]
        methods = ["chat.completions", "responses"]
        endpoints = ["/v1/chat/completions", "/v1/responses"]
        modality = "text->text"

    modalities = list(dict.fromkeys([*input_modalities, *output_modalities]))
    return {
        "modalities": modalities,
        "input_modalities": input_modalities,
        "output_modalities": output_modalities,
        "supported_generation_methods": methods,
        "supportedGenerationMethods": methods,
        "endpoints": endpoints,
        "supported_endpoints": endpoints,
        "architecture": {
            "modality": modality,
            "input_modalities": input_modalities,
            "output_modalities": output_modalities,
        },
    }


def _model_pool_names(spec: ModelSpec) -> list[str]:
    return [_POOL_ID_TO_NAME[pool_id] for pool_id in spec.pool_candidates()]


def _model_availability_payload(
    spec: ModelSpec,
    available_pools: frozenset[str] | None,
) -> dict:
    required = _model_pool_names(spec)
    if available_pools is None:
        status = "unknown"
        reason = "Account pool availability was not evaluated for this request."
    else:
        status = "available" if bool(set(required) & available_pools) else "unavailable"
        reason = (
            "At least one required account pool is active."
            if status == "available"
            else "Requires an active/manageable account in one of the required pools."
        )
    return {
        "status": status,
        "reason": reason,
        "required_pools": required,
        "available_pools": sorted(available_pools) if available_pools is not None else [],
    }


def _model_routing_payload(spec: ModelSpec) -> dict:
    return {
        "upstream_profile": spec.upstream_profile,
        "upstream_model": spec.upstream_model_name(),
        "mode_id": int(spec.mode_id),
        "pool_candidates": _model_pool_names(spec),
    }


def _openai_model_payload(
    spec: ModelSpec,
    created: int,
    available_pools: frozenset[str] | None = None,
) -> dict:
    capabilities = _model_capability_names(spec)
    primary_type = _model_primary_type(spec)
    return {
        "id": spec.model_name,
        "object": "model",
        "created": created,
        "owned_by": "xai",
        "name": spec.public_name,
        "type": primary_type,
        "model_type": primary_type,
        "capability": capabilities[0] if capabilities else "unknown",
        "capabilities": capabilities,
        "availability": _model_availability_payload(spec, available_pools),
        "routing": _model_routing_payload(spec),
        **_model_generation_metadata(spec),
    }


def _anthropic_model_payload(spec: ModelSpec, created: int) -> dict:
    """Return Anthropic-format model entry (see Anthropic List Models API).

    Anthropic represents timestamps as ISO-8601 strings and uses
    ``display_name`` / ``type: "model"`` instead of OpenAI's ``name`` /
    ``object: "model"``.
    """
    from datetime import datetime, timezone

    return {
        "type": "model",
        "id": spec.model_name,
        "display_name": spec.public_name,
        "created_at": datetime.fromtimestamp(created, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _standalone_image_response_format(response_format: str | None) -> str:
    return response_format or _STANDALONE_IMAGE_RESPONSE_FORMAT_DEFAULT


@router.get("/models", tags=[_TAG_MODELS], dependencies=[Depends(verify_api_key)])
async def list_models(
    request: Request,
    anthropic_version: str | None = Header(default=None, alias="anthropic-version"),
):
    import time

    pools = await _available_pools(request)
    created = int(time.time())
    available = model_registry.list_enabled()

    if _is_anthropic_client(anthropic_version):
        data = [
            _anthropic_model_payload(m, created)
            for m in available
            if _is_anthropic_visible_model(m)
        ]
        return JSONResponse(
            {
                "data": data,
                "has_more": False,
                "first_id": data[0]["id"] if data else None,
                "last_id": data[-1]["id"] if data else None,
            }
        )

    return JSONResponse(
        {
            "object": "list",
            "data": [_openai_model_payload(m, created, pools) for m in available],
        }
    )


@router.get(
    "/models/{model_id}", tags=[_TAG_MODELS], dependencies=[Depends(verify_api_key)]
)
async def get_model_endpoint(
    model_id: str,
    request: Request,
    anthropic_version: str | None = Header(default=None, alias="anthropic-version"),
):
    import time

    spec = model_registry.get(model_id)
    pools = await _available_pools(request)
    is_anthropic = _is_anthropic_client(anthropic_version)
    if (
        spec is None
        or not spec.enabled
        or (is_anthropic and not _is_anthropic_visible_model(spec))
    ):
        if is_anthropic:
            return JSONResponse(
                {
                    "type": "error",
                    "error": {
                        "type": "not_found_error",
                        "message": f"Model {model_id!r} not found",
                    },
                },
                status_code=404,
            )
        return JSONResponse(
            {
                "error": {
                    "message": f"Model {model_id!r} not found",
                    "type": "invalid_request_error",
                }
            },
            status_code=404,
        )

    created = int(time.time())
    if is_anthropic:
        return JSONResponse(_anthropic_model_payload(spec, created))
    return JSONResponse(_openai_model_payload(spec, created, pools))


# ---------------------------------------------------------------------------
# SSE streaming helpers
# ---------------------------------------------------------------------------


async def _safe_sse(stream: AsyncIterable[str]) -> AsyncGenerator[str, None]:
    """Wrap an SSE stream, converting exceptions to in-band error events."""
    try:
        async for chunk in stream:
            yield chunk
    except AppError as exc:
        payload = orjson.dumps({"error": exc.to_dict()["error"]}).decode()
        yield f"event: error\ndata: {payload}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as exc:
        payload = orjson.dumps(
            {"error": {"message": str(exc), "type": "server_error"}}
        ).decode()
        yield f"event: error\ndata: {payload}\n\n"
        yield "data: [DONE]\n\n"


_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

_HEARTBEAT_INTERVAL_S = 30


async def _sse_with_heartbeat(
    stream: AsyncIterable[str], interval: int = _HEARTBEAT_INTERVAL_S
) -> AsyncGenerator[str, None]:
    """Keep SSE connections alive through reverse proxies / CDNs.

    - Initial 2KB padding forces intermediate buffers (nginx, Cloudflare) to flush.
    - `: ping` comments sent every `interval` seconds of silence.
    """
    yield ": heartbeat stream connected\n" + " " * 2048 + "\n\n"

    queue: asyncio.Queue[tuple[str, str | Exception | None]] = asyncio.Queue()

    async def _producer() -> None:
        try:
            async for chunk in stream:
                await queue.put(("chunk", chunk))
        except Exception as exc:
            await queue.put(("error", exc))
        else:
            await queue.put(("done", None))

    producer = asyncio.create_task(_producer(), name="sse-heartbeat-producer")
    try:
        while True:
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=interval)
            except asyncio.TimeoutError:
                if producer.done() and queue.empty():
                    try:
                        await producer
                    except asyncio.CancelledError:
                        pass
                    break
                yield ": ping\n\n"
                continue

            if kind == "chunk":
                yield str(payload)
                continue
            if kind == "error":
                raise payload
            break
    finally:
        if not producer.done():
            producer.cancel()
        try:
            await producer
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# /v1/chat/completions
# ---------------------------------------------------------------------------

_VALID_ROLES = {"developer", "system", "user", "assistant", "tool"}
_USER_BLOCK_TYPES = {"text", "image_url", "input_audio", "file"}
_ALLOWED_SIZES = {"1280x720", "720x1280", "1792x1024", "1024x1792", "1024x1024"}
_EFFORT_VALUES = {"none", "minimal", "low", "medium", "high", "xhigh"}
_LITE_IMAGE_MODELS = {"grok-imagine-image-lite"}
_GPT_IMAGE_MODELS = {"gpt-image-1", "gpt-image-2", "codex-gpt-image-2"}


def _validate_chat(req: ChatCompletionRequest) -> None:
    from app.platform.errors import ValidationError

    spec = model_registry.get(req.model)
    if spec is None or not spec.enabled:
        raise ValidationError(
            f"Model {req.model!r} does not exist or you do not have access to it.",
            param="model",
            code="model_not_found",
        )
    if not req.messages:
        raise ValidationError("messages cannot be empty", param="messages")
    for i, msg in enumerate(req.messages):
        if msg.role not in _VALID_ROLES:
            raise ValidationError(
                f"role must be one of {sorted(_VALID_ROLES)}",
                param=f"messages.{i}.role",
            )
    if req.temperature is not None and not (0 <= req.temperature <= 2):
        raise ValidationError(
            "temperature must be between 0 and 2", param="temperature"
        )
    if req.top_p is not None and not (0 <= req.top_p <= 1):
        raise ValidationError("top_p must be between 0 and 1", param="top_p")
    if req.reasoning_effort is not None and req.reasoning_effort not in _EFFORT_VALUES:
        raise ValidationError(
            f"reasoning_effort must be one of {sorted(_EFFORT_VALUES)}",
            param="reasoning_effort",
        )


def _validate_image_n(model_name: str, n: int, *, param: str) -> None:
    max_n = 4 if model_name in _LITE_IMAGE_MODELS or model_name in _GPT_IMAGE_MODELS else 10
    if not (1 <= n <= max_n):
        raise ValidationError(
            f"n must be between 1 and {max_n} for model {model_name!r}",
            param=param,
        )


def _validate_image_edit_n(n: int, *, param: str) -> None:
    if not (1 <= n <= 2):
        raise ValidationError("n must be between 1 and 2 for image edit", param=param)


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") not in {"text", "input_text"}:
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts).strip()


def _last_user_text_prompt(messages: list) -> str:
    for msg in reversed(messages):
        if getattr(msg, "role", None) != "user":
            continue
        prompt = _content_text(getattr(msg, "content", None))
        if prompt:
            return prompt
    return ""


def _coalesce_uploads(*groups: list[UploadFile] | None) -> list[UploadFile]:
    uploads: list[UploadFile] = []
    for group in groups:
        if group:
            uploads.extend(group)
    return uploads


async def _upload_to_data_uri(upload: UploadFile, *, param: str) -> str:
    raw, mime = await _read_upload_image(upload, param=param)
    return _image_bytes_to_data_uri(raw, mime, param=param)


async def _read_upload_image(upload: UploadFile, *, param: str) -> tuple[bytes, str]:
    raw = await upload.read()
    if not raw:
        raise ValidationError("Uploaded image cannot be empty", param=param)

    mime = (
        (upload.content_type or "").strip().lower()
        or mimetypes.guess_type(upload.filename or "")[0]
        or "application/octet-stream"
    )
    if not mime.startswith("image/"):
        raise ValidationError("Uploaded file must be an image", param=param)
    return raw, mime


def _image_bytes_to_data_uri(raw: bytes, mime: str, *, param: str) -> str:
    try:
        blob_b64 = base64.b64encode(raw).decode("ascii")
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValidationError("Failed to encode uploaded image", param=param) from exc
    return f"data:{mime};base64,{blob_b64}"


def _compose_mask_alpha(image_raw: bytes, mask_raw: bytes, *, param: str) -> bytes:
    try:
        from PIL import Image

        image = Image.open(BytesIO(image_raw)).convert("RGBA")
        mask = Image.open(BytesIO(mask_raw))
        if "A" in mask.getbands():
            alpha = mask.getchannel("A")
        elif mask.mode == "L":
            alpha = mask
        else:
            alpha = mask.convert("L")
        alpha = alpha.resize(image.size, Image.Resampling.LANCZOS)
        image.putalpha(alpha)
        buf = BytesIO()
        image.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:
        raise ValidationError("Failed to apply mask to uploaded image", param=param) from exc


async def _uploads_to_data_uris(
    uploads: list[UploadFile],
    *,
    mask: UploadFile | None = None,
) -> list[str]:
    mask_raw: bytes | None = None
    if mask is not None:
        mask_raw, _mask_mime = await _read_upload_image(mask, param="mask")

    image_inputs: list[str] = []
    for index, item in enumerate(uploads):
        param = f"image.{index}"
        raw, mime = await _read_upload_image(item, param=param)
        if mask_raw is not None:
            raw = _compose_mask_alpha(raw, mask_raw, param=param)
            mime = "image/png"
        image_inputs.append(_image_bytes_to_data_uri(raw, mime, param=param))
    return image_inputs


@router.post(
    "/chat/completions", tags=[_TAG_CHAT], dependencies=[Depends(verify_api_key)]
)
async def chat_completions_endpoint(req: ChatCompletionRequest):
    _validate_chat(req)
    from app.platform.config.snapshot import get_config

    cfg = get_config()
    is_stream = (
        req.stream if req.stream is not None else cfg.get_bool("features.stream", True)
    )

    spec = model_registry.get(req.model)
    if spec is None or not spec.enabled:
        raise ValidationError(
            f"Model {req.model!r} does not exist or you do not have access to it.",
            param="model",
            code="model_not_found",
        )
    messages = [m.model_dump(exclude_none=True) for m in req.messages]

    try:
        # Dispatch by model capability.
        if spec.is_image_edit():
            from .images import edit as img_edit

            cfg = req.image_config or ImageConfig()
            _validate_image_edit_n(cfg.n or 1, param="image_config.n")
            result = await img_edit(
                model=req.model,
                messages=messages,
                n=cfg.n or 1,
                size=cfg.size or "1024x1024",
                response_format=cfg.response_format or "url",
                stream=is_stream,
                chat_format=True,
            )

        elif spec.is_image():
            from .images import generate as img_gen

            cfg = req.image_config or ImageConfig()
            size = cfg.size or "1024x1024"
            fmt = cfg.response_format or "url"
            n = cfg.n or 1
            _validate_image_n(req.model, n, param="image_config.n")
            prompt = _last_user_text_prompt(req.messages)
            if not prompt:
                raise ValidationError(
                    "Image generation requires a non-empty text prompt",
                    param="messages",
                )
            result = await img_gen(
                model=req.model,
                prompt=prompt,
                n=n,
                size=size,
                response_format=fmt,
                stream=is_stream,
                chat_format=True,
            )

        elif spec.is_video():
            from .video import completions as vid_comp

            vcfg = req.video_config or VideoConfig()
            from .video import validate_video_length as _validate_video_length

            _validate_video_length(vcfg.seconds or 6)
            result = await vid_comp(
                model=req.model,
                messages=messages,
                stream=is_stream,
                seconds=vcfg.seconds or 6,
                size=vcfg.size or "720x1280",
                resolution_name=vcfg.resolution_name,
                preset=vcfg.preset,
            )

        else:
            request_overrides: dict | None = None
            if req.deepsearch:
                request_overrides = {"deepsearchPreset": req.deepsearch}
            if spec.uses_console_responses() and req.reasoning_effort is not None:
                request_overrides = request_overrides or {}
                request_overrides["_reasoning_effort"] = req.reasoning_effort
            # reasoning_effort=None → config default; "none" → off; otherwise → on.
            if req.reasoning_effort is None:
                emit_think: bool | None = None
            else:
                emit_think = req.reasoning_effort != "none"
            result = await chat_completions(
                model=req.model,
                messages=messages,
                stream=is_stream,
                emit_think=emit_think,
                tools=req.tools,
                tool_choice=req.tool_choice,
                temperature=req.temperature or 0.8,
                top_p=req.top_p or 0.95,
                request_overrides=request_overrides,
            )

    except AppError:
        raise
    except Exception as exc:
        logger.exception(
            "chat completions endpoint failed: model={} stream={} error={}",
            req.model,
            is_stream,
            exc,
        )
        # Video failures must surface their real HTTP status code so downstream
        # billing gateways (e.g. New API) don't misread an SSE-wrapped error as a
        # successful 200 response.
        if spec.is_video():
            raise
        if is_stream:
            _err_msg = str(
                exc
            )  # capture before Python clears the except-scope variable

            async def _err_stream():
                payload = orjson.dumps(
                    {"error": {"message": _err_msg, "type": "server_error"}}
                ).decode()
                yield f"event: error\ndata: {payload}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                _err_stream(),
                media_type="text/event-stream",
                headers={**_SSE_HEADERS, **build_upstream_response_headers(spec)},
            )
        raise

    upstream_headers = build_upstream_response_headers(spec)
    if isinstance(result, dict):
        return JSONResponse(result, headers=upstream_headers)
    return StreamingResponse(
        _sse_with_heartbeat(_safe_sse(result)),
        media_type="text/event-stream",
        headers={**_SSE_HEADERS, **upstream_headers},
    )


# ---------------------------------------------------------------------------
# /v1/responses  (OpenAI Responses API)
# ---------------------------------------------------------------------------


async def _safe_sse_responses(stream) -> AsyncGenerator[str, None]:
    """SSE wrapper that converts errors to Responses API error events."""
    try:
        async for chunk in stream:
            yield chunk
    except Exception as exc:
        from app.platform.errors import AppError

        if isinstance(exc, AppError):
            err = exc.to_dict()["error"]
        else:
            err = {
                "message": str(exc),
                "type": "server_error",
                "code": None,
                "param": None,
            }
        payload = orjson.dumps({"type": "error", **err}).decode()
        yield f"event: error\ndata: {payload}\n\n"
        yield "data: [DONE]\n\n"


@router.post(
    "/responses", tags=[_TAG_RESPONSES], dependencies=[Depends(verify_api_key)]
)
async def responses_endpoint(req: ResponsesCreateRequest):
    from app.platform.config.snapshot import get_config
    from app.platform.errors import ValidationError as _ValidationError

    spec = model_registry.get(req.model)
    if spec is None or not spec.enabled:
        raise _ValidationError(
            f"Model {req.model!r} does not exist or you do not have access to it.",
            param="model",
            code="model_not_found",
        )
    if not req.input:
        raise _ValidationError("input cannot be empty", param="input")

    cfg = get_config()
    is_stream = (
        req.stream if req.stream is not None else cfg.get_bool("features.stream", True)
    )

    # Map reasoning param → emit_think flag.
    # reasoning=None → use config; reasoning.effort="none" → off; otherwise on.
    if req.reasoning is None:
        emit_think = cfg.get_bool("features.thinking", True)
    elif isinstance(req.reasoning, dict) and req.reasoning.get("effort") == "none":
        emit_think = False
    else:
        emit_think = True

    request_overrides: dict | None = None
    if spec.uses_console_responses() and isinstance(req.reasoning, dict) and "effort" in req.reasoning:
        request_overrides = {"_reasoning_effort": req.reasoning.get("effort")}

    from .responses import create as responses_create

    result = await responses_create(
        model=req.model,
        input_val=req.input,
        instructions=req.instructions,
        stream=is_stream,
        emit_think=emit_think,
        temperature=req.temperature or 0.8,
        top_p=req.top_p or 0.95,
        tools=req.tools or None,
        tool_choice=req.tool_choice,
        request_overrides=request_overrides,
    )

    upstream_headers = build_upstream_response_headers(spec)
    if isinstance(result, dict):
        return JSONResponse(result, headers=upstream_headers)
    return StreamingResponse(
        _sse_with_heartbeat(_safe_sse_responses(result)),
        media_type="text/event-stream",
        headers={**_SSE_HEADERS, **upstream_headers},
    )


# ---------------------------------------------------------------------------
# /v1/images/generations (standalone image endpoint)
# ---------------------------------------------------------------------------


@router.post(
    "/images/generations", tags=[_TAG_IMAGES], dependencies=[Depends(verify_api_key)]
)
async def image_generations(req: ImageGenerationRequest):
    spec = model_registry.get(req.model)
    if spec is None or not spec.enabled or not spec.is_image():
        raise ValidationError(
            f"Model {req.model!r} is not an image model", param="model"
        )
    _validate_image_n(req.model, req.n or 1, param="n")

    from .images import generate as img_gen

    result = await img_gen(
        model=req.model,
        prompt=req.prompt,
        n=req.n or 1,
        size=req.size or "1024x1024",
        response_format=_standalone_image_response_format(req.response_format),
        stream=False,
        chat_format=False,
    )
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# /v1/videos (OpenAI videos.create surface)
# ---------------------------------------------------------------------------


@router.post("/videos", tags=[_TAG_VIDEOS], dependencies=[Depends(verify_api_key)])
async def videos_create(
    model: Annotated[str, Form(...)],
    prompt: Annotated[str, Form(...)],
    seconds: Annotated[int, Form()] = 6,
    size: Annotated[
        Literal["720x1280", "1280x720", "1024x1024", "1024x1792", "1792x1024"], Form()
    ] = "720x1280",
    resolution_name: Annotated[Literal["480p", "720p"] | None, Form()] = None,
    preset: Annotated[
        Literal["fun", "normal", "spicy", "custom"] | None, Form()
    ] = None,
    input_reference: Annotated[
        list[UploadFile] | None, File(alias="input_reference")
    ] = None,
    input_reference_array: Annotated[
        list[UploadFile] | None, File(alias="input_reference[]")
    ] = None,
    image: Annotated[
        list[UploadFile] | None, File(alias="image")
    ] = None,
    image_array: Annotated[
        list[UploadFile] | None, File(alias="image[]")
    ] = None,
):
    from .video import create_video

    reference_uploads = _coalesce_uploads(
        input_reference,
        input_reference_array,
        image,
        image_array,
    )
    references_payload = None
    if reference_uploads:
        references_payload = [
            {"image_url": await _upload_to_data_uri(f, param="input_reference")}
            for f in reference_uploads[:5]
        ]

    result = await create_video(
        model=model or "grok-video",
        prompt=prompt,
        seconds=seconds,
        size=size or "720x1280",
        resolution_name=resolution_name,
        preset=preset,
        input_references=references_payload,
    )
    return JSONResponse(result)


@router.get(
    "/videos/{video_id}", tags=[_TAG_VIDEOS], dependencies=[Depends(verify_api_key)]
)
async def videos_retrieve(video_id: str):
    from .video import retrieve

    return JSONResponse(await retrieve(video_id))


@router.get(
    "/videos/{video_id}/content",
    tags=[_TAG_VIDEOS],
    dependencies=[Depends(verify_api_key)],
)
async def videos_content(video_id: str):
    from .video import content_path

    path = await content_path(video_id)
    return FileResponse(path, media_type="video/mp4", filename=f"{video_id}.mp4")


# ---------------------------------------------------------------------------
# /v1/images/edits (standalone image-edit endpoint)
# ---------------------------------------------------------------------------


@router.post(
    "/images/edits", tags=[_TAG_IMAGES], dependencies=[Depends(verify_api_key)]
)
async def image_edits(
    model: Annotated[str, Form(...)],
    prompt: Annotated[str, Form(...)],
    image: Annotated[list[UploadFile] | None, File(alias="image")] = None,
    image_array: Annotated[list[UploadFile] | None, File(alias="image[]")] = None,
    mask: Annotated[UploadFile | None, File()] = None,
    n: Annotated[int, Form()] = 1,
    size: Annotated[str, Form()] = "1024x1024",
    response_format: Annotated[str, Form()] = _STANDALONE_IMAGE_RESPONSE_FORMAT_DEFAULT,
):
    spec = model_registry.get(model)
    if spec is None or not spec.enabled or not spec.is_image_edit():
        raise ValidationError(
            f"Model {model!r} is not an image-edit model", param="model"
        )
    _validate_image_edit_n(n, param="n")
    image_uploads = _coalesce_uploads(image, image_array)
    if not image_uploads:
        raise ValidationError("image is required", param="image")

    from .images import edit as img_edit

    image_inputs = await _uploads_to_data_uris(image_uploads, mask=mask)
    # Wrap input into a single-message conversation.
    content = [{"type": "text", "text": prompt}]
    content.extend(
        {"type": "image_url", "image_url": {"url": image_input}}
        for image_input in image_inputs
    )
    messages = [{"role": "user", "content": content}]
    result = await img_edit(
        model=model,
        messages=messages,
        n=n,
        size=size,
        response_format=response_format,
        stream=False,
        chat_format=False,
    )
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# /v1/files/image — serve locally saved images
# ---------------------------------------------------------------------------


@router.get("/files/video", tags=[_TAG_FILES])
async def serve_video(id: str = Query(..., description="Video file ID")):
    """Serve a locally cached video by file ID."""
    import re

    if not re.fullmatch(r"[0-9a-f\-]{16,36}", id):
        raise ValidationError("Invalid file ID", param="id")

    path = video_files_dir() / f"{id}.mp4"
    if path.exists():
        return FileResponse(path, media_type="video/mp4")

    raise ValidationError(f"Video {id!r} not found", param="id")


@router.get("/files/image", tags=[_TAG_FILES])
async def serve_image(id: str = Query(..., description="Image file ID")):
    """Serve a locally cached image by file ID."""
    import re

    if not re.fullmatch(r"[0-9a-f\-]{16,36}", id):
        raise ValidationError("Invalid file ID", param="id")

    img_dir = image_files_dir()
    mime_by_ext = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
    }
    matches = []
    for ext, mime in mime_by_ext.items():
        path = img_dir / f"{id}{ext}"
        if path.exists():
            matches.append((path, mime))
    if matches:
        path, mime = max(
            matches,
            key=lambda item: item[0].stat().st_mtime_ns,
        )
        return FileResponse(path, media_type=mime)

    raise ValidationError(f"Image {id!r} not found", param="id")


__all__ = ["router"]
