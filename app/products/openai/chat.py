"""Chat completion service — orchestrates account selection, reverse, streaming."""

import asyncio
import base64
import binascii
import hashlib
import re
from typing import Any, AsyncGenerator

import orjson

from app.platform.logging.logger import logger
from app.platform.config.snapshot import get_config
from app.platform.errors import RateLimitError, UpstreamError, ValidationError
from app.platform.runtime.clock import now_s
from app.platform.storage import save_local_image
from app.platform.tokens import (
    estimate_prompt_tokens,
    estimate_tokens,
    estimate_tool_call_tokens,
)
from app.control.account.runtime import get_refresh_service
from app.control.account.invalid_credentials import feedback_kind_for_error
from app.control.model.registry import resolve as resolve_model
from app.control.model.enums import ModeId
from app.control.model.spec import ModelSpec
from app.control.account.enums import FeedbackKind
from app.dataplane.account.selector import current_strategy
from app.dataplane.proxy.adapters.headers import build_http_headers
from app.dataplane.proxy import get_proxy_runtime
from app.dataplane.proxy.adapters.session import (
    ResettableSession,
    build_session_kwargs,
)
from app.dataplane.reverse.protocol.xai_chat import (
    build_chat_payload,
    classify_line,
    StreamAdapter,
)
from app.dataplane.reverse.protocol.xai_console import (
    ConsoleResponsesStreamAdapter,
    build_console_responses_payload,
    client_function_tool_names,
    console_tool_choice_override,
    ensure_console_search_tools,
    split_console_server_tools,
)
from app.dataplane.reverse.protocol.xai_usage import is_invalid_credentials_error
from app.dataplane.reverse.planner import build_plan
from app.dataplane.reverse.runtime.endpoint_table import CHAT, CONSOLE_RESPONSES
from app.dataplane.reverse.transport.asset_upload import upload_from_input
from app.dataplane.reverse.protocol.tool_prompt import (
    build_tool_system_prompt,
    extract_tool_names,
    inject_into_message,
    tool_calls_to_xml,
)
from app.dataplane.reverse.protocol.tool_parser import parse_tool_calls
from ._format import (
    make_response_id,
    make_stream_chunk,
    make_thinking_chunk,
    make_chat_response,
    make_tool_call_chunk,
    make_tool_call_done_chunk,
    make_tool_call_response,
    build_usage,
)
from ._tool_sieve import ToolSieve
from app.products._account_selection import reserve_account, selection_max_retries

_FileInput = str | dict[str, str]


def _to_chat_annotations(anns: list[dict]) -> list[dict]:
    """扁平 annotations → Chat Completions 嵌套格式（内层无 type）"""
    return (
        [
            {
                "type": "url_citation",
                "url_citation": {
                    "url": a["url"],
                    "title": a["title"],
                    "start_index": a["start_index"],
                    "end_index": a["end_index"],
                },
            }
            for a in anns
        ]
        if anns
        else []
    )


def _log_task_exception(task: "asyncio.Task") -> None:
    """Done-callback: log exceptions from fire-and-forget tasks."""
    exc = task.exception() if not task.cancelled() else None
    if exc:
        logger.warning("background task failed: task={} error={}", task.get_name(), exc)


def _upstream_body_excerpt(exc: UpstreamError, *, limit: int = 240) -> str:
    body = _upstream_body(exc).replace("\n", "\\n")
    return body[:limit] or "-"


def _upstream_body(exc: UpstreamError) -> str:
    details = getattr(exc, "details", {})
    if not isinstance(details, dict):
        return ""
    return str(details.get("body", "") or "")


def _chat_pool_names(spec: ModelSpec) -> list[str]:
    pool_names = {0: "basic", 1: "super", 2: "heavy"}
    return [pool_names.get(pool_id, str(pool_id)) for pool_id in spec.pool_candidates()]


def _no_available_account_error(spec: ModelSpec) -> RateLimitError:
    pools = ", ".join(_chat_pool_names(spec))
    return RateLimitError(
        f"No active/manageable accounts are available for chat model {spec.model_name!r}; "
        f"required pool(s): {pools}."
    )


def _transport_upstream_error(exc: BaseException, *, context: str) -> UpstreamError:
    if isinstance(exc, UpstreamError):
        return exc
    body = str(exc).replace("\n", "\\n")[:400]
    return UpstreamError(
        f"{context}: {exc}",
        status=502,
        body=body,
    )


def _raise_chat_status_error(
    *,
    spec: ModelSpec | None,
    status_code: int,
    body: str,
) -> None:
    if spec and spec.uses_console_responses() and status_code == 404:
        upstream_model = spec.upstream_model_name()
        raise UpstreamError(
            "Console Responses upstream does not expose model "
            f"{upstream_model!r} for public model {spec.model_name!r}; "
            "the model is listed locally for diagnostics, but it is unavailable "
            "until the upstream account/Console API exposes that model id or the "
            "registry maps it to a supported upstream model.",
            status=status_code,
            body=body,
        )
    raise UpstreamError(
        f"Chat upstream returned {status_code}",
        status=status_code,
        body=body,
    )


async def _quota_sync(token: str, mode_id: int) -> None:
    """Fire-and-forget: fetch real quota after a successful call."""
    try:
        if current_strategy() != "quota":
            return
        svc = get_refresh_service()
        if svc:
            await svc.refresh_call_async(token, mode_id)
    except Exception as exc:
        logger.warning(
            "chat quota sync failed: token={}... mode_id={} error={}",
            token[:10],
            mode_id,
            exc,
        )


