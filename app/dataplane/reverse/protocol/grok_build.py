"""Grok Build CLI OAuth credentials and native Responses transport."""

import asyncio
import codecs
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator

import orjson

from app.platform.config.snapshot import get_config
from app.platform.errors import UpstreamError
from app.platform.logging.logger import logger
from app.dataplane.proxy import get_proxy_runtime
from app.dataplane.proxy.adapters.session import ResettableSession, build_session_kwargs

_credential_lock = asyncio.Lock()
_credential_cursor = 0
_CUSTOM_TOOL_INPUT_KEY = "input"


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


def _custom_tool_names(payload: dict[str, Any]) -> set[str]:
    names = {
        str(tool.get("name") or "")
        for tool in payload.get("tools") or []
        if isinstance(tool, dict) and tool.get("type") == "custom"
    }
    for item in payload.get("input") or []:
        if isinstance(item, dict) and item.get("type") == "custom_tool_call":
            names.add(str(item.get("name") or ""))
    names.discard("")
    return names


def _custom_tool_as_function(tool: dict[str, Any]) -> dict[str, Any]:
    description = str(tool.get("description") or "")
    format_config = tool.get("format")
    if isinstance(format_config, dict) and format_config.get("type") == "grammar":
        syntax = str(format_config.get("syntax") or "grammar")
        definition = str(format_config.get("definition") or "")
        if definition:
            description = (
                f"{description}\n\nRaw input must satisfy this {syntax} grammar:\n"
                f"{definition}"
            ).strip()
    converted = {
        "type": "function",
        "name": str(tool.get("name") or ""),
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {
                _CUSTOM_TOOL_INPUT_KEY: {
                    "type": "string",
                    "description": "Complete raw input for this custom tool.",
                }
            },
            "required": [_CUSTOM_TOOL_INPUT_KEY],
            "additionalProperties": False,
        },
        "strict": True,
    }
    return converted


def _function_tool_for_grok(tool: dict[str, Any]) -> dict[str, Any]:
    """Strip Codex-only function extensions rejected by Grok Build."""
    converted = dict(tool)
    converted.pop("defer_loading", None)
    return converted


def _web_search_tool_for_grok(tool: dict[str, Any]) -> dict[str, Any]:
    """Remove Codex web-search switches absent from Grok Build's schema."""
    converted = dict(tool)
    converted.pop("external_web_access", None)
    converted.pop("indexed_web_access", None)
    converted.pop("search_content_types", None)
    return converted


def _tools_for_grok(tools: list[Any]) -> list[Any]:
    """Translate Codex Responses tools into Grok Build's supported subset."""
    converted: list[Any] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        tool_type = str(tool.get("type") or "")
        if tool_type == "custom":
            converted.append(_custom_tool_as_function(tool))
        elif tool_type == "function":
            converted.append(_function_tool_for_grok(tool))
        elif tool_type == "namespace":
            for nested in tool.get("tools") or []:
                if isinstance(nested, dict) and nested.get("type") == "function":
                    converted.append(_function_tool_for_grok(nested))
        elif tool_type == "tool_search":
            # Namespace tools are eagerly expanded above, so dynamic lookup is
            # unnecessary and unsupported by the Grok Build schema.
            continue
        elif tool_type == "web_search":
            converted.append(_web_search_tool_for_grok(tool))
        else:
            converted.append(tool)
    return converted


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


def _custom_call_as_function(item: dict[str, Any]) -> dict[str, Any]:
    converted = dict(item)
    converted["type"] = "function_call"
    converted["arguments"] = json.dumps(
        {_CUSTOM_TOOL_INPUT_KEY: item.get("input", "")},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    converted.pop("input", None)
    return converted


def _custom_output_as_function(item: dict[str, Any]) -> dict[str, Any]:
    converted = dict(item)
    converted["type"] = "function_call_output"
    return converted


def _decode_custom_arguments(arguments: Any) -> str:
    if isinstance(arguments, str):
        try:
            decoded = json.loads(arguments)
        except json.JSONDecodeError:
            return arguments
    else:
        decoded = arguments
    if isinstance(decoded, dict) and _CUSTOM_TOOL_INPUT_KEY in decoded:
        value = decoded[_CUSTOM_TOOL_INPUT_KEY]
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)


