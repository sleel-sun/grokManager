"""ChatGPT web-backed GPT image generation."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import random
import re
import time
from dataclasses import dataclass
from io import BytesIO
from typing import Any, AsyncGenerator
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import aiohttp
import orjson
from PIL import Image

from app.control.account.commands import AccountPatch, ListAccountsQuery
from app.control.account.runtime import get_account_repository
from app.platform.config.snapshot import get_config
from app.platform.errors import AppError, RateLimitError, UpstreamError, ValidationError
from app.platform.logging.logger import logger
from app.platform.paths import data_path
from app.platform.storage import save_local_image

from ._format import make_chat_response, make_response_id, make_stream_chunk, make_thinking_chunk

BASE_URL = "https://chatgpt.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
CLIENT_BUILD_NUMBER = "5955942"
CLIENT_VERSION = "prod-be885abbfcfe7b1f511e88b3003d9ee44757fbad"
TIMEZONE = "America/Los_Angeles"
TIMEZONE_OFFSET_MIN = -480
MAX_POW_ATTEMPTS = 500000
_TRANSIENT_STATUSES = {429, 502, 503, 504, 524}
_FILE_ID_RE = re.compile(r"(file-service://|sediment://)([A-Za-z0-9_-]+)")
_DATA_URI_RE = re.compile(r"^data:([^;,]+)?(;base64)?,(.*)$", re.S)
_DATA_BUILD_RE = re.compile(r'data-build="([^"]*)"', re.I)
_SCRIPT_RE = re.compile(r'<script[^>]+src="([^"]+)"', re.I)
_AZURE_BLOB_HOST_SUFFIXES = (
    "blob.core.windows.net",
    "blob.core.chinacloudapi.cn",
    "blob.core.usgovcloudapi.net",
    "blob.core.cloudapi.de",
)
_DEFAULT_GENERATION_TIMEOUT_S = 360.0
_DEFAULT_ACCOUNT_ATTEMPT_TIMEOUT_S = 120.0
_INVALID_CREDENTIAL_MARKERS = (
    "invalidated auth token",
    "invalid token",
    "expired token",
    "token_revoked",
    "token revoked",
    "unauthorized",
    "authentication",
    "not authenticated",
    "login required",
    "access token",
    "invalid_api_key",
)
_GENERATION_FAILURE_COOLDOWN_S = 1800.0
_DEFAULT_MAX_ACCOUNT_ATTEMPTS_PER_IMAGE = 4
_QUOTA_LIMIT_MARKERS = (
    "free plan limit",
    "limit for image",
    "limit resets",
    "usage limit",
    "rate limit",
    "too many requests",
)
GPT_IMAGE_MODEL = "gpt-image-2"
CODEX_GPT_IMAGE_MODEL = "codex-gpt-image-2"


@dataclass(slots=True)
class GPTImageAccount:
    record_token: str
    access_token: str
    is_free: bool = False
    status_key: str = "gpt_image_status"
    error_key: str = "gpt_image_error"


@dataclass(slots=True)
class _ChatGPTContext:
    access_token: str
    device_id: str
    script: str
    dpl: str


@dataclass(slots=True)
class _GeneratedImage:
    b64_json: str
    mime_type: str = "image/png"


@dataclass(slots=True)
class _EditReference:
    file_id: str
    name: str
    mime_type: str
    size: int
    width: int
    height: int


def _app_url() -> str:
    return get_config().get_str("app.app_url", "").rstrip("/")


def _generation_timeout_s() -> float:
    try:
        value = get_config().get_float("gpt_image.timeout", _DEFAULT_GENERATION_TIMEOUT_S)
    except Exception:
        value = _DEFAULT_GENERATION_TIMEOUT_S
    return max(5.0, float(value or _DEFAULT_GENERATION_TIMEOUT_S))


def _account_attempt_timeout_s() -> float:
    try:
        value = get_config().get_float(
            "gpt_image.account_attempt_timeout_s",
            _DEFAULT_ACCOUNT_ATTEMPT_TIMEOUT_S,
        )
    except Exception:
        value = _DEFAULT_ACCOUNT_ATTEMPT_TIMEOUT_S
    return max(5.0, float(value or _DEFAULT_ACCOUNT_ATTEMPT_TIMEOUT_S))


def _max_account_attempts_per_image(account_count: int) -> int:
    try:
        value = get_config().get_int(
            "gpt_image.max_account_attempts_per_image",
            _DEFAULT_MAX_ACCOUNT_ATTEMPTS_PER_IMAGE,
        )
    except Exception:
        value = _DEFAULT_MAX_ACCOUNT_ATTEMPTS_PER_IMAGE
    return min(max(1, int(value or 1)), max(1, account_count))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _mask(value: str) -> str:
    value = str(value or "")
    return f"{value[:8]}...{value[-8:]}" if len(value) > 20 else value


def _local_image_url(file_id: str) -> str:
    path = f"/v1/files/image?id={file_id}"
    app_url = _app_url()
    return f"{app_url}{path}" if app_url else path


def _image_value(image: _GeneratedImage, response_format: str) -> dict[str, str]:
    fmt = (response_format or "url").strip().lower()
    if fmt == "b64_json":
        return {"b64_json": image.b64_json}
    if fmt != "url":
        raise ValidationError(
            "response_format must be one of ['url', 'b64_json']",
            param="response_format",
        )
    raw = base64.b64decode(image.b64_json)
    file_id = hashlib.sha1(raw).hexdigest()[:32]
    saved_id = save_local_image(raw, image.mime_type, file_id)
    return {"url": _local_image_url(saved_id)}


def _markdown_value(image: _GeneratedImage, response_format: str) -> str:
    value = _image_value(image, response_format)
    if "url" in value:
        return f"![image]({value['url']})"
    return f"![image](data:{image.mime_type};base64,{value['b64_json']})"


def _browser_headers(device_id: str) -> dict[str, str]:
    return {
        "user-agent": USER_AGENT,
        "accept-language": "en-US,en;q=0.9",
        "origin": BASE_URL,
        "referer": f"{BASE_URL}/",
        "accept": "*/*",
        "sec-ch-ua": '"Microsoft Edge";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "oai-device-id": device_id,
    }


def _conversation_headers(
    context: _ChatGPTContext,
    chat_token: str,
    proof_token: str | None,
    *,
    accept: str = "text/event-stream",
    conduit_token: str = "",
) -> dict[str, str]:
    headers = {
        **_browser_headers(context.device_id),
        "authorization": f"Bearer {context.access_token}",
        "accept": accept,
        "content-type": "application/json",
        "oai-language": "zh-CN",
        "oai-client-build-number": CLIENT_BUILD_NUMBER,
        "oai-client-version": CLIENT_VERSION,
        "openai-sentinel-chat-requirements-token": chat_token,
    }
    if proof_token:
        headers["openai-sentinel-proof-token"] = proof_token
    if conduit_token:
        headers["x-conduit-token"] = conduit_token
    if accept == "text/event-stream":
        headers["x-oai-turn-trace-id"] = str(uuid4())
    return headers


async def _response_error(response: aiohttp.ClientResponse, prefix: str) -> UpstreamError:
    try:
        body = await response.text()
    except Exception:
        body = ""
    detail = body[:500]
    status = 504 if response.status == 524 else response.status
    message = (
        f"{prefix}: upstream timed out (Cloudflare 524)"
        if response.status == 524
        else f"{prefix}: upstream returned {response.status}"
    )
    if detail:
        message = f"{message}: {detail}"
    return UpstreamError(message, status=status, body=detail)


async def _request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    data: bytes | None = None,
    timeout_s: float = 30.0,
    retries: int = 4,
    retry_statuses: set[int] | None = None,
) -> aiohttp.ClientResponse:
    retry_statuses = retry_statuses or set()
    last_exc: BaseException | None = None
    for attempt in range(max(1, retries)):
        try:
            response = await session.request(
                method,
                url,
                headers=headers,
                json=json_body,
                data=data,
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            )
            if response.status in retry_statuses and attempt < retries - 1:
                response.release()
                await asyncio.sleep(2 * (attempt + 1))
                continue
            return response
        except Exception as exc:
            last_exc = exc
            if attempt < retries - 1:
                await asyncio.sleep(2 * (attempt + 1))
                continue
    raise UpstreamError(f"ChatGPT image request failed: {last_exc}") from last_exc


async def _bootstrap(session: aiohttp.ClientSession, access_token: str) -> _ChatGPTContext:
    response = await _request(
        session,
        "GET",
        f"{BASE_URL}/",
        headers={
            "user-agent": USER_AGENT,
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
        },
        timeout_s=30.0,
    )
    if not response.ok:
        raise await _response_error(response, "ChatGPT bootstrap failed")
    html = await response.text()
    device_id = response.cookies.get("oai-did")
    script_match = _SCRIPT_RE.search(html)
    build_match = _DATA_BUILD_RE.search(html)
    return _ChatGPTContext(
        access_token=access_token,
        device_id=(device_id.value if device_id else str(uuid4())),
        script=script_match.group(1) if script_match else f"{BASE_URL}/backend-api/sentinel/sdk.js",
        dpl=build_match.group(1) if build_match else "",
    )


def _pow_config(context: _ChatGPTContext) -> list[Any]:
    return [
        random.choice([3000, 4000, 6000]),
        time.strftime("%a %b %d %Y %H:%M:%S GMT-0500 (Eastern Standard Time)"),
        4294705152,
        0,
        USER_AGENT,
        context.script,
        context.dpl,
        "en-US",
        "en-US,es-US,en,es",
        0,
        random.choice(
            [
                "webdriver-false",
                "vendor-Google Inc.",
                "cookieEnabled-true",
                "pdfViewerEnabled-true",
                "hardwareConcurrency-32",
            ]
        ),
        random.choice(["location", "_reactListeningo743lnnpvdg"]),
        random.choice(["innerWidth", "innerHeight", "devicePixelRatio", "screen", "chrome"]),
        int(time.time() * 1000) % 100000,
        str(uuid4()),
        "",
        random.choice([8, 16, 24, 32]),
        int(time.time() * 1000),
    ]


def _json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _generate_answer(seed: str, difficulty: str, config: list[Any]) -> str:
    target = bytes.fromhex(difficulty)
    seed_bytes = seed.encode("utf-8")
    part1 = f"{_json_compact(config[:3])[:-1]},".encode("utf-8")
    part2 = f",{_json_compact(config[4:9])[1:-1]},".encode("utf-8")
    part3 = f",{_json_compact(config[10:])[1:]}".encode("utf-8")
    for attempt in range(MAX_POW_ATTEMPTS):
        encoded = base64.b64encode(
            b"".join(
                [
                    part1,
                    str(attempt).encode(),
                    part2,
                    str(attempt >> 1).encode(),
                    part3,
                ]
            )
        ).decode("ascii")
        digest = hashlib.sha3_512(seed_bytes + encoded.encode("utf-8")).digest()
        if digest[: len(target)] <= target:
            return encoded
    return base64.b64encode(json.dumps(seed).encode("utf-8")).decode("ascii")


def _requirements_token(context: _ChatGPTContext) -> str:
    return f"gAAAAAC{_generate_answer(str(random.random()), '0fffff', _pow_config(context))}"


def _proof_token(context: _ChatGPTContext, proof_info: dict[str, Any] | None) -> str | None:
    if not proof_info or not proof_info.get("required"):
        return None
    seed = str(proof_info.get("seed") or "")
    difficulty = str(proof_info.get("difficulty") or "")
    if not seed or not difficulty:
        return None
    return f"gAAAAAB{_generate_answer(seed, difficulty, _pow_config(context))}"


async def _chat_requirements(
    session: aiohttp.ClientSession,
    context: _ChatGPTContext,
) -> tuple[str, dict[str, Any] | None]:
    response = await _request(
        session,
        "POST",
        f"{BASE_URL}/backend-api/sentinel/chat-requirements",
        headers={
            **_browser_headers(context.device_id),
            "authorization": f"Bearer {context.access_token}",
            "accept": "application/json",
            "content-type": "application/json",
        },
        json_body={"p": _requirements_token(context)},
        timeout_s=30.0,
        retry_statuses=_TRANSIENT_STATUSES,
    )
    if not response.ok:
        raise await _response_error(response, "ChatGPT chat-requirements failed")
    payload = await response.json(content_type=None)
    return str(payload.get("token") or ""), payload.get("proofofwork") or None


def _client_contextual_info() -> dict[str, Any]:
    return {
        "is_dark_mode": False,
        "time_since_loaded": random.randint(50, 500),
        "page_height": random.randint(500, 1000),
        "page_width": random.randint(1000, 2000),
        "pixel_ratio": 1.2,
        "screen_height": random.randint(800, 1200),
        "screen_width": random.randint(1200, 2200),
    }


def _normalize_image_model(requested_model: str) -> str:
    model = (requested_model or GPT_IMAGE_MODEL).strip()
    if model in {"gpt-image-1", GPT_IMAGE_MODEL}:
        return GPT_IMAGE_MODEL
    if model == CODEX_GPT_IMAGE_MODEL:
        return CODEX_GPT_IMAGE_MODEL
    return model or GPT_IMAGE_MODEL


def _upstream_model(requested_model: str, is_free: bool) -> str:
    model = _normalize_image_model(requested_model)
    if model == GPT_IMAGE_MODEL:
        return GPT_IMAGE_MODEL
    return model or "gpt-4o"


def _image_model_slug(requested_model: str) -> str:
    model = _normalize_image_model(requested_model)
    if model == GPT_IMAGE_MODEL:
        return "gpt-5-3"
    if model == CODEX_GPT_IMAGE_MODEL:
        return CODEX_GPT_IMAGE_MODEL
    return model or "auto"


def _image_generation_prompt(prompt: str) -> str:
    user_prompt = str(prompt or "").strip()
    return (
        "Create exactly one original image from the following user prompt. "
        "Use image generation only. Do not search the web, do not return "
        "existing image results, do not provide image_group/search results, "
        "and do not answer with explanatory text. Return the generated image "
        "file.\n\n"
        f"User prompt:\n{user_prompt}"
    )


def _image_edit_prompt(prompt: str, reference_count: int) -> str:
    user_prompt = str(prompt or "").strip()
    plural = "images" if reference_count != 1 else "image"
    return (
        f"Edit the provided reference {plural} according to the user prompt. "
        "Use image editing/generation only. Do not search the web, do not return "
        "existing image results, and do not answer with explanatory text. Return "
        "the edited image file.\n\n"
        f"User prompt:\n{user_prompt}"
    )


def _no_image_error(text: str) -> str:
    clean = str(text or "").strip()
    lower = clean.lower()
    if (
        "free plan limit" in lower
        or "limit for image" in lower
        or "limit resets" in lower
        or "usage limit" in lower
        or "rate limit" in lower
        or "too many requests" in lower
    ):
        return clean[:500] or "ChatGPT image generation quota exhausted"
    if (
        "processing image" in lower
        or "creating images" in lower
        or "image is still being generated" in lower
        or "正在处理图片" in clean
        or "很多人在创建图片" in clean
        or "图片准备好后" in clean
    ):
        return "ChatGPT image generation is still queued upstream; retry later"
    if "image_group" in clean or "image" in clean:
        return "ChatGPT returned image search results instead of a generated image"
    return clean[:500] or "ChatGPT image generation returned no images"


def _no_image_exception(text: str) -> UpstreamError:
    message = _no_image_error(text)
    lower = f"{text} {message}".lower()
    status = (
        429
        if (
            "free plan limit" in lower
            or "limit for image" in lower
            or "limit resets" in lower
            or "usage limit" in lower
            or "rate limit" in lower
            or "too many requests" in lower
        )
        else 502
    )
    return UpstreamError(message, status=status, body=str(text or "")[:500])


def _append_file_id(file_ids: list[str], prefix: str, file_id: str) -> None:
    value = f"sed:{file_id}" if prefix == "sediment://" else file_id
    if value not in file_ids:
        file_ids.append(value)


def _extract_file_ids_from_text(text: str, file_ids: list[str]) -> None:
    for match in _FILE_ID_RE.finditer(str(text or "")):
        prefix, file_id = match.groups()
        _append_file_id(file_ids, prefix, file_id)


def _extract_file_ids_recursive(value: Any, file_ids: list[str]) -> None:
    if isinstance(value, str):
        _extract_file_ids_from_text(value, file_ids)
        return
    if isinstance(value, list):
        for item in value:
            _extract_file_ids_recursive(item, file_ids)
        return
    if isinstance(value, dict):
        message = value.get("message") if isinstance(value.get("message"), dict) else value
        author = message.get("author") if isinstance(message.get("author"), dict) else {}
        if author.get("role") == "user":
            return
        for item in value.values():
            _extract_file_ids_recursive(item, file_ids)


def _image_tool_message(value: dict[str, Any]) -> dict[str, Any]:
    for candidate in (value, value.get("v")):
        if not isinstance(candidate, dict):
            continue
        message = candidate.get("message")
        if isinstance(message, dict):
            return message
    return {}


def _is_image_tool_event(value: dict[str, Any]) -> bool:
    message = _image_tool_message(value)
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    author = message.get("author") if isinstance(message.get("author"), dict) else {}
    return author.get("role") == "tool" and metadata.get("async_task_type") == "image_gen"


def _consume_sse_payload(
    payload: str,
    conversation_id: str,
    file_ids: list[str],
    text_parts: list[str],
) -> tuple[str, bool]:
    if not payload:
        return conversation_id, False
    if payload == "[DONE]":
        return conversation_id, True
    try:
        obj = orjson.loads(payload)
    except Exception:
        _extract_file_ids_from_text(payload, file_ids)
        return conversation_id, False
    if isinstance(obj, dict):
        conversation_id = str(obj.get("conversation_id") or conversation_id)
        nested = obj.get("v")
        if isinstance(nested, dict):
            conversation_id = str(nested.get("conversation_id") or conversation_id)
        if _is_image_tool_event(obj):
            _extract_file_ids_recursive(obj, file_ids)
        message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        content = message.get("content") if isinstance(message.get("content"), dict) else {}
        if content.get("content_type") == "text":
            parts = content.get("parts")
            if isinstance(parts, list) and parts:
                text_parts.append(str(parts[0]))
    return conversation_id, False


async def _send_conversation(
    session: aiohttp.ClientSession,
    context: _ChatGPTContext,
    *,
    prompt: str,
    requested_model: str,
    is_free: bool,
) -> tuple[str, list[str], str]:
    chat_token, proof_info = await _chat_requirements(session, context)
    proof_token = _proof_token(context, proof_info)
    path = "/backend-api/conversation"
    prompt_text = _image_generation_prompt(prompt)
    response = await _request(
        session,
        "POST",
        f"{BASE_URL}{path}",
        headers=_conversation_headers(
            context,
            chat_token,
            proof_token,
        ),
        json_body={
            "action": "next",
            "messages": [
                {
                    "id": str(uuid4()),
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": [prompt_text]},
                    "metadata": {"attachments": []},
                }
            ],
            "parent_message_id": str(uuid4()),
            "model": _upstream_model(requested_model, is_free),
            "history_and_training_disabled": False,
            "timezone_offset_min": TIMEZONE_OFFSET_MIN,
            "timezone": TIMEZONE,
            "conversation_mode": {"kind": "primary_assistant"},
            "conversation_origin": None,
            "force_paragen": False,
            "force_paragen_model_slug": "",
            "force_rate_limit": False,
            "force_use_sse": True,
            "paragen_cot_summary_display_override": "allow",
            "paragen_stream_type_override": None,
            "reset_rate_limits": False,
            "suggestions": [],
            "supported_encodings": [],
            "system_hints": ["picture_v2"],
            "variant_purpose": "comparison_implicit",
            "websocket_request_id": str(uuid4()),
            "client_contextual_info": _client_contextual_info(),
        },
        timeout_s=180.0,
        retry_statuses=_TRANSIENT_STATUSES,
    )
    if not response.ok:
        raise await _response_error(response, "ChatGPT image conversation failed")
    conversation_id = ""
    file_ids: list[str] = []
    text_parts: list[str] = []
    buffer = ""
    async for chunk in response.content.iter_chunked(8192):
        if not chunk:
            continue
        buffer += chunk.decode("utf-8", errors="ignore")
        while "\n" in buffer:
            raw_line, buffer = buffer.split("\n", 1)
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            conversation_id, done = _consume_sse_payload(
                payload,
                conversation_id,
                file_ids,
                text_parts,
            )
            if file_ids or done:
                response.release()
                return conversation_id, file_ids, "".join(text_parts)
    if buffer.strip().startswith("data:"):
        conversation_id, _done = _consume_sse_payload(
            buffer.strip()[5:].strip(),
            conversation_id,
            file_ids,
            text_parts,
        )
    return conversation_id, file_ids, "".join(text_parts)


def _image_ext(mime_type: str) -> str:
    return {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
    }.get(mime_type.lower(), "png")


def _decode_data_uri(image_input: str) -> tuple[bytes, str]:
    match = _DATA_URI_RE.match(str(image_input or "").strip())
    if not match:
        raise ValidationError(
            "GPT image edit currently requires uploaded images or data URI image inputs",
            param="image",
        )
    mime_type = (match.group(1) or "image/png").strip().lower()
    is_base64 = bool(match.group(2))
    data = match.group(3) or ""
    try:
        if is_base64:
            raw = base64.b64decode(data, validate=True)
        else:
            from urllib.parse import unquote_to_bytes

            raw = unquote_to_bytes(data)
    except (binascii.Error, ValueError) as exc:
        raise ValidationError("Invalid image data URI", param="image") from exc
    if not raw:
        raise ValidationError("image data is empty", param="image")
    if not mime_type.startswith("image/"):
        raise ValidationError("image data URI must use an image MIME type", param="image")
    return raw, mime_type


def _image_metadata(raw: bytes, fallback_mime_type: str) -> tuple[int, int, str]:
    try:
        with Image.open(BytesIO(raw)) as image:
            width, height = image.size
            mime_type = Image.MIME.get(image.format or "", fallback_mime_type)
    except Exception as exc:
        raise ValidationError("Invalid image data", param="image") from exc
    return int(width), int(height), (mime_type or fallback_mime_type)


def _is_azure_blob_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    hostname = (parsed.hostname or "").lower()
    if any(
        hostname == suffix or hostname.endswith(f".{suffix}")
        for suffix in _AZURE_BLOB_HOST_SUFFIXES
    ):
        return True
    query_keys = {key.lower() for key in parse_qs(parsed.query, keep_blank_values=True)}
    return any(
        key in query_keys for key in {"sv", "sr", "sp"}
    ) and "sig" in query_keys and not any(
        key.startswith("x-amz-") for key in query_keys
    )


def _set_header(headers: dict[str, str], name: str, value: str) -> None:
    existing = next((key for key in headers if key.lower() == name.lower()), None)
    if existing and existing != name:
        headers.pop(existing, None)
    headers[name] = value


def _merge_upload_headers(headers: dict[str, str], value: object) -> None:
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        name = str(key or "").strip()
        if not name or item is None:
            continue
        _set_header(headers, name, str(item))


def _has_nonempty_header(headers: dict[str, str], name: str) -> bool:
    return any(
        key.lower() == name.lower() and str(value or "").strip()
        for key, value in headers.items()
    )


def _edit_reference_upload_headers(
    payload: dict[str, Any],
    upload_url: str,
    mime_type: str,
) -> dict[str, str]:
    headers = {
        "content-type": mime_type,
        "user-agent": USER_AGENT,
    }
    for key in (
        "requiredHeaders",
        "required_headers",
        "uploadHeaders",
        "upload_headers",
    ):
        _merge_upload_headers(headers, payload.get(key))
    if _is_azure_blob_url(upload_url) and not _has_nonempty_header(headers, "x-ms-blob-type"):
        _set_header(headers, "x-ms-blob-type", "BlockBlob")
    return headers


async def _missing_blob_type_error(response: aiohttp.ClientResponse) -> bool:
    if response.status != 400:
        return False
    try:
        body = await response.text()
    except Exception:
        return False
    lowered = body.lower()
    return "missingrequiredheader" in lowered and "x-ms-blob-type" in lowered


async def _upload_edit_reference(
    session: aiohttp.ClientSession,
    context: _ChatGPTContext,
    image_input: str,
    index: int,
) -> _EditReference:
    raw, mime_type = _decode_data_uri(image_input)
    width, height, mime_type = _image_metadata(raw, mime_type)
    name = f"reference-{index + 1}.{_image_ext(mime_type)}"
    create_response = await _request(
        session,
        "POST",
        f"{BASE_URL}/backend-api/files",
        headers={
            **_browser_headers(context.device_id),
            "authorization": f"Bearer {context.access_token}",
            "accept": "application/json",
            "content-type": "application/json",
        },
        json_body={
            "file_name": name,
            "file_size": len(raw),
            "use_case": "multimodal",
            "width": width,
            "height": height,
            "timezone_offset_min": TIMEZONE_OFFSET_MIN,
        },
        timeout_s=30.0,
        retry_statuses=_TRANSIENT_STATUSES,
    )
    if not create_response.ok:
        raise await _response_error(create_response, "ChatGPT image-edit upload create failed")
    payload = await create_response.json(content_type=None)
    file_id = str(
        payload.get("file_id")
        or payload.get("fileId")
        or payload.get("id")
        or ""
    ).strip()
    upload_url = str(payload.get("upload_url") or payload.get("uploadUrl") or "").strip()
    if not file_id:
        raise UpstreamError("ChatGPT image-edit upload returned no file id", status=502)
    if upload_url:
        upload_headers = _edit_reference_upload_headers(payload, upload_url, mime_type)
        upload_response = await _request(
            session,
            "PUT",
            upload_url,
            headers=upload_headers,
            data=raw,
            timeout_s=60.0,
            retries=2,
            retry_statuses=_TRANSIENT_STATUSES,
        )
        if not upload_response.ok and await _missing_blob_type_error(upload_response):
            upload_response.release()
            _set_header(upload_headers, "x-ms-blob-type", "BlockBlob")
            upload_response = await _request(
                session,
                "PUT",
                upload_url,
                headers=upload_headers,
                data=raw,
                timeout_s=60.0,
                retries=1,
            )
        if not upload_response.ok:
            raise await _response_error(upload_response, "ChatGPT image-edit upload failed")
        complete_response = await _request(
            session,
            "POST",
            f"{BASE_URL}/backend-api/files/{file_id}/uploaded",
            headers={
                **_browser_headers(context.device_id),
                "authorization": f"Bearer {context.access_token}",
                "accept": "application/json",
                "content-type": "application/json",
            },
            json_body={},
            timeout_s=30.0,
            retries=1,
        )
        if not complete_response.ok:
            complete_response.release()
    return _EditReference(
        file_id=file_id,
        name=name,
        mime_type=mime_type,
        size=len(raw),
        width=width,
        height=height,
    )


def _edit_attachment_payload(reference: _EditReference) -> dict[str, Any]:
    return {
        "id": reference.file_id,
        "mimeType": reference.mime_type,
        "name": reference.name,
        "size": reference.size,
        "width": reference.width,
        "height": reference.height,
    }


def _edit_reference_part(reference: _EditReference) -> dict[str, Any]:
    return {
        "content_type": "image_asset_pointer",
        "asset_pointer": f"file-service://{reference.file_id}",
        "width": reference.width,
        "height": reference.height,
        "size_bytes": reference.size,
    }


async def _prepare_image_conversation(
    session: aiohttp.ClientSession,
    context: _ChatGPTContext,
    *,
    prompt_text: str,
    requested_model: str,
    chat_token: str,
    proof_token: str | None,
) -> str:
    response = await _request(
        session,
        "POST",
        f"{BASE_URL}/backend-api/f/conversation/prepare",
        headers=_conversation_headers(
            context,
            chat_token,
            proof_token,
            accept="*/*",
        ),
        json_body={
            "action": "next",
            "fork_from_shared_post": False,
            "parent_message_id": str(uuid4()),
            "model": _image_model_slug(requested_model),
            "client_prepare_state": "success",
            "timezone_offset_min": TIMEZONE_OFFSET_MIN,
            "timezone": TIMEZONE,
            "conversation_mode": {"kind": "primary_assistant"},
            "system_hints": ["picture_v2"],
            "partial_query": {
                "id": str(uuid4()),
                "author": {"role": "user"},
                "content": {"content_type": "text", "parts": [prompt_text]},
            },
            "supports_buffering": True,
            "supported_encodings": ["v1"],
            "client_contextual_info": {"app_name": "chatgpt.com"},
        },
        timeout_s=60.0,
        retry_statuses=_TRANSIENT_STATUSES,
    )
    if not response.ok:
        raise await _response_error(response, "ChatGPT image-edit prepare failed")
    payload = await response.json(content_type=None)
    conduit_token = str(payload.get("conduit_token") or "").strip()
    if not conduit_token:
        raise UpstreamError("ChatGPT image-edit prepare returned no conduit token", status=502)
    return conduit_token


async def _send_edit_conversation(
    session: aiohttp.ClientSession,
    context: _ChatGPTContext,
    *,
    prompt: str,
    image_inputs: list[str],
    requested_model: str,
    is_free: bool,
) -> tuple[str, list[str], str]:
    references = [
        await _upload_edit_reference(session, context, image_input, index)
        for index, image_input in enumerate(image_inputs)
    ]
    chat_token, proof_info = await _chat_requirements(session, context)
    proof_token = _proof_token(context, proof_info)
    prompt_text = _image_edit_prompt(prompt, len(references))
    conduit_token = await _prepare_image_conversation(
        session,
        context,
        prompt_text=prompt_text,
        requested_model=requested_model,
        chat_token=chat_token,
        proof_token=proof_token,
    )
    content_parts: list[Any] = [
        _edit_reference_part(reference)
        for reference in references
    ]
    content_parts.append(prompt_text)
    response = await _request(
        session,
        "POST",
        f"{BASE_URL}/backend-api/f/conversation",
        headers=_conversation_headers(
            context,
            chat_token,
            proof_token,
            conduit_token=conduit_token,
        ),
        json_body={
            "action": "next",
            "messages": [
                {
                    "id": str(uuid4()),
                    "author": {"role": "user"},
                    "create_time": time.time(),
                    "content": {
                        "content_type": "multimodal_text",
                        "parts": content_parts,
                    },
                    "metadata": {
                        "developer_mode_connector_ids": [],
                        "selected_github_repos": [],
                        "selected_all_github_repos": False,
                        "attachments": [
                            _edit_attachment_payload(reference)
                            for reference in references
                        ],
                        "system_hints": ["picture_v2"],
                        "serialization_metadata": {"custom_symbol_offsets": []},
                    },
                }
            ],
            "parent_message_id": str(uuid4()),
            "model": _image_model_slug(requested_model),
            "client_prepare_state": "sent",
            "timezone_offset_min": TIMEZONE_OFFSET_MIN,
            "timezone": TIMEZONE,
            "conversation_mode": {"kind": "primary_assistant"},
            "enable_message_followups": True,
            "paragen_cot_summary_display_override": "allow",
            "force_parallel_switch": "auto",
            "supports_buffering": True,
            "supported_encodings": ["v1"],
            "system_hints": ["picture_v2"],
            "client_contextual_info": {
                **_client_contextual_info(),
                "app_name": "chatgpt.com",
            },
        },
        timeout_s=180.0,
        retry_statuses=_TRANSIENT_STATUSES,
    )
    if not response.ok:
        raise await _response_error(response, "ChatGPT image-edit conversation failed")
    conversation_id = ""
    file_ids: list[str] = []
    text_parts: list[str] = []
    buffer = ""
    async for chunk in response.content.iter_chunked(8192):
        if not chunk:
            continue
        buffer += chunk.decode("utf-8", errors="ignore")
        while "\n" in buffer:
            raw_line, buffer = buffer.split("\n", 1)
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            conversation_id, done = _consume_sse_payload(
                payload,
                conversation_id,
                file_ids,
                text_parts,
            )
            if file_ids or done:
                response.release()
                return conversation_id, file_ids, "".join(text_parts)
    if buffer.strip().startswith("data:"):
        conversation_id, _done = _consume_sse_payload(
            buffer.strip()[5:].strip(),
            conversation_id,
            file_ids,
            text_parts,
        )
    return conversation_id, file_ids, "".join(text_parts)


def _parse_sse(raw_text: str) -> tuple[str, list[str], str]:
    conversation_id = ""
    file_ids: list[str] = []
    text_parts: list[str] = []
    for raw_line in str(raw_text or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        conversation_id, done = _consume_sse_payload(
            payload,
            conversation_id,
            file_ids,
            text_parts,
        )
        if done:
            break
    return conversation_id, file_ids, "".join(text_parts)


def _extract_image_ids(mapping: dict[str, Any]) -> list[str]:
    file_ids: list[str] = []
    for node in (mapping or {}).values():
        if not isinstance(node, dict):
            continue
        message = node.get("message") if isinstance(node.get("message"), dict) else {}
        author = message.get("author") if isinstance(message.get("author"), dict) else {}
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        content = message.get("content") if isinstance(message.get("content"), dict) else {}
        if author.get("role") != "tool" or metadata.get("async_task_type") != "image_gen":
            continue
        if content.get("content_type") != "multimodal_text":
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            pointer = str(part.get("asset_pointer") or "")
            if pointer.startswith("file-service://"):
                value = pointer.replace("file-service://", "")
            elif pointer.startswith("sediment://"):
                value = f"sed:{pointer.replace('sediment://', '')}"
            else:
                continue
            if value not in file_ids:
                file_ids.append(value)
    if file_ids:
        return file_ids

    # ChatGPT's web conversation schema changes frequently. When the strict
    # image_gen tool shape is absent, fall back to any generated asset pointer
    # embedded in the mapping.
    _extract_file_ids_recursive(mapping, file_ids)
    return file_ids


def _extract_assistant_text(mapping: dict[str, Any]) -> str:
    latest_time = -1.0
    latest_text = ""
    for node in (mapping or {}).values():
        if not isinstance(node, dict):
            continue
        message = node.get("message") if isinstance(node.get("message"), dict) else {}
        author = message.get("author") if isinstance(message.get("author"), dict) else {}
        content = message.get("content") if isinstance(message.get("content"), dict) else {}
        if author.get("role") != "assistant" or content.get("content_type") != "text":
            continue
        parts = content.get("parts")
        if not isinstance(parts, list) or not parts:
            continue
        text = str(parts[0] or "").strip()
        if not text:
            continue
        try:
            create_time = float(message.get("create_time") or 0.0)
        except (TypeError, ValueError):
            create_time = 0.0
        if create_time >= latest_time:
            latest_time = create_time
            latest_text = text
    return latest_text


async def _poll_image_result(
    session: aiohttp.ClientSession,
    context: _ChatGPTContext,
    conversation_id: str,
    *,
    timeout_s: float = 180.0,
) -> tuple[list[str], str]:
    last_text = ""
    deadline = time.monotonic() + max(1.0, float(timeout_s or 180.0))
    while time.monotonic() < deadline:
        response = await _request(
            session,
            "GET",
            f"{BASE_URL}/backend-api/conversation/{conversation_id}",
            headers={
                **_browser_headers(context.device_id),
                "authorization": f"Bearer {context.access_token}",
                "accept": "*/*",
            },
            timeout_s=30.0,
            retries=2,
            retry_statuses=_TRANSIENT_STATUSES,
        )
        if response.ok:
            try:
                payload = await response.json(content_type=None)
                file_ids = _extract_image_ids(payload.get("mapping") or {})
                if file_ids:
                    return file_ids, last_text
                text = _extract_assistant_text(payload.get("mapping") or {})
                if text:
                    last_text = text
                    if any(
                        marker in text.lower()
                        for marker in (
                            "free plan limit",
                            "limit for image",
                            "limit resets",
                            "usage limit",
                            "rate limit",
                            "too many requests",
                        )
                    ):
                        return [], last_text
            except Exception:
                pass
        await asyncio.sleep(3)
    return [], last_text


async def _poll_image_ids(
    session: aiohttp.ClientSession,
    context: _ChatGPTContext,
    conversation_id: str,
) -> list[str]:
    file_ids, _text = await _poll_image_result(session, context, conversation_id)
    return file_ids


async def _fetch_download_url(
    session: aiohttp.ClientSession,
    context: _ChatGPTContext,
    conversation_id: str,
    file_id: str,
) -> str:
    is_sediment = file_id.startswith("sed:")
    raw_id = file_id[4:] if is_sediment else file_id
    endpoint = (
        f"{BASE_URL}/backend-api/conversation/{conversation_id}/attachment/{raw_id}/download"
        if is_sediment
        else f"{BASE_URL}/backend-api/files/{raw_id}/download"
    )
    response = await _request(
        session,
        "GET",
        endpoint,
        headers={
            **_browser_headers(context.device_id),
            "authorization": f"Bearer {context.access_token}",
        },
        timeout_s=30.0,
        retries=2,
    )
    if not response.ok:
        return ""
    try:
        payload = await response.json(content_type=None)
    except Exception:
        return ""
    return str(payload.get("download_url") or "")


async def _download_base64(
    session: aiohttp.ClientSession,
    context: _ChatGPTContext,
    download_url: str,
) -> _GeneratedImage:
    same_origin = download_url.startswith(BASE_URL)
    headers = (
        {
            **_browser_headers(context.device_id),
            "authorization": f"Bearer {context.access_token}",
            "accept": "*/*",
        }
        if same_origin
        else {
            "user-agent": USER_AGENT,
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
        }
    )
    response = await _request(
        session,
        "GET",
        download_url,
        headers=headers,
        timeout_s=60.0,
        retries=2,
    )
    if not response.ok:
        raise await _response_error(response, "ChatGPT image download failed")
    raw = await response.read()
    if not raw:
        raise UpstreamError("ChatGPT image download returned empty body", status=502)
    content_type = (response.headers.get("content-type") or "image/png").split(";", 1)[0]
    return _GeneratedImage(
        b64_json=base64.b64encode(raw).decode("ascii"),
        mime_type=content_type or "image/png",
    )


async def _generate_one_inner(
    account: GPTImageAccount,
    prompt: str,
    model: str,
    *,
    timeout_s: float | None = None,
) -> _GeneratedImage:
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
        context = await _bootstrap(session, account.access_token)
        conversation_id, file_ids, text = await _send_conversation(
            session,
            context,
            prompt=prompt,
            requested_model=model,
            is_free=account.is_free,
        )
        if conversation_id and not file_ids:
            file_ids, polled_text = await _poll_image_result(
                session,
                context,
                conversation_id,
                timeout_s=max(1.0, float(timeout_s or _generation_timeout_s())),
            )
            text = polled_text or text
        if not file_ids:
            raise _no_image_exception(text)
        download_url = await _fetch_download_url(session, context, conversation_id, file_ids[0])
        if not download_url:
            raise UpstreamError("ChatGPT image generation returned no download URL", status=502)
        return await _download_base64(session, context, download_url)


async def _generate_one(
    account: GPTImageAccount,
    prompt: str,
    model: str,
    *,
    timeout_s: float | None = None,
) -> _GeneratedImage:
    timeout_s = max(1.0, float(timeout_s or _generation_timeout_s()))
    try:
        return await asyncio.wait_for(
            _generate_one_inner(account, prompt, model, timeout_s=timeout_s),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError as exc:
        raise UpstreamError(
            f"ChatGPT image generation timed out after {timeout_s:g}s",
            status=504,
            body="timeout",
        ) from exc


async def _edit_one_inner(
    account: GPTImageAccount,
    prompt: str,
    image_inputs: list[str],
    model: str,
    *,
    timeout_s: float | None = None,
) -> _GeneratedImage:
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
        context = await _bootstrap(session, account.access_token)
        conversation_id, file_ids, text = await _send_edit_conversation(
            session,
            context,
            prompt=prompt,
            image_inputs=image_inputs,
            requested_model=model,
            is_free=account.is_free,
        )
        if conversation_id and not file_ids:
            file_ids, polled_text = await _poll_image_result(
                session,
                context,
                conversation_id,
                timeout_s=max(1.0, float(timeout_s or _generation_timeout_s())),
            )
            text = polled_text or text
        if not file_ids:
            raise _no_image_exception(text)
        download_url = await _fetch_download_url(session, context, conversation_id, file_ids[0])
        if not download_url:
            raise UpstreamError("ChatGPT image edit returned no download URL", status=502)
        return await _download_base64(session, context, download_url)


async def _edit_one(
    account: GPTImageAccount,
    prompt: str,
    image_inputs: list[str],
    model: str,
    *,
    timeout_s: float | None = None,
) -> _GeneratedImage:
    timeout_s = max(1.0, float(timeout_s or _generation_timeout_s()))
    try:
        return await asyncio.wait_for(
            _edit_one_inner(account, prompt, image_inputs, model, timeout_s=timeout_s),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError as exc:
        raise UpstreamError(
            f"ChatGPT image edit timed out after {timeout_s:g}s",
            status=504,
            body="timeout",
        ) from exc


async def _validate_access_token(access_token: str) -> None:
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
        context = await _bootstrap(session, access_token)
        chat_token, _proof_info = await _chat_requirements(session, context)
        if not chat_token:
            raise UpstreamError("ChatGPT validation returned no requirements token", status=502)


async def _gpt_image_accounts() -> list[GPTImageAccount]:
    repo = get_account_repository()
    if repo is None:
        raise RateLimitError("Account repository not initialised")

    candidates: list[GPTImageAccount] = []
    blocked_tokens: set[str] = set()
    page_num = 1
    while True:
        page = await repo.list_accounts(
            ListAccountsQuery(
                page=page_num,
                page_size=2000,
                include_deleted=False,
                sort_by="updated_at",
                sort_desc=True,
            )
        )
        for record in page.items:
            blocked_access_token = _record_blocked_access_token(record)
            if blocked_access_token:
                blocked_tokens.add(_token_key(blocked_access_token))
            account = await _account_from_record(record)
            if account is not None:
                candidates.append(account)
        if page_num * 2000 >= page.total:
            break
        page_num += 1

    accounts: list[GPTImageAccount] = []
    seen_tokens: set[str] = set()
    for account in sorted(
        candidates,
        key=lambda item: 1 if item.status_key.startswith("gpt_image_") else 0,
    ):
        token_key = _token_key(account.access_token)
        if token_key in blocked_tokens or token_key in seen_tokens:
            continue
        seen_tokens.add(token_key)
        accounts.append(account)
    return accounts


def _maintainer_web_config_path() -> str:
    return str(data_path("maintainer", "web", "maintainer.config.json"))


def _is_gpt_credential_record(record: Any) -> bool:
    ext = record.ext or {}
    tags = set(record.tags or [])
    return (
        bool(ext.get("gpt_image"))
        or bool(ext.get("gpt"))
        or "gpt-image" in tags
        or "gpt" in tags
    )


def _record_access_token(ext: dict[str, Any]) -> str:
    return str(
        ext.get("gpt_access_token")
        or ext.get("gpt_image_access_token")
        or ""
    ).strip()


def _record_credentials(ext: dict[str, Any]) -> tuple[str, str, str]:
    email = str(ext.get("gpt_email") or ext.get("gpt_image_email") or "").strip()
    password = str(ext.get("gpt_password") or ext.get("gpt_image_password") or "").strip()
    mail_token = str(ext.get("gpt_mail_token") or ext.get("gpt_image_mail_token") or "").strip()
    return email, password, mail_token


def _record_is_free(ext: dict[str, Any]) -> bool:
    if "gpt_image_is_free" in ext:
        return bool(ext.get("gpt_image_is_free"))
    if ext.get("gpt_image"):
        return bool(ext.get("gpt_image_is_free"))
    plan = str(ext.get("gpt_plan_type") or "").strip().lower()
    return plan in {"", "free", "basic"}


def _record_patch_keys(ext: dict[str, Any]) -> tuple[str, str, str, str]:
    if ext.get("gpt") or any(key in ext for key in ("gpt_access_token", "gpt_status")):
        return (
            "gpt_access_token",
            "gpt_status",
            "gpt_registration_error",
            "gpt_login_attempt_at",
        )
    return (
        "gpt_image_access_token",
        "gpt_image_status",
        "gpt_image_error",
        "gpt_image_login_attempt_at",
    )


def _login_cooldown_active(ext: dict[str, Any]) -> bool:
    *_unused, attempt_key = _record_patch_keys(ext)
    last_attempt = int(ext.get(attempt_key) or 0)
    cooldown_s = get_config().get_float("gpt_image.login_retry_cooldown_s", 300.0)
    return bool(last_attempt and time.time() - (last_attempt / 1000) < cooldown_s)


def _usable_access_token_status(status: str) -> bool:
    # timeout/rate_limited are retryable once their cooldown/recent-failure
    # window expires. invalid/login_failed require a fresh login/token.
    return status not in {"invalid", "login_failed"}


def _token_key(access_token: str) -> str:
    return hashlib.sha256(access_token.encode("utf-8")).hexdigest()


def _recent_generation_failure(record: Any) -> bool:
    reason = str(getattr(record, "last_fail_reason", "") or "").lower()
    if not reason:
        return False
    generation_markers = (
        "gpt image",
        "image generation",
        "image generations",
        "image search results",
        "generated image",
        "queued upstream",
        "returned no images",
        *_QUOTA_LIMIT_MARKERS,
    )
    if not any(marker in reason for marker in generation_markers):
        return False
    transient_markers = (
        "timed out",
        "timeout",
        "rate",
        "image search results",
        "queued upstream",
        "returned no images",
        *_QUOTA_LIMIT_MARKERS,
    )
    if not any(marker in reason for marker in transient_markers):
        return False
    try:
        last_fail_at = int(getattr(record, "last_fail_at", None) or 0)
    except (TypeError, ValueError):
        last_fail_at = 0
    if not last_fail_at:
        return True
    age_s = (_now_ms() - last_fail_at) / 1000
    return age_s < _GENERATION_FAILURE_COOLDOWN_S


def _record_blocked_access_token(record: Any) -> str:
    if not _is_gpt_credential_record(record):
        return ""
    ext = record.ext or {}
    access_token = _record_access_token(ext)
    if not access_token:
        return ""
    _access_key, status_key, _error_key, _attempt_key = _record_patch_keys(ext)
    status = str(ext.get(status_key) or "unchecked")
    if (
        _usable_access_token_status(status)
        and not _generation_cooldown_active(ext, status_key)
        and not _recent_generation_failure(record)
    ):
        return ""
    return access_token


def _timeout_repair_after_s() -> float:
    try:
        value = get_config().get_float(
            "gpt_image.timeout_repair_after_s",
            _GENERATION_FAILURE_COOLDOWN_S,
        )
    except Exception:
        value = _GENERATION_FAILURE_COOLDOWN_S
    return max(0.0, float(value or 0.0))


def _timeout_repair_enabled() -> bool:
    try:
        return get_config().get_bool("gpt_image.auto_repair_timeout_accounts", True)
    except Exception:
        return True


def _has_timeout_marker(*values: Any) -> bool:
    text = " ".join(str(value or "") for value in values).lower()
    return "timeout" in text or "timed out" in text or "超时" in text


def _timeout_repair_due(record: Any, ext: dict[str, Any], status_key: str, error_key: str, now: int) -> bool:
    if _generation_cooldown_active(ext, status_key):
        return False
    status = str(ext.get(status_key) or "").strip().lower()
    error = str(ext.get(error_key) or "")
    reason = str(getattr(record, "last_fail_reason", "") or "")
    if status != "timeout" and not _has_timeout_marker(error, reason):
        return False
    try:
        last_fail_at = int(getattr(record, "last_fail_at", None) or 0)
    except (TypeError, ValueError):
        last_fail_at = 0
    return not last_fail_at or now - last_fail_at >= int(_timeout_repair_after_s() * 1000)


def _timeout_repair_status(ext: dict[str, Any]) -> str:
    if _record_access_token(ext):
        return "available"
    email, password, mail_token = _record_credentials(ext)
    return "login_required" if email and password and mail_token else "login_required"


async def repair_timed_out_gpt_image_accounts(repo: Any | None = None) -> int:
    """Clear stale transient timeout failures for GPT/GPT-image records.

    GPT records intentionally stay persistently DISABLED so they do not enter
    the Grok SSO pool; this repair only clears GPT capability failure fields.
    """
    if not _timeout_repair_enabled():
        return 0
    repo = repo or get_account_repository()
    if repo is None:
        return 0

    now = _now_ms()
    page_num = 1
    page_size = 2000
    patches: list[AccountPatch] = []
    while True:
        page = await repo.list_accounts(
            ListAccountsQuery(
                page=page_num,
                page_size=page_size,
                include_deleted=False,
                sort_by="updated_at",
                sort_desc=False,
            )
        )
        for record in page.items:
            if not _is_gpt_credential_record(record):
                continue
            ext = record.ext or {}
            _access_key, status_key, error_key, _attempt_key = _record_patch_keys(ext)
            if not _timeout_repair_due(record, ext, status_key, error_key, now):
                continue
            patches.append(
                AccountPatch(
                    token=record.token,
                    clear_last_failure=True,
                    ext_merge={
                        status_key: _timeout_repair_status(ext),
                        error_key: None,
                        _last_checked_key(status_key): now,
                        _cooldown_until_key(status_key): 0,
                    },
                )
            )

        if page_num * page_size >= page.total:
            break
        page_num += 1

    if not patches:
        return 0
    result = await repo.patch_accounts(patches)
    count = int(getattr(result, "patched", 0) or len(patches))
    logger.info("gpt image timeout accounts auto-repaired: count={}", count)
    return count


async def _login_gpt_credentials_async(
    *,
    email: str,
    password: str,
    mail_token: str,
) -> str:
    from app.maintainer.gpt import login_gpt_credentials

    return await asyncio.to_thread(
        login_gpt_credentials,
        email=email,
        password=password,
        mail_token=mail_token,
        config_path=_maintainer_web_config_path(),
    )


async def _account_from_record(record: Any) -> GPTImageAccount | None:
    if not _is_gpt_credential_record(record):
        return None
    ext = record.ext or {}
    access_key, status_key, error_key, attempt_key = _record_patch_keys(ext)
    access_token = _record_access_token(ext)
    if access_token:
        status = str(ext.get(status_key) or "unchecked")
        if (
            not _usable_access_token_status(status)
            or _generation_cooldown_active(ext, status_key)
            or _recent_generation_failure(record)
        ):
            return None
        return GPTImageAccount(
            record_token=record.token,
            access_token=access_token,
            is_free=_record_is_free(ext),
            status_key=status_key,
            error_key=error_key,
        )

    email, password, mail_token = _record_credentials(ext)
    if not (email and password and mail_token):
        return None
    if not get_config().get_bool("gpt_image.auto_login_credentials", True):
        return None
    if _login_cooldown_active(ext):
        return None

    repo = get_account_repository()
    if repo is None:
        return None
    now = _now_ms()
    try:
        access_token = await _login_gpt_credentials_async(
            email=email,
            password=password,
            mail_token=mail_token,
        )
        await repo.patch_accounts(
            [
                AccountPatch(
                    token=record.token,
                    ext_merge={
                        access_key: access_token,
                        status_key: "available",
                        error_key: None,
                        attempt_key: now,
                    },
                )
            ]
        )
        return GPTImageAccount(
            record_token=record.token,
            access_token=access_token,
            is_free=_record_is_free(ext),
            status_key=status_key,
            error_key=error_key,
        )
    except Exception as exc:
        await repo.patch_accounts(
            [
                AccountPatch(
                    token=record.token,
                    last_fail_at=now,
                    last_fail_reason=str(exc)[:500],
                    ext_merge={
                        status_key: "login_failed",
                        error_key: str(exc)[:500],
                        attempt_key: now,
                    },
                )
            ]
        )
        logger.warning(
            "gpt image credential login failed: record={} error={}",
            record.token,
            exc,
        )
        return None


async def _patch_account_failure(
    repo: Any,
    account: GPTImageAccount,
    exc: BaseException,
    *,
    status: str | None = None,
    ext_merge_extra: dict[str, Any] | None = None,
) -> tuple[str, str]:
    now = _now_ms()
    message = _failure_message(exc)
    resolved_status = status or _capability_failure_status(exc)
    ext_merge = {
        account.status_key: resolved_status,
        account.error_key: message,
        _last_checked_key(account.status_key): now,
    }
    cooldown_until = _cooldown_until_from_failure(message, resolved_status)
    if cooldown_until:
        ext_merge[_cooldown_until_key(account.status_key)] = cooldown_until
    if ext_merge_extra:
        ext_merge.update(ext_merge_extra)
    await repo.patch_accounts(
        [
            AccountPatch(
                token=account.record_token,
                last_fail_at=now,
                last_fail_reason=message,
                ext_merge=ext_merge,
            )
        ]
    )
    return resolved_status, message


async def _mark_account_failure(account: GPTImageAccount, exc: BaseException) -> None:
    if _is_cloudflare_524_timeout(exc):
        logger.info(
            "gpt image cloudflare timeout skipped account cooldown: account={}",
            account.record_token,
        )
        return
    repo = get_account_repository()
    if repo is None:
        return
    try:
        await _patch_account_failure(repo, account, exc)
    except Exception as patch_exc:
        logger.debug("gpt image account failure patch failed: error={}", patch_exc)


async def _patch_account_success(
    repo: Any,
    account: GPTImageAccount,
    *,
    ext_merge_extra: dict[str, Any] | None = None,
) -> None:
    now = _now_ms()
    ext_merge = {
        account.status_key: "available",
        account.error_key: None,
        _last_checked_key(account.status_key): now,
        _cooldown_until_key(account.status_key): 0,
    }
    if ext_merge_extra:
        ext_merge.update(ext_merge_extra)
    await repo.patch_accounts(
        [
            AccountPatch(
                token=account.record_token,
                last_use_at=now,
                ext_merge=ext_merge,
            )
        ]
    )


async def _mark_account_success(account: GPTImageAccount) -> None:
    repo = get_account_repository()
    if repo is None:
        return
    try:
        await _patch_account_success(repo, account)
    except Exception as patch_exc:
        logger.debug("gpt image account success patch failed: error={}", patch_exc)


async def test_gpt_account_record(record: Any, *, repo: Any | None = None) -> dict[str, Any]:
    """Validate a stored GPT/GPT-image record and persist the outcome."""
    if not _is_gpt_credential_record(record):
        raise ValidationError("Not a GPT account record", param="account")
    repo = repo or get_account_repository()
    if repo is None:
        raise RateLimitError("Account repository not initialised")

    ext = record.ext or {}
    access_key, status_key, error_key, attempt_key = _record_patch_keys(ext)
    access_token = _record_access_token(ext)
    account = GPTImageAccount(
        record_token=record.token,
        access_token=access_token,
        is_free=_record_is_free(ext),
        status_key=status_key,
        error_key=error_key,
    )
    extra: dict[str, Any] = {}

    if not access_token:
        email, password, mail_token = _record_credentials(ext)
        if not (email and password and mail_token):
            message = "No access token or login credentials configured"
            await _patch_account_failure(
                repo,
                account,
                ValidationError(message, param="account"),
                status="login_required",
            )
            return _test_result(record, ok=False, status="login_required", error=message)
        attempt_at = _now_ms()
        try:
            access_token = await _login_gpt_credentials_async(
                email=email,
                password=password,
                mail_token=mail_token,
            )
            account.access_token = access_token
            extra = {
                access_key: access_token,
                attempt_key: attempt_at,
            }
        except Exception as exc:
            status, message = await _patch_account_failure(
                repo,
                account,
                exc,
                status="login_failed",
                ext_merge_extra={attempt_key: attempt_at},
            )
            return _test_result(record, ok=False, status=status, error=message)

    try:
        await _validate_access_token(access_token)
        await _patch_account_success(repo, account, ext_merge_extra=extra)
        return _test_result(record, ok=True, status="available", access_token=access_token)
    except Exception as exc:
        status, message = await _patch_account_failure(
            repo,
            account,
            exc,
            ext_merge_extra=extra or None,
        )
        return _test_result(record, ok=False, status=status, error=message, access_token=access_token)


def _test_result(
    record: Any,
    *,
    ok: bool,
    status: str,
    error: str = "",
    access_token: str = "",
) -> dict[str, Any]:
    ext = record.ext or {}
    token = access_token or _record_access_token(ext)
    return {
        "id": record.token,
        "ok": ok,
        "kind": "gpt" if ext.get("gpt") else "gpt_image",
        "email": ext.get("gpt_email") or ext.get("gpt_image_email"),
        "alias": ext.get("gpt_alias") or ext.get("gpt_image_alias"),
        "capability_status": status,
        "capability_error": error or None,
        "has_access_token": bool(token),
        "access_token_masked": _mask(token),
    }


def _last_checked_key(status_key: str) -> str:
    if status_key.endswith("_status"):
        return f"{status_key[:-7]}_last_checked_at"
    return f"{status_key}_last_checked_at"


def _cooldown_until_key(status_key: str) -> str:
    if status_key.endswith("_status"):
        return f"{status_key[:-7]}_cooldown_until"
    return f"{status_key}_cooldown_until"


def _generation_cooldown_active(ext: dict[str, Any], status_key: str) -> bool:
    try:
        cooldown_until = int(ext.get(_cooldown_until_key(status_key)) or 0)
    except (TypeError, ValueError):
        cooldown_until = 0
    return bool(cooldown_until and cooldown_until > _now_ms())


def _cooldown_until_from_failure(message: str, status: str) -> int:
    text = str(message or "").lower()
    if status not in {"rate_limited", "timeout"} and not any(
        marker in text for marker in (*_QUOTA_LIMIT_MARKERS, "queued upstream")
    ):
        return 0

    cooldown_s = _GENERATION_FAILURE_COOLDOWN_S
    reset_match = re.search(
        r"reset(?:s)?\s+in\s+(?:(\d+)\s*hours?)?(?:\s*(?:and)?\s*)?(?:(\d+)\s*minutes?)?",
        text,
    )
    if reset_match:
        hours = int(reset_match.group(1) or 0)
        minutes = int(reset_match.group(2) or 0)
        parsed_s = hours * 3600 + minutes * 60
        if parsed_s > 0:
            cooldown_s = max(cooldown_s, float(parsed_s))
    elif any(marker in text for marker in _QUOTA_LIMIT_MARKERS):
        cooldown_s = max(cooldown_s, 6 * 3600.0)

    return _now_ms() + int(cooldown_s * 1000)


def _error_text(exc: BaseException) -> str:
    parts = [str(exc)]
    if isinstance(exc, UpstreamError):
        body = str(exc.details.get("body") or "")
        if body:
            parts.append(body)
    return " ".join(parts).lower()


def _is_invalid_credential_error(exc: BaseException) -> bool:
    if not isinstance(exc, UpstreamError):
        return False
    text = _error_text(exc)
    return (
        exc.status == 401
        or "token_revoked" in text
        or "invalidated auth token" in text
        or (
            exc.status == 403
            and any(marker in text for marker in _INVALID_CREDENTIAL_MARKERS)
        )
    )


def _is_invalid_credential_failure(status: int | None, message: str) -> bool:
    text = str(message or "").lower()
    return (
        status == 401
        or "token_revoked" in text
        or "invalidated auth token" in text
        or "invalid or revoked" in text
        or (
            status == 403
            and any(marker in text for marker in _INVALID_CREDENTIAL_MARKERS)
        )
    )


def _failure_message(exc: BaseException) -> str:
    if _is_invalid_credential_error(exc):
        return "ChatGPT access token is invalid or revoked; re-login or replace this GPT account"
    message = str(exc)
    if isinstance(exc, UpstreamError):
        body = str(exc.details.get("body") or "").strip()
        if body and body not in message:
            message = f"{message}: {body}"
    return message[:500]


def _is_cloudflare_524_timeout(exc: BaseException) -> bool:
    if not isinstance(exc, UpstreamError):
        return False
    text = _error_text(exc)
    return exc.status in {504, 524} and "cloudflare" in text and "524" in text


def _capability_failure_status(exc: BaseException) -> str:
    if isinstance(exc, UpstreamError):
        text = _error_text(exc)
        if _is_invalid_credential_error(exc):
            return "invalid"
        if exc.status == 429 or any(marker in text for marker in _QUOTA_LIMIT_MARKERS):
            return "rate_limited"
        if exc.status in {504, 524} or "timed out" in text or "timeout" in text:
            return "timeout"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    return "failed"


async def _run_generation(prompt: str, model: str, n: int) -> list[_GeneratedImage]:
    accounts = await _gpt_image_accounts()
    if not accounts:
        raise RateLimitError("No currently usable GPT image accounts configured")

    deadline = time.monotonic() + _generation_timeout_s()
    images: list[_GeneratedImage] = []
    failures: list[tuple[int | None, str]] = []
    last_exc: BaseException | None = None
    account_index = 0
    max_attempts = _max_account_attempts_per_image(len(accounts))
    attempt_timeout_s = _account_attempt_timeout_s()
    for _ in range(n):
        generated = False
        for _attempt in range(max_attempts):
            account = accounts[account_index % len(accounts)]
            account_index += 1
            remaining_s = deadline - time.monotonic()
            if remaining_s <= 1.0:
                last_exc = UpstreamError(
                    f"ChatGPT image generation timed out after {_generation_timeout_s():g}s",
                    status=504,
                    body="timeout",
                )
                failures.append((last_exc.status, str(last_exc)))
                break
            try:
                images.append(
                    await _generate_one(
                        account,
                        prompt,
                        model,
                        timeout_s=min(remaining_s, attempt_timeout_s),
                    )
                )
                await _mark_account_success(account)
                generated = True
                break
            except Exception as exc:
                last_exc = exc
                failures.append((
                    exc.status if isinstance(exc, AppError) else None,
                    _failure_message(exc),
                ))
                await _mark_account_failure(account, exc)
                logger.warning(
                    "gpt image account attempt failed: account={} error={}",
                    account.record_token,
                    exc,
                )
        if not generated:
            break
    if not images:
        if isinstance(last_exc, AppError) and last_exc.status not in {401, 403, 429}:
            raise last_exc
        quota_detail = next(
            (
                message
                for status, message in reversed(failures)
                if status == 429
                or any(
                    marker in message.lower()
                    for marker in (
                        "free plan limit",
                        "limit for image",
                        "limit resets",
                        "usage limit",
                        "rate limit",
                        "too many requests",
                    )
                )
            ),
            "",
        )
        if not quota_detail and failures and all(
            _is_invalid_credential_failure(status, message)
            for status, message in failures
        ):
            raise RateLimitError(
                "GPT image generation failed because the tried ChatGPT account tokens "
                "are invalid or revoked; re-login or replace those GPT accounts and retry"
            )
        detail = quota_detail or (failures[-1][1] if failures else "no account attempts were made")
        raise RateLimitError(f"GPT image generation failed across configured accounts: {detail}")
    return images


async def _run_edit(
    prompt: str,
    image_inputs: list[str],
    model: str,
    n: int,
) -> list[_GeneratedImage]:
    accounts = await _gpt_image_accounts()
    if not accounts:
        raise RateLimitError("No currently usable GPT image accounts configured")

    deadline = time.monotonic() + _generation_timeout_s()
    images: list[_GeneratedImage] = []
    failures: list[tuple[int | None, str]] = []
    last_exc: BaseException | None = None
    account_index = 0
    max_attempts = _max_account_attempts_per_image(len(accounts))
    attempt_timeout_s = _account_attempt_timeout_s()
    for _ in range(n):
        edited = False
        for _attempt in range(max_attempts):
            account = accounts[account_index % len(accounts)]
            account_index += 1
            remaining_s = deadline - time.monotonic()
            if remaining_s <= 1.0:
                last_exc = UpstreamError(
                    f"ChatGPT image edit timed out after {_generation_timeout_s():g}s",
                    status=504,
                    body="timeout",
                )
                failures.append((last_exc.status, str(last_exc)))
                break
            try:
                images.append(
                    await _edit_one(
                        account,
                        prompt,
                        image_inputs,
                        model,
                        timeout_s=min(remaining_s, attempt_timeout_s),
                    )
                )
                await _mark_account_success(account)
                edited = True
                break
            except Exception as exc:
                last_exc = exc
                failures.append((
                    exc.status if isinstance(exc, AppError) else None,
                    _failure_message(exc),
                ))
                await _mark_account_failure(account, exc)
                logger.warning(
                    "gpt image edit account attempt failed: account={} error={}",
                    account.record_token,
                    exc,
                )
        if not edited:
            break
    if not images:
        if isinstance(last_exc, AppError) and last_exc.status not in {401, 403, 429}:
            raise last_exc
        quota_detail = next(
            (
                message
                for status, message in reversed(failures)
                if status == 429
                or any(marker in message.lower() for marker in _QUOTA_LIMIT_MARKERS)
            ),
            "",
        )
        if not quota_detail and failures and all(
            _is_invalid_credential_failure(status, message)
            for status, message in failures
        ):
            raise RateLimitError(
                "GPT image edit failed because the tried ChatGPT account tokens "
                "are invalid or revoked; re-login or replace those GPT accounts and retry"
            )
        detail = quota_detail or (failures[-1][1] if failures else "no account attempts were made")
        raise RateLimitError(f"GPT image edit failed across configured accounts: {detail}")
    return images


async def generate(
    *,
    model: str,
    prompt: str,
    n: int = 1,
    response_format: str = "url",
    stream: bool = False,
    chat_format: bool = False,
) -> dict | AsyncGenerator[str, None]:
    """Generate images with ChatGPT GPT-image models."""
    if not prompt.strip():
        raise ValidationError("prompt is required", param="prompt")
    if not (1 <= n <= 4):
        raise ValidationError("n must be between 1 and 4 for GPT image models", param="n")

    model = _normalize_image_model(model)
    response_id = make_response_id()

    if stream:
        async def _sse() -> AsyncGenerator[str, None]:
            if chat_format:
                yield f"data: {orjson.dumps(make_thinking_chunk(response_id, model, 'GPT image generation started')).decode()}\n\n"
            images = await _run_generation(prompt, model, n)
            for image in images:
                chunk = make_stream_chunk(
                    response_id,
                    model,
                    _markdown_value(image, response_format) if chat_format else json.dumps(_image_value(image, response_format)),
                )
                yield f"data: {orjson.dumps(chunk).decode()}\n\n"
            final = make_stream_chunk(response_id, model, "", is_final=True)
            yield f"data: {orjson.dumps(final).decode()}\n\n"
            yield "data: [DONE]\n\n"

        return _sse()

    images = await _run_generation(prompt, model, n)
    if chat_format:
        content = "\n\n".join(_markdown_value(image, response_format) for image in images)
        return make_chat_response(
            model,
            content,
            prompt_content=prompt,
            response_id=response_id,
            reasoning_content="GPT image generation completed",
        )
    return {
        "created": int(time.time()),
        "data": [_image_value(image, response_format) for image in images],
    }


async def edit(
    *,
    model: str,
    prompt: str,
    image_inputs: list[str],
    n: int = 1,
    response_format: str = "url",
    stream: bool = False,
    chat_format: bool = False,
) -> dict | AsyncGenerator[str, None]:
    """Edit images with ChatGPT GPT-image models."""
    if not prompt.strip():
        raise ValidationError("prompt is required", param="prompt")
    if not image_inputs:
        raise ValidationError("image is required", param="image")
    if not (1 <= n <= 2):
        raise ValidationError("n must be between 1 and 2 for GPT image edits", param="n")

    model = _normalize_image_model(model)
    response_id = make_response_id()

    if stream:
        async def _sse() -> AsyncGenerator[str, None]:
            if chat_format:
                yield f"data: {orjson.dumps(make_thinking_chunk(response_id, model, 'GPT image edit started')).decode()}\n\n"
            images = await _run_edit(prompt, image_inputs, model, n)
            for image in images:
                chunk = make_stream_chunk(
                    response_id,
                    model,
                    _markdown_value(image, response_format) if chat_format else json.dumps(_image_value(image, response_format)),
                )
                yield f"data: {orjson.dumps(chunk).decode()}\n\n"
            final = make_stream_chunk(response_id, model, "", is_final=True)
            yield f"data: {orjson.dumps(final).decode()}\n\n"
            yield "data: [DONE]\n\n"

        return _sse()

    images = await _run_edit(prompt, image_inputs, model, n)
    if chat_format:
        content = "\n\n".join(_markdown_value(image, response_format) for image in images)
        return make_chat_response(
            model,
            content,
            prompt_content=prompt,
            response_id=response_id,
            reasoning_content="GPT image edit completed",
        )
    return {
        "created": int(time.time()),
        "data": [_image_value(image, response_format) for image in images],
    }


__all__ = [
    "GPTImageAccount",
    "generate",
    "edit",
    "test_gpt_account_record",
    "repair_timed_out_gpt_image_accounts",
]
