"""Grok Build CLI OAuth credentials and native Responses transport."""

import asyncio
import codecs
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator

import orjson

from app.platform.config.snapshot import get_config
from app.platform.errors import UpstreamError
from app.platform.logging.logger import logger
from app.dataplane.proxy import get_proxy_runtime
from app.dataplane.proxy.adapters.session import ResettableSession, build_session_kwargs
from app.dataplane.translation import (
    GROK_BUILD_RESPONSES,
    OPENAI_RESPONSES,
    RequestEnvelope,
    ResponseEnvelope,
    get_translation_pipeline,
)
from app.dataplane.translation.transforms.grok_build import (
    _CUSTOM_TOOL_INPUT_KEY as _CUSTOM_TOOL_INPUT_KEY,
    _custom_call_as_function as _custom_call_as_function,
    _custom_output_as_function as _custom_output_as_function,
    _custom_tool_as_function as _custom_tool_as_function,
    _custom_tool_names as _custom_tool_names,
    _decode_custom_arguments as _decode_custom_arguments,
    _function_call_as_custom as _function_call_as_custom,
    _function_tool_for_grok as _function_tool_for_grok,
    _restore_custom_tool_response as _restore_custom_tool_response,
    _restore_custom_tool_stream as _restore_custom_tool_stream,
    _rewrite_custom_tool_event as _rewrite_custom_tool_event,
    _rewrite_sse_frame as _rewrite_sse_frame,
    _tools_for_grok as _tools_for_grok,
    _web_search_tool_for_grok as _web_search_tool_for_grok,
    _message_text as _message_text,
    sanitize_responses_payload as sanitize_responses_payload,
)

_credential_lock = asyncio.Lock()
_credential_cursor = 0


def _response_headers(response: Any) -> dict[str, str]:
    try:
        return {str(key): str(value) for key, value in response.headers.items()}
    except (AttributeError, TypeError):
        return {}


def _response_usage(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    usage = payload.get("usage")
    if isinstance(usage, dict):
        return usage
    response = payload.get("response")
    if isinstance(response, dict) and isinstance(response.get("usage"), dict):
        return response["usage"]
    return {}


def _sse_event_payload(event: str) -> dict[str, Any]:
    data = "\n".join(
        line[5:].strip()
        for line in event.splitlines()
        if line.startswith("data:")
    )
    if not data or data == "[DONE]":
        return {}
    try:
        payload = orjson.loads(data)
    except orjson.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _sse_event_usage(event: str) -> dict[str, Any]:
    return _response_usage(_sse_event_payload(event))


def _sse_event_status(event: str) -> int | None:
    if any(line.strip() == "data: [DONE]" for line in event.splitlines()):
        return 200
    event_type = str(_sse_event_payload(event).get("type") or "")
    if event_type == "response.completed":
        return 200
    if event_type in {"response.failed", "response.error"}:
        return 502
    if event_type == "response.incomplete":
        return 422
    return None


async def _record_build_usage(
    source_id: str | None,
    generation: str,
    response: Any,
    *,
    usage: dict[str, Any] | None = None,
    count_request: bool = True,
    status_code: int | None = None,
) -> None:
    if not source_id:
        return
    from app.maintainer.grok_build_usage import record_usage

    try:
        await asyncio.to_thread(
            record_usage,
            source_id,
            generation=generation or "legacy",
            status_code=int(
                response.status_code if status_code is None else status_code
            ),
            usage=usage,
            headers=_response_headers(response),
            count_request=count_request,
        )
    except Exception as exc:
        logger.debug(
            "Grok Build usage accounting failed: source_id={} error_type={}",
            source_id,
            type(exc).__name__,
        )


def _payload_diagnostics(payload: dict[str, Any]) -> dict[str, Any]:
    tools = payload.get("tools")
    tool_shapes = []
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict):
                tool_shapes.append({"python_type": type(tool).__name__})
                continue
            parameters = tool.get("parameters")
            tool_shapes.append({
                "type": tool.get("type"),
                "name": tool.get("name"),
                "keys": sorted(tool),
                "parameters_bytes": (
                    len(orjson.dumps(parameters)) if parameters is not None else 0
                ),
            })

    input_value = payload.get("input")
    input_shapes = []
    if isinstance(input_value, list):
        for item in input_value:
            if isinstance(item, dict):
                input_shapes.append({
                    "type": item.get("type"),
                    "role": item.get("role"),
                    "keys": sorted(item),
                })
            else:
                input_shapes.append({"python_type": type(item).__name__})

    return {
        "payload_bytes": len(orjson.dumps(payload)),
        "instructions_chars": len(str(payload.get("instructions") or "")),
        "tools": tool_shapes,
        "input": input_shapes,
        "reasoning_keys": sorted(payload.get("reasoning") or {}),
    }