async def _fail_sync(
    token: str, mode_id: int, exc: BaseException | None = None
) -> None:
    """Fire-and-forget: persist failure metadata after a failed call.

    In random mode this helper must not trigger upstream quota probes. It still
    records failures so 401 invalidation and local failure accounting continue
    to work unchanged.
    """
    try:
        svc = get_refresh_service()
        if svc:
            await svc.record_failure_async(token, mode_id, exc)
            if (
                current_strategy() == "quota"
                and getattr(exc, "status", None) == 429
            ):
                result = await svc.refresh_on_demand()
                logger.info(
                    "account on-demand refresh triggered: token={}... mode_id={} refreshed={} failed={} rate_limited={}",
                    token[:10],
                    mode_id,
                    result.refreshed,
                    result.failed,
                    result.rate_limited,
                )
    except Exception as e:
        logger.warning(
            "chat fail sync error: token={}... mode_id={} error={}",
            token[:10],
            mode_id,
            e,
        )


def _parse_retry_codes(s: str) -> frozenset[int]:
    """Parse retry status-code config from either a CSV string or a list."""
    result: set[int] = set()
    parts: list[object]
    if isinstance(s, str):
        parts = [part.strip() for part in s.split(",")]
    elif isinstance(s, (list, tuple, set)):
        parts = list(s)
    else:
        return frozenset()
    for part in parts:
        text = str(part).strip()
        if text.isdigit():
            result.add(int(text))
    return frozenset(result)


def _configured_retry_codes(cfg) -> frozenset[int]:
    """Read retry codes from current config, including legacy array keys."""
    raw = cfg.get("retry.on_codes")
    if raw is None:
        raw = cfg.get("retry.retry_status_codes", "429,401,502,503")
    return _parse_retry_codes(raw)


def _should_retry_upstream(exc: UpstreamError, retry_codes: frozenset[int]) -> bool:
    """Return whether this upstream error is retryable by the chat loop."""
    if _is_transient_transport_error(exc):
        return True
    if _is_account_scoped_forbidden(exc):
        return True
    return exc.status in retry_codes or is_invalid_credentials_error(exc)


_CLOUDFLARE_CHALLENGE_MARKERS = (
    "just a moment",
    "cf-challenge",
    "cf-mitigated",
    "cloudflare",
)
_CHAT_ACCOUNT_RETRY_MIN_RETRIES = 20
_CHAT_TRANSPORT_RETRY_MAX_RETRIES = 2
_TRANSIENT_TRANSPORT_STATUSES = frozenset({502, 503, 504})
_TRANSIENT_TRANSPORT_MARKERS = (
    "transport request failed",
    "transport failed",
    "stream read failed",
    "curl:",
    "connect tunnel",
    "http/2 stream",
    "connection reset",
    "timed out",
)


def _is_transient_transport_error(exc: UpstreamError) -> bool:
    """Return whether this is a proxy/network failure, not account quota."""
    if exc.status not in _TRANSIENT_TRANSPORT_STATUSES:
        return False
    haystack = f"{exc} {_upstream_body(exc)}".lower()
    return any(marker in haystack for marker in _TRANSIENT_TRANSPORT_MARKERS)


def _is_account_scoped_forbidden(exc: UpstreamError) -> bool:
    """Treat empty/non-CF 403 responses as account entitlement failures."""
    if exc.status != 403:
        return False
    return not _is_cloudflare_challenge(exc)


def _is_cloudflare_challenge(exc: UpstreamError) -> bool:
    """Return whether the upstream error is a proxy/clearance challenge."""
    if exc.status != 403:
        return False
    haystack = f"{_upstream_body(exc)} {exc}".lower()
    return any(marker in haystack for marker in _CLOUDFLARE_CHALLENGE_MARKERS)


def _should_retry_same_account_upstream(exc: UpstreamError) -> bool:
    """Retry transient proxy clearance failures without rotating accounts."""
    return _is_cloudflare_challenge(exc)


def _chat_max_retries(cfg) -> int:
    """Use enough retries to rotate past account-specific 403 failures."""
    configured = max(0, selection_max_retries())
    account_min = max(
        0,
        cfg.get_int("chat.account_retry_min_retries", _CHAT_ACCOUNT_RETRY_MIN_RETRIES),
    )
    return max(configured, account_min)


def _chat_transport_max_retries(cfg) -> int:
    """Cap proxy/HTTP transport retries separately from account rotation."""
    return max(
        0,
        cfg.get_int(
            "chat.transport_retry_max_retries",
            _CHAT_TRANSPORT_RETRY_MAX_RETRIES,
        ),
    )


def _feedback_kind(exc: BaseException) -> "FeedbackKind":
    """Map an upstream exception to the appropriate FeedbackKind."""
    return feedback_kind_for_error(exc)


_CONSOLE_ONLY_REQUEST_OVERRIDE_KEYS = frozenset(
    {
        "_reasoning_effort",
        "reasoning",
        "reasoning_effort",
        "tools",
        "tool_choice",
    }
)


def _uses_console_responses_transport(
    spec: ModelSpec | None,
    files: list[_FileInput] | None = None,
) -> bool:
    """Console Responses currently rejects multimodal input; use app-chat then."""
    return bool(spec and spec.uses_console_responses() and not files)


