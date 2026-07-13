"""Pure request and response transforms for Grok Build Responses."""

import json
from typing import Any, AsyncGenerator

import orjson

from ..types import TranslationContext

_CUSTOM_TOOL_INPUT_KEY = "input"


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
    return {
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
    if event_type == "response.function_call_arguments.done" and is_custom_call:
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
            if (
                isinstance(response_format, dict)
                and response_format.get("type") == "json_schema"
            ):
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


def translate_grok_build_request(
    body: Any,
    _context: TranslationContext,
) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise TypeError("Grok Build request must be a dict")
    return sanitize_responses_payload(body)


def translate_grok_build_nonstream_response(
    body: Any,
    context: TranslationContext,
) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise TypeError("Grok Build non-stream response must be a dict")
    names = set(context.metadata.get("custom_tool_names") or ())
    return _restore_custom_tool_response(body, names)


async def translate_grok_build_stream_response(
    source: Any,
    context: TranslationContext,
) -> Any:
    names = set(context.metadata.get("custom_tool_names") or ())
    if not names:
        return source
    return _restore_custom_tool_stream(source, names)


__all__ = [
    "_custom_tool_names",
    "_restore_custom_tool_response",
    "_restore_custom_tool_stream",
    "sanitize_responses_payload",
    "translate_grok_build_nonstream_response",
    "translate_grok_build_request",
    "translate_grok_build_stream_response",
]