def _function_call_as_custom(item: dict[str, Any], names: set[str]) -> dict[str, Any]:
    if item.get("type") != "function_call" or item.get("name") not in names:
        return item
    converted = dict(item)
    converted["type"] = "custom_tool_call"
    converted["input"] = _decode_custom_arguments(item.get("arguments", ""))
    converted.pop("arguments", None)
    return converted


def _restore_custom_tool_response(
    payload: dict[str, Any], names: set[str]
) -> dict[str, Any]:
    if not names:
        return payload
    restored = dict(payload)
    output = restored.get("output")
    if isinstance(output, list):
        restored["output"] = [
            _function_call_as_custom(item, names) if isinstance(item, dict) else item
            for item in output
        ]
    return restored


def _rewrite_custom_tool_event(
    payload: dict[str, Any],
    names: set[str],
    custom_item_ids: set[str],
    custom_call_ids: set[str],
) -> list[dict[str, Any]]:
    event_type = str(payload.get("type") or "")
    rewritten = dict(payload)

    item = rewritten.get("item")
    if isinstance(item, dict):
        converted = _function_call_as_custom(item, names)
        if converted is not item:
            item_id = str(item.get("id") or "")
            call_id = str(item.get("call_id") or "")
            if item_id:
                custom_item_ids.add(item_id)
            if call_id:
                custom_call_ids.add(call_id)
            rewritten["item"] = converted

    response = rewritten.get("response")
    if isinstance(response, dict):
        rewritten["response"] = _restore_custom_tool_response(response, names)

    is_custom_call = (
        str(rewritten.get("item_id") or "") in custom_item_ids
        or str(rewritten.get("call_id") or "") in custom_call_ids
        or str(rewritten.get("name") or "") in names
    )
    if event_type == "response.function_call_arguments.delta" and is_custom_call:
        rewritten["type"] = "response.custom_tool_call_input.delta"
        rewritten["delta"] = ""
        return [rewritten]
    elif event_type == "response.function_call_arguments.done" and is_custom_call:
        custom_input = _decode_custom_arguments(rewritten.pop("arguments", ""))
        rewritten.pop("name", None)
        rewritten["type"] = "response.custom_tool_call_input.done"
        rewritten["input"] = custom_input
        return [rewritten]
    return [rewritten]


def _rewrite_sse_frame(
    frame: str,
    names: set[str],
    custom_item_ids: set[str],
    custom_call_ids: set[str],
) -> str:
    lines = frame.split("\n")
    event_name = ""
    data_lines: list[str] = []
    passthrough: list[str] = []
    for line in lines:
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        else:
            passthrough.append(line)
    if not data_lines:
        return frame + "\n\n"
    data = "\n".join(data_lines)
    if data == "[DONE]":
        return frame + "\n\n"
    try:
        payload = orjson.loads(data)
    except orjson.JSONDecodeError:
        return frame + "\n\n"
    if not isinstance(payload, dict):
        return frame + "\n\n"

    rewritten_events = _rewrite_custom_tool_event(
        payload,
        names,
        custom_item_ids,
        custom_call_ids,
    )
    output_frames: list[str] = []
    for rewritten in rewritten_events:
        rewritten_type = str(rewritten.get("type") or event_name)
        output: list[str] = []
        if event_name:
            output.append(f"event: {rewritten_type}")
        output.extend(line for line in passthrough if line)
        output.append(f"data: {orjson.dumps(rewritten).decode()}")
        output_frames.append("\n".join(output) + "\n\n")
    return "".join(output_frames)


async def _restore_custom_tool_stream(
    source: AsyncGenerator[str, None], names: set[str]
) -> AsyncGenerator[str, None]:
    buffer = ""
    custom_item_ids: set[str] = set()
    custom_call_ids: set[str] = set()
    try:
        async for chunk in source:
            buffer += chunk.replace("\r\n", "\n")
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                yield _rewrite_sse_frame(
                    frame,
                    names,
                    custom_item_ids,
                    custom_call_ids,
                )
        if buffer:
            if "data:" in buffer:
                yield _rewrite_sse_frame(
                    buffer, names, custom_item_ids, custom_call_ids
                )
            else:
                yield buffer
    finally:
        await source.aclose()