def _legacy_chat_request_overrides(
    request_overrides: dict | None,
) -> dict | None:
    if not request_overrides:
        return None
    cleaned = {
        key: value
        for key, value in request_overrides.items()
        if value is not None and key not in _CONSOLE_ONLY_REQUEST_OVERRIDE_KEYS
    }
    return cleaned or None


def _prepare_console_request_tools(
    *,
    tools: list[Any] | None,
    tool_choice: Any,
    spec: ModelSpec,
    cfg: Any,
    request_overrides: dict | None,
) -> tuple[list[dict[str, Any]] | None, dict | None]:
    local_tools, console_tools = split_console_server_tools(tools, spec)
    if spec.uses_console_responses() and cfg.get_bool(
        "features.console_default_search",
        False,
    ):
        console_tools = ensure_console_search_tools(console_tools)

    if console_tools:
        request_overrides = request_overrides or {}
        request_overrides["tools"] = console_tools
        console_choice = console_tool_choice_override(
            tool_choice, local_tools=local_tools
        )
        if console_choice is not None:
            request_overrides["tool_choice"] = console_choice
    return local_tools, request_overrides


def _chat_exhausted_error(
    model: str,
    *,
    attempted_accounts: int,
    last_exc: UpstreamError,
) -> BaseException:
    if _is_account_scoped_forbidden(last_exc) or last_exc.status == 429:
        return RateLimitError(
            f"Chat model {model!r} has no available account quota/entitlement "
            f"after {attempted_accounts} account attempts; "
            f"last upstream status={last_exc.status}."
        )
    return last_exc


async def _download_image_bytes(token: str, url: str) -> tuple[bytes, str]:
    """Download image bytes via the shared asset transport used by /v1/images."""
    from app.dataplane.reverse.protocol.xai_assets import infer_content_type
    from app.dataplane.reverse.transport.assets import download_asset

    try:
        stream, content_type = await download_asset(token, url)
        chunks: list[bytes] = []
        async for chunk in stream:
            chunks.append(chunk)
    except UpstreamError:
        raise
    except Exception as exc:
        raise UpstreamError(f"Image download failed: {exc}") from exc
    return b"".join(chunks), (content_type or infer_content_type(url) or "image/jpeg")


_LOCAL_IMAGE_ID_RE = re.compile(r"^[0-9a-fA-F\-]{16,36}$")
_DATA_IMAGE_RE = re.compile(r"^data:(image/[^;,]+);base64,(.*)$", re.IGNORECASE | re.DOTALL)
_GROK_GENERATED_IMAGE_URL_RE = re.compile(
    r"^https?://grok\.x\.ai/generated-image-[^?#]+\.(?:png|jpe?g|webp|gif)"
    r"(?:[?#].*)?$",
    re.IGNORECASE,
)


def _safe_image_file_id(url: str, image_id: str) -> str:
    candidate = (image_id or "").strip().split(".", 1)[0]
    if _LOCAL_IMAGE_ID_RE.fullmatch(candidate):
        return candidate.lower()
    return hashlib.sha1((url or candidate).encode("utf-8")).hexdigest()[:32]


def _decode_data_image(url: str) -> tuple[bytes, str] | None:
    match = _DATA_IMAGE_RE.match((url or "").strip())
    if not match:
        return None
    try:
        return base64.b64decode(match.group(2), validate=True), match.group(1)
    except (ValueError, TypeError, binascii.Error):
        return None


def _is_inaccessible_generated_image_url(url: str) -> bool:
    return bool(_GROK_GENERATED_IMAGE_URL_RE.match((url or "").strip()))


def _save_image(raw: bytes, mime: str, image_id: str) -> str:
    """Save raw bytes to ``${DATA_DIR}/files/images`` and return the file ID."""
    return save_local_image(raw, mime, image_id)


async def _resolve_image(token: str, url: str, image_id: str) -> str:
    """Return the image embed text for the response body based on image_format config.

    Format values:
      grok_url  — raw CDN URL (no download)
      local_url — download + serve locally, return accessible URL
      grok_md   — ![image](grok_cdn_url) markdown
      local_md  — ![image](local_url) markdown
      base64    — ![image](data:...) markdown
    """
    cfg = get_config()
    fmt = _normalize_image_format(cfg.get_str("features.image_format", "grok_url"))
    data_image = _decode_data_image(url)
    if data_image is not None:
        raw, mime = data_image
        if fmt == "base64":
            b64 = base64.b64encode(raw).decode()
            return f"![image](data:{mime};base64,{b64})"
        if fmt in {"local_url", "local_md"}:
            file_id = await asyncio.to_thread(
                _save_image,
                raw,
                mime,
                _safe_image_file_id(url, image_id),
            )
            app_url = cfg.get_str("app.app_url", "").rstrip("/")
            local_url = (
                f"{app_url}/v1/files/image?id={file_id}"
                if app_url
                else f"/v1/files/image?id={file_id}"
            )
            return local_url if fmt == "local_url" else f"![image]({local_url})"
        return url if fmt == "grok_url" else f"![image]({url})"

    # Formats that don't need downloading
    if fmt == "grok_url":
        return url
    if fmt == "grok_md":
        return f"![image]({url})"

    # Formats that require downloading
    try:
        raw, mime = await _download_image_bytes(token, url)
    except Exception as exc:
        if _is_inaccessible_generated_image_url(url):
            logger.warning(
                "chat image download failed: dropping_inaccessible_grok_generated_url error={}",
                exc,
            )
            return ""
        logger.warning(
            "chat image download failed: fallback_to=upstream_url error={}", exc
        )
        return url

    if fmt == "base64":
        b64 = base64.b64encode(raw).decode()
        return f"![image](data:{mime};base64,{b64})"

    # local_url / local_md: save to disk and return local path
    file_id = await asyncio.to_thread(
        _save_image,
        raw,
        mime,
        _safe_image_file_id(url, image_id),
    )
    app_url = cfg.get_str("app.app_url", "").rstrip("/")
    local_url = (
        f"{app_url}/v1/files/image?id={file_id}"
        if app_url
        else f"/v1/files/image?id={file_id}"
    )

    if fmt == "local_url":
        return local_url
    return f"![image]({local_url})"  # local_md