def _auth_path() -> Path:
    from app.maintainer.grok_build_oauth import pool_path

    return pool_path()


def _parse_expiry(value: Any) -> float:
    from app.maintainer.grok_build_oauth import parse_pool_expiry

    return parse_pool_expiry(value)


def _select_entry(
    document: dict[str, Any], preferred_key: str | None = None
) -> tuple[str, dict[str, Any]]:
    global _credential_cursor
    if "key" in document or "access_token" in document:
        return "default", document
    candidates = [
        (key, value)
        for key, value in sorted(document.items())
        if isinstance(value, dict) and (value.get("key") or value.get("access_token"))
    ]
    if preferred_key:
        for key, value in candidates:
            if key == preferred_key:
                return key, value
    if candidates:
        selected = candidates[_credential_cursor % len(candidates)]
        _credential_cursor += 1
        return selected
    raise UpstreamError("No Grok Build OAuth credential found", status=503)


def _load_document(
    preferred_key: str | None = None,
) -> tuple[Path, dict[str, Any], str, dict[str, Any]]:
    from app.maintainer.grok_build_oauth import read_pool_document

    path = _auth_path()
    try:
        document = read_pool_document()
    except FileNotFoundError as exc:
        raise UpstreamError(
            f"Grok Build auth file not found: {path}", status=503
        ) from exc
    except (OSError, ValueError) as exc:
        raise UpstreamError("Invalid Grok Build auth file", status=503) from exc
    if not isinstance(document, dict):
        raise UpstreamError("Invalid Grok Build auth document", status=503)
    entry_key, entry = _select_entry(document, preferred_key)
    return path, document, entry_key, entry


def _credential_count() -> int:
    from app.maintainer.grok_build_oauth import read_pool_document

    try:
        document = read_pool_document()
    except (OSError, ValueError):
        return 1
    if not isinstance(document, dict):
        return 1
    if "key" in document or "access_token" in document:
        return 1
    return max(
        1,
        sum(
            1
            for value in document.values()
            if isinstance(value, dict)
            and (value.get("key") or value.get("access_token"))
        ),
    )


def _save_document(
    path: Path,
    document: dict[str, Any],
    entry_key: str,
    entry: dict[str, Any],
) -> None:
    from app.maintainer.grok_build_oauth import save_pool_entry

    # Merge the refreshed entry into the latest on-disk document instead of
    # replacing it with the stale snapshot loaded before the network request.
    save_pool_entry(entry_key, entry, require_existing=True)


