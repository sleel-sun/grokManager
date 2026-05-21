"""Anthropic Messages API router (/v1/messages)."""

from typing import Any

import orjson
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app.platform.auth.middleware import verify_api_key
from app.platform.errors import AppError, ValidationError
from app.platform.tokens import estimate_prompt_tokens, estimate_tokens
from app.control.model import registry as model_registry


router = APIRouter(prefix="/v1", dependencies=[Depends(verify_api_key)])
_TAG_MESSAGES = "Anthropic - Messages"

_SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}


# ---------------------------------------------------------------------------
# Request schema
# ---------------------------------------------------------------------------

class _ContentBlock(BaseModel):
    model_config = {"extra": "allow"}
    type: str = "text"


class _Message(BaseModel):
    model_config = {"extra": "allow"}
    role:    str
    content: Any = ""


class MessagesRequest(BaseModel):
    model_config = {"extra": "ignore"}

    model:       str
    messages:    list[_Message]
    system:      Any = None          # string or array of content blocks
    max_tokens:  int | None = None   # ignored (Grok doesn't expose this param)
    stream:      bool | None = None
    temperature: float | None = None
    top_p:       float | None = None
    tools:       list[dict] | None = None
    tool_choice: Any = None
    thinking:    Any = None          # {type:"enabled", budget_tokens:N} — used to enable thinking output


# ---------------------------------------------------------------------------
# SSE error wrapper
# ---------------------------------------------------------------------------

async def _safe_sse_anthropic(stream):
    """Wrap an Anthropic SSE stream, converting exceptions to error events."""
    try:
        async for chunk in stream:
            yield chunk
    except AppError as exc:
        err = exc.to_dict()["error"]
        payload = orjson.dumps({"type": "error", "error": err}).decode()
        yield f"event: error\ndata: {payload}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as exc:
        payload = orjson.dumps({
            "type": "error",
            "error": {"type": "api_error", "message": str(exc)},
        }).decode()
        yield f"event: error\ndata: {payload}\n\n"
        yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# /v1/messages
# ---------------------------------------------------------------------------

@router.post("/messages", tags=[_TAG_MESSAGES])
async def messages_endpoint(req: MessagesRequest):
    from app.platform.config.snapshot import get_config

    # Model validation
    spec = model_registry.get(req.model)
    if spec is None or not spec.enabled:
        raise ValidationError(
            f"Model {req.model!r} does not exist or you do not have access to it.",
            param="model", code="model_not_found",
        )

    if not req.messages:
        raise ValidationError("messages cannot be empty", param="messages")

    cfg       = get_config()
    is_stream = req.stream if req.stream is not None else cfg.get_bool("features.stream", True)

    # thinking flag: enable when request has thinking config or config default
    if req.thinking is not None and isinstance(req.thinking, dict):
        emit_think = req.thinking.get("type") != "disabled"
    else:
        emit_think = cfg.get_bool("features.thinking", True)

    # Convert Pydantic models → plain dicts
    messages = [m.model_dump() for m in req.messages]

    from .messages import create as messages_create
    result = await messages_create(
        model        = req.model,
        messages     = messages,
        system       = req.system,
        stream       = is_stream,
        emit_think   = emit_think,
        temperature  = req.temperature or 0.8,
        top_p        = req.top_p or 0.95,
        tools        = req.tools or None,
        tool_choice  = req.tool_choice,
    )

    if isinstance(result, dict):
        return JSONResponse(result)
    return StreamingResponse(
        _safe_sse_anthropic(result),
        media_type = "text/event-stream",
        headers    = _SSE_HEADERS,
    )


# ---------------------------------------------------------------------------
# /v1/messages/count_tokens
# ---------------------------------------------------------------------------

class CountTokensRequest(BaseModel):
    """Subset of Anthropic CountMessageTokens request schema we accept.

    Mirrors the input contract of ``POST /v1/messages``: only ``messages`` is
    required, everything else is optional and used for a more accurate count.
    """

    model_config = {"extra": "ignore"}

    model:       str | None = None
    messages:    list[_Message]
    system:      Any = None
    tools:       list[dict] | None = None
    tool_choice: Any = None
    thinking:    Any = None


@router.post("/messages/count_tokens", tags=[_TAG_MESSAGES])
async def count_tokens_endpoint(req: CountTokensRequest):
    """Estimate the input token count for an Anthropic-format request.

    Mirrors `Anthropic's Count Message Tokens endpoint`__ — used by the
    official SDK and agents (Claude Code, etc.) for pre-flight budgeting.

    Token counting uses the same tiktoken-backed estimator as
    :func:`app.platform.tokens.estimate_prompt_tokens` so the reported value
    is consistent with the ``usage.input_tokens`` field returned by
    ``/v1/messages``.

    __ https://docs.anthropic.com/en/api/messages-count-tokens
    """
    if not req.messages:
        raise ValidationError("messages cannot be empty", param="messages")

    # Lazy import to avoid a top-level dependency on the Messages handler
    # (which itself imports heavier dataplane modules).
    from .messages import _parse_anthropic_messages, _convert_tools

    messages_payload = [m.model_dump() for m in req.messages]
    internal_messages = _parse_anthropic_messages(messages_payload, req.system)

    total = estimate_prompt_tokens(internal_messages)

    if req.tools:
        # Tool schemas are part of the prompt the model sees, so they count
        # against ``input_tokens`` in the same way the messages do.
        total += estimate_tokens(_convert_tools(req.tools))

    return JSONResponse({"input_tokens": total})


__all__ = ["router"]