def _normalize_image_format(value: str | None) -> str:
    fmt = (value or "grok_url").strip().lower()
    if fmt not in {"grok_url", "local_url", "grok_md", "local_md", "base64"}:
        raise ValidationError(
            "image_format must be one of [grok_url, local_url, grok_md, local_md, base64]",
            param="features.image_format",
        )
    return fmt


_THINK_TAG_RE = re.compile(r"<think>[\s\S]*?</think>")
_INLINE_BASE64_IMG_RE = re.compile(r"!\[image\]\(data:[^)]*?base64,[^)]*?\)")
# 精确匹配 grok2api 注入的 Sources 段落（含标记行），用于多轮对话剥离
_SOURCES_STRIP_RE = re.compile(
    r"(?:^|\r?\n\r?\n)## Sources\r?\n\[grok2api-sources\]: #\r?\n[\s\S]*$"
)


def _strip_generated_artifacts(text: str, *, strip_sources: bool = False) -> str:
    """Remove generated assistant artifacts before reusing conversation history."""
    if not text or not isinstance(text, str):
        return text
    if strip_sources:
        text = _SOURCES_STRIP_RE.sub("", text)
    text = _THINK_TAG_RE.sub("", text).strip()
    return _INLINE_BASE64_IMG_RE.sub("[图片]", text)


def _extract_message(messages: list[dict]) -> tuple[str, list[_FileInput]]:
    """Flatten OpenAI messages into a single prompt string + file attachments."""
    parts: list[str] = []
    files: list[_FileInput] = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content") or ""
        tool_calls = msg.get("tool_calls")

        # ── role=tool: tool execution result ─────────────────────────────────
        if role == "tool":
            tool_call_id = msg.get("tool_call_id", "")
            label = (
                f"[tool result for {tool_call_id}]" if tool_call_id else "[tool result]"
            )
            text = content.strip() if isinstance(content, str) else ""
            if text:
                parts.append(f"{label}:\n{text}")
            continue

        # ── role=assistant with tool_calls: reconstruct as XML ────────────────
        if role == "assistant" and tool_calls:
            xml = tool_calls_to_xml(tool_calls)
            # Prepend any accompanying text content (rare but valid)
            text = content.strip() if isinstance(content, str) else ""
            if text:
                parts.append(f"[assistant]: {text}\n{xml}")
            else:
                parts.append(f"[assistant]:\n{xml}")
            continue

        # ── 剥离前轮 assistant 消息中 grok2api 注入的 Sources 段落 ────────────
        if role == "assistant" and isinstance(content, str):
            content = _strip_generated_artifacts(content, strip_sources=True)

        # ── normal content handling ───────────────────────────────────────────
        if isinstance(content, str):
            cleaned = _strip_generated_artifacts(content.strip())
            if cleaned:
                parts.append(f"[{role}]: {cleaned}")
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text") or ""
                    text = _strip_generated_artifacts(
                        text.strip(),
                        strip_sources=(role == "assistant"),
                    )
                    if text:
                        parts.append(f"[{role}]: {text}")
                elif btype == "image_url":
                    url = (block.get("image_url") or {}).get("url", "")
                    if url:
                        files.append(url)
                elif btype in ("input_audio", "file"):
                    inner = block.get(btype) or {}
                    data = inner.get("data") or inner.get("file_data", "")
                    if data:
                        filename = str(inner.get("filename") or "").strip()
                        if filename:
                            files.append({"data": data, "filename": filename})
                        else:
                            files.append(data)

    return "\n\n".join(parts), files


async def _prepare_file_attachments(token: str, file_inputs: list[_FileInput]) -> list[str]:
    """Upload OpenAI-style multimodal inputs and return Grok chat attachment IDs."""
    attachments: list[str] = []
    for file_input in file_inputs:
        if not file_input:
            continue
        if isinstance(file_input, dict):
            file_id, _file_uri = await upload_from_input(
                token,
                file_input.get("data", ""),
                filename=file_input.get("filename"),
            )
        else:
            file_id, _file_uri = await upload_from_input(token, file_input)
        if file_id:
            attachments.append(file_id)
    return attachments