async def _refresh(entry: dict[str, Any]) -> dict[str, Any]:
    refresh_token = str(entry.get("refresh_token") or "").strip()
    if not refresh_token:
        raise UpstreamError("Grok Build OAuth token expired and has no refresh token", status=401)
    cfg = get_config()
    lease = await (await get_proxy_runtime()).acquire()
    async with ResettableSession(**build_session_kwargs(lease=lease)) as session:
        response = await session.post(
            cfg.get_str("grok_build.oauth_token_url", "https://auth.x.ai/oauth2/token"),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            data={
                "grant_type": "refresh_token",
                "client_id": cfg.get_str("grok_build.oauth_client_id"),
                "refresh_token": refresh_token,
            },
            timeout=30.0,
        )
    if response.status_code != 200:
        raise UpstreamError("Grok Build OAuth refresh failed", status=response.status_code)
    payload = orjson.loads(response.content)
    entry["key"] = payload.get("access_token", entry.get("key", ""))
    entry["access_token"] = entry["key"]
    entry["refresh_token"] = payload.get("refresh_token") or refresh_token
    expires_in = int(payload.get("expires_in") or 3600)
    entry["expires_at"] = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() + expires_in,
        tz=timezone.utc,
    ).isoformat().replace("+00:00", "Z")
    return entry


async def _credential(
    *, force_refresh: bool = False, entry_key: str | None = None
) -> tuple[str, str, str]:
    async with _credential_lock:
        path, document, selected_key, entry = _load_document(entry_key)
        expiry = _parse_expiry(entry.get("expires_at"))
        skew = get_config().get_int("grok_build.refresh_skew_seconds", 180)
        now = datetime.now(timezone.utc).timestamp()
        if force_refresh or (expiry and now >= expiry - max(0, skew)):
            entry = await _refresh(entry)
            _save_document(path, document, selected_key, entry)
        token = str(entry.get("key") or entry.get("access_token") or "").strip()
        if not token:
            raise UpstreamError("Grok Build OAuth access token is missing", status=503)
        return token, selected_key, str(entry.get("generation") or "legacy")


async def access_token(*, force_refresh: bool = False) -> str:
    token, _entry_key, _generation = await _credential(force_refresh=force_refresh)
    return token


def _headers(token: str, model: str, stream: bool, conv_id: str = "") -> dict[str, str]:
    cfg = get_config()
    headers = {
        "Authorization": f"Bearer {token}",
        "X-XAI-Token-Auth": cfg.get_str("grok_build.token_auth", "xai-grok-cli"),
        "x-grok-client-version": cfg.get_str("grok_build.client_version", "0.2.93"),
        "x-grok-client-identifier": cfg.get_str("grok_build.client_identifier", "grok-pager"),
        "x-grok-model-override": model,
        "User-Agent": cfg.get_str("grok_build.user_agent"),
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
    }
    if conv_id:
        headers["x-grok-conv-id"] = conv_id
    return headers