def _message_text(item: dict[str, Any]) -> str:
    content = item.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") in ("input_text", "output_text", "text"):
            text = str(part.get("text") or "").strip()
            if text:
                parts.append(text)
    return "\n".join(parts)


def sanitize_responses_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize OpenAI/Codex Responses fields accepted by Grok Build."""
    cleaned = dict(payload)
    instruction_parts: list[str] = []
    existing = str(cleaned.get("instructions") or "").strip()
    if existing:
        instruction_parts.append(existing)

    input_value = cleaned.get("input")
    if input_value is None and "messages" in cleaned:
        input_value = cleaned.pop("messages")
        cleaned["input"] = input_value
    if isinstance(input_value, list):
        rewritten: list[Any] = []
        for item in input_value:
            if not isinstance(item, dict):
                rewritten.append(item)
                continue
            item = dict(item)
            item.pop("internal_chat_message_metadata_passthrough", None)
            if item.get("type") == "reasoning":
                # Codex may replay null or foreign encrypted reasoning blocks.
                # Tool continuation only requires the call and output items.
                continue
            role = str(item.get("role") or item.get("type") or "").lower()
            if role in ("system", "developer"):
                text = _message_text(item)
                if text:
                    instruction_parts.append(text)
                continue
            if item.get("type") == "custom_tool_call":
                rewritten.append(_custom_call_as_function(item))
            elif item.get("type") == "custom_tool_call_output":
                rewritten.append(_custom_output_as_function(item))
            else:
                rewritten.append(item)
        cleaned["input"] = rewritten
    if instruction_parts:
        cleaned["instructions"] = "\n\n".join(instruction_parts)

    response_format = cleaned.pop("response_format", None)
    if response_format is not None:
        text_config = cleaned.get("text")
        if not isinstance(text_config, dict):
            text_config = {}
        if "format" not in text_config:
            if isinstance(response_format, dict) and response_format.get("type") == "json_schema":
                schema = response_format.get("json_schema")
                if isinstance(schema, dict):
                    text_config["format"] = {
                        "type": "json_schema",
                        **{
                            key: schema[key]
                            for key in ("name", "description", "schema", "strict")
                            if key in schema
                        },
                    }
                else:
                    text_config["format"] = response_format
            else:
                text_config["format"] = (
                    {"type": response_format}
                    if isinstance(response_format, str)
                    else response_format
                )
        cleaned["text"] = text_config

    if "max_output_tokens" not in cleaned and "max_tokens" in cleaned:
        cleaned["max_output_tokens"] = cleaned["max_tokens"]
    cleaned.pop("max_tokens", None)

    flat_effort = cleaned.pop("reasoning_effort", None)
    if flat_effort is not None:
        reasoning = cleaned.get("reasoning")
        if not isinstance(reasoning, dict):
            reasoning = {}
        reasoning.setdefault("effort", flat_effort)
        cleaned["reasoning"] = reasoning
    reasoning = cleaned.get("reasoning")
    if isinstance(reasoning, dict) and isinstance(reasoning.get("effort"), str):
        effort = reasoning["effort"].strip().lower()
        reasoning["effort"] = "low" if effort == "minimal" else effort

    tools = cleaned.get("tools")
    if isinstance(tools, list):
        cleaned["tools"] = _tools_for_grok(tools)
    tool_choice = cleaned.get("tool_choice")
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "custom":
        cleaned["tool_choice"] = {**tool_choice, "type": "function"}

    if not str(cleaned.get("prompt_cache_key") or "").strip():
        prompt_cache_id = str(cleaned.pop("prompt_cache_id", "") or "").strip()
        if prompt_cache_id:
            cleaned["prompt_cache_key"] = prompt_cache_id
    else:
        cleaned.pop("prompt_cache_id", None)
    return cleaned


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
    payload: dict[str, Any], *, model: str, stream: bool
) -> dict[str, Any] | AsyncGenerator[str, None]:
    cfg = get_config()
    custom_tool_names = _custom_tool_names(payload)
    payload = sanitize_responses_payload(payload)
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
            return _restore_custom_tool_response(result, custom_tool_names)
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

    stream_chunks = chunks()
    if custom_tool_names:
        return _restore_custom_tool_stream(stream_chunks, custom_tool_names)
    return stream_chunks


__all__ = ["access_token", "post_responses"]
