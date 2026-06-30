"""WebUI chat API routes."""

import time

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse

from app.control.model import registry as model_registry
from app.platform.config.snapshot import get_config
from app.platform.auth.middleware import WebUIUser, verify_webui_key
from app.platform.errors import ValidationError
from app.products._upstream_headers import build_upstream_response_headers
from app.products.openai.chat import completions as chat_completions
from app.products.openai.router import (
    _SSE_HEADERS,
    _safe_sse,
    _sse_with_heartbeat,
    _validate_chat,
    chat_completions_endpoint,
)
from app.products.openai.schemas import ChatCompletionRequest
from .mcp import should_handle_mcp, webui_chat_completions_with_mcp

router = APIRouter(prefix="/webui/api", tags=["WebUI - Chat"])
_WEBUI_CHAT_REQUEST_OVERRIDES = {"temporary": True, "disableMemory": True}
_WEBUI_CHAT_HIDDEN_MODELS = {"gpt-image-1", "gpt-image-2", "codex-gpt-image-2"}


def _capability_name(spec) -> str:
    if spec.is_image_edit():
        return "image_edit"
    if spec.is_image():
        return "image"
    if spec.is_video():
        return "video"
    return "chat"


@router.get("/models")
async def list_webui_models(_user: WebUIUser = Depends(verify_webui_key)):
    models = [
        {
            "id": spec.model_name,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "xai",
            "name": spec.public_name,
            "capability": _capability_name(spec),
        }
        for spec in model_registry.list_enabled()
        if spec.model_name not in _WEBUI_CHAT_HIDDEN_MODELS
    ]
    return JSONResponse({"object": "list", "data": models})


async def _webui_chat_text_completions(req: ChatCompletionRequest):
    cfg = get_config()
    spec = model_registry.get(req.model)
    if spec is None or not spec.enabled:
        from app.platform.errors import ValidationError

        raise ValidationError(
            f"Model {req.model!r} does not exist or you do not have access to it.",
            param="model",
            code="model_not_found",
        )

    is_stream = req.stream if req.stream is not None else cfg.get_bool("features.stream", True)
    request_overrides = dict(_WEBUI_CHAT_REQUEST_OVERRIDES)
    if req.deepsearch:
        request_overrides["deepsearchPreset"] = req.deepsearch
    if spec.uses_console_responses() and req.reasoning_effort is not None:
        request_overrides["_reasoning_effort"] = req.reasoning_effort
    emit_think = None if req.reasoning_effort is None else req.reasoning_effort != "none"
    result = await chat_completions(
        model=req.model,
        messages=[m.model_dump(exclude_none=True) for m in req.messages],
        stream=is_stream,
        emit_think=emit_think,
        tools=req.tools,
        tool_choice=req.tool_choice,
        temperature=req.temperature or 0.8,
        top_p=req.top_p or 0.95,
        request_overrides=request_overrides,
    )

    upstream_headers = build_upstream_response_headers(spec)
    if isinstance(result, dict):
        return JSONResponse(result, headers=upstream_headers)
    return StreamingResponse(
        _sse_with_heartbeat(_safe_sse(result)),
        media_type="text/event-stream",
        headers={**_SSE_HEADERS, **upstream_headers},
    )


@router.post("/chat/completions")
async def webui_chat_completions(
    req: ChatCompletionRequest,
    user: WebUIUser = Depends(verify_webui_key),
):
    _validate_chat(req)
    if req.model in _WEBUI_CHAT_HIDDEN_MODELS:
        raise ValidationError(
            f"Model {req.model!r} is not available in WebUI chat.",
            param="model",
            code="model_not_allowed",
        )
    if should_handle_mcp(req):
        return await webui_chat_completions_with_mcp(req, user=user)
    spec = model_registry.get(req.model)
    if spec and spec.enabled and spec.is_chat():
        return await _webui_chat_text_completions(req)
    return await chat_completions_endpoint(req)


__all__ = ["router"]