async def _stream_chat(
    token: str,
    mode_id: "ModeId",
    message: str,
    files: list[_FileInput],
    *,
    spec: ModelSpec | None = None,
    tool_overrides: dict | None = None,
    model_config_override: dict | None = None,
    request_overrides: dict | None = None,
    messages: list[dict] | None = None,
    timeout_s: float = 120.0,
) -> AsyncGenerator[str, None]:
    """Yield raw SSE lines from the selected upstream chat endpoint."""
    proxy = await get_proxy_runtime()
    lease = await proxy.acquire()
    use_console_transport = _uses_console_responses_transport(spec, files)
    plan = (
        build_plan(spec, request_overrides or {})
        if spec and (use_console_transport or not spec.uses_console_responses())
        else None
    )

    if use_console_transport:
        endpoint = plan.endpoint if plan else CONSOLE_RESPONSES
        origin = plan.origin if plan else "https://console.x.ai"
        referer = plan.referer if plan else "https://console.x.ai/"
        content_type = plan.content_type if plan else "application/json"
        payload = build_console_responses_payload(
            model=spec.upstream_model_name(),
            message=message,
            stream=True,
            public_model=spec.model_name,
            spec=spec,
            request_overrides=request_overrides,
            messages=messages,
        )
        transport_context = "Console Responses transport failed"
        stream_context = "Console Responses stream read failed"
    else:
        is_console_attachment_fallback = bool(spec and spec.uses_console_responses())
        endpoint = plan.endpoint if plan else CHAT
        origin = plan.origin if plan else "https://grok.com"
        referer = plan.referer if plan else "https://grok.com/"
        content_type = plan.content_type if plan else "application/json"
        attachments = await _prepare_file_attachments(token, files)
        payload = build_chat_payload(
            message=message,
            mode_id=ModeId.FAST if is_console_attachment_fallback else mode_id,
            file_attachments=attachments,
            tool_overrides=tool_overrides,
            model_config_override=model_config_override,
            request_overrides=(
                _legacy_chat_request_overrides(request_overrides)
                if is_console_attachment_fallback
                else request_overrides
            ),
        )
        transport_context = "Chat transport failed"
        stream_context = "Chat stream read failed"

    payload_bytes = orjson.dumps(payload)

    headers = build_http_headers(
        token,
        content_type=content_type,
        origin=origin,
        referer=referer,
        lease=lease,
    )
    session_kwargs = build_session_kwargs(lease=lease)

    async with ResettableSession(**session_kwargs) as session:
        try:
            response = await session.post(
                endpoint,
                headers=headers,
                data=payload_bytes,
                timeout=timeout_s,
                stream=True,
            )
        except Exception as exc:
            raise _transport_upstream_error(
                exc, context=transport_context
            ) from exc

        if response.status_code != 200:
            try:
                body = response.content.decode("utf-8", "replace")[:400]
            except Exception:
                body = ""
            _raise_chat_status_error(
                spec=spec,
                status_code=response.status_code,
                body=body,
            )

        try:
            async for line in response.aiter_lines():
                yield line
        except Exception as exc:
            raise _transport_upstream_error(
                exc, context=stream_context
            ) from exc


def _new_stream_adapter(
    spec: ModelSpec,
    files: list[_FileInput] | None = None,
    function_tool_names: set[str] | list[str] | tuple[str, ...] | None = None,
) -> StreamAdapter | ConsoleResponsesStreamAdapter:
    """Return a stream adapter matching the selected upstream protocol."""
    if _uses_console_responses_transport(spec, files):
        return ConsoleResponsesStreamAdapter(function_tool_names=function_tool_names)
    return StreamAdapter()