async def post_responses(
    payload: dict[str, Any],
    *,
    model: str,
    stream: bool,
    source_format: str = OPENAI_RESPONSES,
) -> dict[str, Any] | AsyncGenerator[str, None]:
    cfg = get_config()
    custom_tool_names = _custom_tool_names(payload)
    translated = await get_translation_pipeline().translate_request(
        source_format,
        GROK_BUILD_RESPONSES,
        RequestEnvelope(
            source_format,
            payload,
            model=model,
            stream=stream,
        ),
    )
    if not isinstance(translated.body, dict):
        raise TypeError("Grok Build request translation must return a dict")
    payload = translated.body
    url = cfg.get_str("grok_build.base_url", "https://cli-chat-proxy.grok.com/v1").rstrip("/") + "/responses"
    conv_id = str(payload.get("prompt_cache_key") or "").strip()
    lease = await (await get_proxy_runtime()).acquire()
    session = ResettableSession(**build_session_kwargs(lease=lease))

    selected_key: str | None = None
    selected_generation = "legacy"
    logical_recorded = False

    async def request(force_refresh: bool = False):
        nonlocal selected_key, selected_generation
        token, selected_key, selected_generation = await _credential(
            force_refresh=force_refresh,
            entry_key=selected_key,
        )
        return await session.post(
            url,
            headers=_headers(token, model, stream, conv_id),
            data=orjson.dumps(payload),
            timeout=cfg.get_float("chat.timeout", 120.0),
            stream=stream,
        )

    try:
        response = None
        attempts = _credential_count()
        for attempt in range(attempts):
            response = await request()
            if response.status_code == 401:
                await _record_build_usage(
                    selected_key, selected_generation, response, count_request=False
                )
                response = await request(force_refresh=True)
            if response.status_code == 200:
                break
            await _record_build_usage(
                selected_key, selected_generation, response, count_request=False
            )
            if response.status_code not in (401, 403, 429) or attempt + 1 >= attempts:
                break
            selected_key = None
        if response is None or response.status_code != 200:
            status = response.status_code if response is not None else 502
            body = (
                response.content.decode("utf-8", "replace")[:400]
                if response is not None
                else ""
            )
            logger.warning(
                "grok build request failed: status={} body={} payload_keys={} diagnostics={}",
                status,
                body,
                sorted(payload),
                _payload_diagnostics(payload),
            )
            if response is not None:
                await _record_build_usage(
                    selected_key, selected_generation, response
                )
                logical_recorded = True
            raise UpstreamError(
                f"Grok Build upstream returned {status}",
                status=status,
                body=body,
            )
        if not stream:
            try:
                result = orjson.loads(response.content)
            except orjson.JSONDecodeError:
                await _record_build_usage(
                    selected_key,
                    selected_generation,
                    response,
                    status_code=502,
                )
                logical_recorded = True
                raise
            await _record_build_usage(
                selected_key,
                selected_generation,
                response,
                usage=_response_usage(result),
            )
            logical_recorded = True
            await session.close()
            translated_response = await get_translation_pipeline().translate_response(
                GROK_BUILD_RESPONSES,
                source_format,
                ResponseEnvelope(
                    GROK_BUILD_RESPONSES,
                    result,
                    model=model,
                    original_request=payload,
                    metadata={"custom_tool_names": custom_tool_names},
                ),
            )
            if not isinstance(translated_response.body, dict):
                raise TypeError("Responses translation must return a dict")
            return translated_response.body
    except Exception:
        if not logical_recorded and selected_key:
            await _record_build_usage(
                selected_key,
                selected_generation,
                response,
                status_code=502,
            )
        await session.close()
        raise

    async def chunks() -> AsyncGenerator[str, None]:
        decoder = codecs.getincrementaldecoder("utf-8")()
        event_buffer = ""
        stream_usage: dict[str, Any] = {}
        terminal_status: int | None = None
        outcome_status = 502
        try:
            async for chunk in response.aiter_content():
                text = decoder.decode(chunk)
                if text:
                    event_buffer = (event_buffer + text).replace("\r\n", "\n")
                    while "\n\n" in event_buffer:
                        event, event_buffer = event_buffer.split("\n\n", 1)
                        stream_usage.update(_sse_event_usage(event))
                        terminal_status = _sse_event_status(event) or terminal_status
                    yield text
            tail = decoder.decode(b"", final=True)
            if tail:
                event_buffer = (event_buffer + tail).replace("\r\n", "\n")
                yield tail
        except asyncio.CancelledError:
            outcome_status = 499
            raise
        except GeneratorExit:
            outcome_status = 499
            raise
        except Exception:
            outcome_status = 502
            raise
        finally:
            if event_buffer:
                stream_usage.update(_sse_event_usage(event_buffer))
                terminal_status = _sse_event_status(event_buffer) or terminal_status
            await _record_build_usage(
                selected_key,
                selected_generation,
                response,
                usage=stream_usage,
                status_code=terminal_status or outcome_status,
            )
            await session.close()

    translated_response = await get_translation_pipeline().translate_response(
        GROK_BUILD_RESPONSES,
        source_format,
        ResponseEnvelope(
            GROK_BUILD_RESPONSES,
            chunks(),
            model=model,
            stream=True,
            original_request=payload,
            metadata={"custom_tool_names": custom_tool_names},
        ),
    )
    return translated_response.body


__all__ = ["access_token", "post_responses"]