async def completions(
    *,
    model: str,
    messages: list[dict],
    stream: bool | None = None,
    emit_think: bool | None = None,
    tools: list[dict] | None = None,
    tool_choice: Any = None,
    temperature: float = 0.8,
    top_p: float = 0.95,
    request_overrides: dict | None = None,
) -> dict | AsyncGenerator[str, None]:
    """Entry point for /v1/chat/completions.

    Returns an async generator for streaming, or a dict for non-streaming.
    Supports transparent retry with a different account on configured HTTP
    status codes (chat.retry_on_codes) up to chat.max_retries times.
    """
    cfg = get_config()
    spec = resolve_model(model)
    is_stream = stream if stream is not None else cfg.get_bool("features.stream", True)
    if emit_think is None:
        emit_think = cfg.get_bool("features.thinking", True)

    logger.info(
        "chat request accepted: model={} stream={} message_count={}",
        model,
        is_stream,
        len(messages),
    )

    message, files = _extract_message(messages)
    if not message.strip():
        raise UpstreamError("Empty message after extraction", status=400)

    from app.dataplane.account import _directory as _acct_dir

    if _acct_dir is None:
        raise RateLimitError("Account directory not initialised")
    directory = _acct_dir

    max_retries = _chat_max_retries(cfg)
    transport_max_retries = _chat_transport_max_retries(cfg)
    retry_codes = _configured_retry_codes(cfg)
    response_id = make_response_id()
    timeout_s = cfg.get_float("chat.timeout", 120.0)

    # ── Tool call setup ───────────────────────────────────────────────────────
    tool_names: list[str] = []
    local_tools, request_overrides = _prepare_console_request_tools(
        tools=tools,
        tool_choice=tool_choice,
        spec=spec,
        cfg=cfg,
        request_overrides=request_overrides,
    )
    native_tool_names = (
        client_function_tool_names(tools)
        if _uses_console_responses_transport(spec, files)
        else set()
    )
    if local_tools:
        tool_names = extract_tool_names(local_tools)
        tool_prompt = build_tool_system_prompt(local_tools, tool_choice)
        message = inject_into_message(message, tool_prompt)
    tool_overrides: dict | None = None

    # ── Streaming path ────────────────────────────────────────────────────────
    if is_stream:

        async def _run_stream() -> AsyncGenerator[str, None]:
            excluded: list[str] = []
            transport_retry_count = 0
            for attempt in range(max_retries + 1):
                acct, selected_mode_id = await reserve_account(
                    directory,
                    spec,
                    now_s_override=now_s(),
                    exclude_tokens=excluded or None,
                )
                if acct is None:
                    raise _no_available_account_error(spec)

                token = acct.token
                success = False
                _retry = False
                _retry_same_account = False
                fail_exc: BaseException | None = None
                adapter = _new_stream_adapter(spec, files, native_tool_names)
                collected_annotations: list[dict] = []
                native_text_buffer: list[str] = []

                try:
                    try:
                        ended = False
                        sieve = ToolSieve(tool_names)
                        tool_calls_emitted = False
                        async for line in _stream_chat(
                            token=token,
                            mode_id=ModeId(selected_mode_id),
                            message=message,
                            files=files,
                            spec=spec,
                            tool_overrides=tool_overrides,
                            request_overrides=request_overrides,
                            messages=messages if native_tool_names else None,
                            timeout_s=timeout_s,
                        ):
                            event_type, data = classify_line(line)
                            if event_type == "done":
                                break
                            if event_type != "data" or not data:
                                continue
                            events = adapter.feed(data)
                            for ev in events:
                                if tool_calls_emitted:
                                    break  # already sent [DONE], drop remaining events
                                if ev.kind == "text":
                                    if native_tool_names:
                                        native_text_buffer.append(ev.content)
                                        continue
                                    if tool_names:
                                        safe_text, parsed_calls = sieve.feed(ev.content)
                                        if safe_text:
                                            chunk = make_stream_chunk(
                                                response_id, model, safe_text
                                            )
                                            yield f"data: {orjson.dumps(chunk).decode()}\n\n"
                                        if parsed_calls is not None:
                                            for i, tc in enumerate(parsed_calls):
                                                chunk = make_tool_call_chunk(
                                                    response_id,
                                                    model,
                                                    i,
                                                    tc.call_id,
                                                    tc.name,
                                                    tc.arguments,
                                                    is_first=True,
                                                )
                                                yield f"data: {orjson.dumps(chunk).decode()}\n\n"
                                            done_chunk = make_tool_call_done_chunk(
                                                response_id, model
                                            )
                                            yield f"data: {orjson.dumps(done_chunk).decode()}\n\n"
                                            yield "data: [DONE]\n\n"
                                            tool_calls_emitted = True
                                            success = True
                                            logger.info(
                                                "chat stream tool_calls: attempt={}/{} model={} call_count={}",
                                                attempt + 1,
                                                max_retries + 1,
                                                model,
                                                len(parsed_calls),
                                            )
                                            ended = True
                                            break  # stop processing remaining events in this batch
                                    else:
                                        chunk = make_stream_chunk(
                                            response_id, model, ev.content
                                        )
                                        yield f"data: {orjson.dumps(chunk).decode()}\n\n"
                                elif ev.kind == "thinking" and emit_think:
                                    chunk = make_thinking_chunk(
                                        response_id, model, ev.content
                                    )
                                    yield f"data: {orjson.dumps(chunk).decode()}\n\n"
                                elif ev.kind == "annotation" and ev.annotation_data:
                                    collected_annotations.append(ev.annotation_data)
                                elif ev.kind == "tool_calls" and ev.tool_calls:
                                    for i, tc in enumerate(ev.tool_calls):
                                        chunk = make_tool_call_chunk(
                                            response_id,
                                            model,
                                            i,
                                            tc.call_id,
                                            tc.name,
                                            tc.arguments,
                                            is_first=True,
                                        )
                                        yield f"data: {orjson.dumps(chunk).decode()}\n\n"
                                    done_chunk = make_tool_call_done_chunk(
                                        response_id, model
                                    )
                                    yield f"data: {orjson.dumps(done_chunk).decode()}\n\n"
                                    yield "data: [DONE]\n\n"
                                    tool_calls_emitted = True
                                    success = True
                                    ended = True
                                    logger.info(
                                        "chat stream native tool_calls: attempt={}/{} model={} call_count={}",
                                        attempt + 1,
                                        max_retries + 1,
                                        model,
                                        len(ev.tool_calls),
                                    )
                                    break
                                elif ev.kind == "soft_stop":
                                    ended = True
                                    break
                            if ended:
                                break

                        if not tool_calls_emitted and tool_names:
                            # Stream ended — flush sieve for any buffered XML
                            flushed_calls = sieve.flush()
                            if flushed_calls:
                                for i, tc in enumerate(flushed_calls):
                                    chunk = make_tool_call_chunk(
                                        response_id,
                                        model,
                                        i,
                                        tc.call_id,
                                        tc.name,
                                        tc.arguments,
                                        is_first=True,
                                    )
                                    yield f"data: {orjson.dumps(chunk).decode()}\n\n"
                                done_chunk = make_tool_call_done_chunk(
                                    response_id, model
                                )
                                # 注入结构化搜索信源（tool_calls 场景）
                                sources = adapter.search_sources_list()
                                if sources:
                                    done_chunk["search_sources"] = sources
                                yield f"data: {orjson.dumps(done_chunk).decode()}\n\n"
                                yield "data: [DONE]\n\n"
                                tool_calls_emitted = True
                                success = True
                                logger.info(
                                    "chat stream tool_calls (flushed): model={} call_count={}",
                                    model,
                                    len(flushed_calls),
                                )

                        if not tool_calls_emitted:
                            if native_text_buffer:
                                chunk = make_stream_chunk(
                                    response_id, model, "".join(native_text_buffer)
                                )
                                yield f"data: {orjson.dumps(chunk).decode()}\n\n"
                            extract_images = getattr(
                                adapter, "extract_generated_images_from_text", None
                            )
                            if callable(extract_images):
                                extract_images("".join(adapter.text_buf))
                            for url, img_id in adapter.image_urls:
                                img_text = await _resolve_image(token, url, img_id)
                                if not img_text:
                                    continue
                                chunk = make_stream_chunk(
                                    response_id, model, img_text + "\n"
                                )
                                yield f"data: {orjson.dumps(chunk).decode()}\n\n"

                            references = adapter.references_suffix()
                            if references:
                                chunk = make_stream_chunk(
                                    response_id, model, references
                                )
                                yield f"data: {orjson.dumps(chunk).decode()}\n\n"

                            chat_anns = _to_chat_annotations(collected_annotations)
                            final = make_stream_chunk(
                                response_id,
                                model,
                                "",
                                is_final=True,
                                annotations=chat_anns or None,
                            )
                            # 注入结构化搜索信源到 chunk 根对象（避免 delta strict schema 拒绝）
                            sources = adapter.search_sources_list()
                            if sources:
                                final["search_sources"] = sources
                            yield f"data: {orjson.dumps(final).decode()}\n\n"
                            yield "data: [DONE]\n\n"
                            success = True
                            logger.info(
                                "chat stream completed: attempt={}/{} model={} image_count={}",
                                attempt + 1,
                                max_retries + 1,
                                model,
                                len(adapter.image_urls),
                            )

                    except UpstreamError as exc:
                        fail_exc = exc
                        if (
                            _should_retry_same_account_upstream(exc)
                            and attempt < max_retries
                        ):
                            _retry = True
                            _retry_same_account = True
                            logger.warning(
                                "chat stream same-account retry scheduled: attempt={}/{} status={} token={}...",
                                attempt + 1,
                                max_retries,
                                exc.status,
                                token[:8],
                            )
                        elif (
                            _is_transient_transport_error(exc)
                            and transport_retry_count < transport_max_retries
                            and attempt < max_retries
                        ):
                            transport_retry_count += 1
                            _retry = True
                            _retry_same_account = True
                            logger.warning(
                                "chat stream transport retry scheduled: transport_attempt={}/{} attempt={}/{} status={} token={}...",
                                transport_retry_count,
                                transport_max_retries,
                                attempt + 1,
                                max_retries,
                                exc.status,
                                token[:8],
                            )
                        elif (
                            not _is_transient_transport_error(exc)
                            and _should_retry_upstream(exc, retry_codes)
                            and attempt < max_retries
                        ):
                            _retry = True
                            logger.warning(
                                "chat stream retry scheduled: attempt={}/{} status={} token={}...",
                                attempt + 1,
                                max_retries,
                                exc.status,
                                token[:8],
                            )
                        else:
                            logger.warning(
                                "chat stream upstream failed: attempt={}/{} model={} status={} body={}",
                                attempt + 1,
                                max_retries + 1,
                                model,
                                exc.status,
                                _upstream_body_excerpt(exc),
                            )
                            raise _chat_exhausted_error(
                                model,
                                attempted_accounts=attempt + 1,
                                last_exc=exc,
                            ) from exc

                finally:
                    await directory.release(acct)
                    kind = (
                        FeedbackKind.SUCCESS
                        if success
                        else _feedback_kind(fail_exc)
                        if fail_exc
                        else FeedbackKind.SERVER_ERROR
                    )
                    await directory.feedback(
                        token, kind, selected_mode_id, now_s_val=now_s()
                    )
                    if success:
                        asyncio.create_task(
                            _quota_sync(token, selected_mode_id)
                        ).add_done_callback(_log_task_exception)
                    else:
                        asyncio.create_task(
                            _fail_sync(token, selected_mode_id, fail_exc)
                        ).add_done_callback(_log_task_exception)

                if success or not _retry:
                    return
                if not _retry_same_account:
                    excluded.append(token)

        return _run_stream()

    # ── Non-streaming path ────────────────────────────────────────────────────
    excluded: list[str] = []
    transport_retry_count = 0
    token = ""
    adapter = _new_stream_adapter(spec, files, native_tool_names)
    for attempt in range(max_retries + 1):
        acct, selected_mode_id = await reserve_account(
            directory,
            spec,
            now_s_override=now_s(),
            exclude_tokens=excluded or None,
        )
        if acct is None:
            raise _no_available_account_error(spec)

        token = acct.token
        success = False
        _retry = False
        _retry_same_account = False
        fail_exc: BaseException | None = None
        adapter = _new_stream_adapter(spec, files, native_tool_names)  # fresh adapter per attempt

        try:
            try:
                async for line in _stream_chat(
                    token=token,
                    mode_id=ModeId(selected_mode_id),
                    message=message,
                    files=files,
                    spec=spec,
                    tool_overrides=tool_overrides,
                    request_overrides=request_overrides,
                    messages=messages if native_tool_names else None,
                    timeout_s=timeout_s,
                ):
                    event_type, data = classify_line(line)
                    if event_type == "done":
                        break
                    if event_type != "data" or not data:
                        continue
                    ended = False
                    for ev in adapter.feed(data):
                        if ev.kind == "soft_stop":
                            ended = True
                            break
                    if ended:
                        break
                success = True

            except UpstreamError as exc:
                fail_exc = exc
                if (
                    _should_retry_same_account_upstream(exc)
                    and attempt < max_retries
                ):
                    _retry = True
                    _retry_same_account = True
                    logger.warning(
                        "chat same-account retry scheduled: attempt={}/{} status={} token={}...",
                        attempt + 1,
                        max_retries,
                        exc.status,
                        token[:8],
                    )
                elif (
                    _is_transient_transport_error(exc)
                    and transport_retry_count < transport_max_retries
                    and attempt < max_retries
                ):
                    transport_retry_count += 1
                    _retry = True
                    _retry_same_account = True
                    logger.warning(
                        "chat transport retry scheduled: transport_attempt={}/{} attempt={}/{} status={} token={}...",
                        transport_retry_count,
                        transport_max_retries,
                        attempt + 1,
                        max_retries,
                        exc.status,
                        token[:8],
                    )
                elif (
                    not _is_transient_transport_error(exc)
                    and _should_retry_upstream(exc, retry_codes)
                    and attempt < max_retries
                ):
                    _retry = True
                    logger.warning(
                        "chat retry scheduled: attempt={}/{} status={} token={}...",
                        attempt + 1,
                        max_retries,
                        exc.status,
                        token[:8],
                    )
                else:
                    logger.warning(
                        "chat upstream failed: attempt={}/{} model={} status={} body={}",
                        attempt + 1,
                        max_retries + 1,
                        model,
                        exc.status,
                        _upstream_body_excerpt(exc),
                    )
                    raise _chat_exhausted_error(
                        model,
                        attempted_accounts=attempt + 1,
                        last_exc=exc,
                    ) from exc

        finally:
            await directory.release(acct)
            kind = (
                FeedbackKind.SUCCESS
                if success
                else _feedback_kind(fail_exc)
                if fail_exc
                else FeedbackKind.SERVER_ERROR
            )
            await directory.feedback(token, kind, selected_mode_id, now_s_val=now_s())
            if success:
                asyncio.create_task(
                    _quota_sync(token, selected_mode_id)
                ).add_done_callback(_log_task_exception)
            else:
                asyncio.create_task(
                    _fail_sync(token, selected_mode_id, fail_exc)
                ).add_done_callback(_log_task_exception)

        if success or not _retry:
            break
        if not _retry_same_account:
            excluded.append(token)

    full_text = "".join(adapter.text_buf)
    extract_images = getattr(adapter, "extract_generated_images_from_text", None)
    if callable(extract_images):
        full_text = extract_images(full_text)
    if adapter.image_urls:
        img_texts = await asyncio.gather(
            *[_resolve_image(token, url, img_id) for url, img_id in adapter.image_urls],
            return_exceptions=True,
        )
        for img_text in img_texts:
            if isinstance(img_text, BaseException):
                logger.warning("chat image resolve failed: error={}", img_text)
            elif isinstance(img_text, str):
                if not img_text:
                    continue
                if full_text:
                    full_text += "\n\n"
                full_text += img_text

    references = adapter.references_suffix()
    if references:
        full_text += references

    thinking_text = ("".join(adapter.thinking_buf) or None) if emit_think else None

    # ── Tool call detection (non-streaming) ──────────────────────────────────
    if native_tool_names and getattr(adapter, "function_calls", None):
        calls = list(adapter.function_calls)
        logger.info(
            "chat request native tool_calls: attempt={}/{} model={} call_count={}",
            attempt + 1,
            max_retries + 1,
            model,
            len(calls),
        )
        pt = estimate_prompt_tokens(message)
        resp = make_tool_call_response(
            model,
            calls,
            prompt_content=message,
            response_id=response_id,
            usage=build_usage(pt, estimate_tool_call_tokens(calls)),
        )
        sources = adapter.search_sources_list()
        if sources:
            resp["search_sources"] = sources
        return resp

    if tool_names:
        parse_result = parse_tool_calls(full_text, tool_names)
        if parse_result.calls:
            logger.info(
                "chat request tool_calls: attempt={}/{} model={} call_count={}",
                attempt + 1,
                max_retries + 1,
                model,
                len(parse_result.calls),
            )
            pt = estimate_prompt_tokens(message)
            resp = make_tool_call_response(
                model,
                parse_result.calls,
                prompt_content=message,
                response_id=response_id,
                usage=build_usage(pt, estimate_tool_call_tokens(parse_result.calls)),
            )
            # 注入结构化搜索信源（tool_calls 场景）
            sources = adapter.search_sources_list()
            if sources:
                resp["search_sources"] = sources
            return resp

    logger.info(
        "chat request completed: attempt={}/{} model={} text_len={} reasoning_len={} image_count={}",
        attempt + 1,
        max_retries + 1,
        model,
        len(full_text),
        len(thinking_text or ""),
        len(adapter.image_urls),
    )

    pt = estimate_prompt_tokens(message)
    ct = estimate_tokens(full_text)
    rt = estimate_tokens(thinking_text) if thinking_text else 0
    chat_anns = _to_chat_annotations(adapter.annotations_list())
    return make_chat_response(
        model,
        full_text,
        prompt_content=message,
        response_id=response_id,
        reasoning_content=thinking_text,
        search_sources=adapter.search_sources_list(),
        annotations=chat_anns or None,
        usage=build_usage(pt, ct + rt, reasoning_tokens=rt),
    )


__all__ = [
    "completions",
    "_configured_retry_codes",
    "_should_retry_upstream",
]
