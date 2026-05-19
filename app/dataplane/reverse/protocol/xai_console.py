"""xAI Console Responses protocol helpers."""

from typing import Any

import orjson

from app.dataplane.reverse.protocol.xai_chat import FrameEvent
from app.dataplane.reverse.protocol.tool_parser import ParsedToolCall


def build_console_responses_payload(
    *,
    model: str,
    message: str,
    stream: bool = True,
    tools: list[dict] | None = None,
    tool_choice: Any = None,
    request_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a Responses-compatible payload for console.x.ai/v1/responses."""
    payload: dict[str, Any] = {}
    if request_overrides:
        payload.update(
            {
                key: value
                for key, value in request_overrides.items()
                if value is not None and key not in {"model", "input"}
            }
        )
    payload["model"] = model
    payload["input"] = message
    payload["stream"] = stream
    normalized_tools = _normalize_tools(tools)
    if normalized_tools:
        payload["tools"] = normalized_tools
    normalized_choice = _normalize_tool_choice(tool_choice)
    if normalized_choice is not None:
        payload["tool_choice"] = normalized_choice
    return payload


class ConsoleResponsesStreamAdapter:
    """Parse xAI Console Responses SSE JSON into FrameEvent objects."""

    __slots__ = (
        "thinking_buf",
        "text_buf",
        "image_urls",
        "tool_calls",
        "_tool_items",
        "_tool_arg_deltas",
        "_emitted_tool_call_ids",
    )

    def __init__(self) -> None:
        self.thinking_buf: list[str] = []
        self.text_buf: list[str] = []
        self.image_urls: list[tuple[str, str]] = []
        self.tool_calls: list[ParsedToolCall] = []
        self._tool_items: dict[str, dict[str, Any]] = {}
        self._tool_arg_deltas: dict[str, list[str]] = {}
        self._emitted_tool_call_ids: set[str] = set()

    def references_suffix(self) -> str:
        return ""

    def annotations_list(self) -> list[dict]:
        return []

    def search_sources_list(self) -> list[dict] | None:
        return None

    def feed(self, data: str) -> list[FrameEvent]:
        """Parse one Responses SSE ``data:`` JSON payload."""
        try:
            obj = orjson.loads(data)
        except (orjson.JSONDecodeError, ValueError, TypeError):
            return []
        if not isinstance(obj, dict):
            return []

        event_type = str(obj.get("type") or obj.get("event") or "")
        events: list[FrameEvent] = []

        if event_type.endswith("reasoning_summary_text.delta") or event_type.endswith(
            "reasoning_text.delta"
        ):
            delta = _extract_delta(obj)
            if delta:
                self.thinking_buf.append(delta)
                events.append(FrameEvent("thinking", delta))
            return events

        if event_type.endswith("output_text.delta") or event_type.endswith("text.delta"):
            delta = _extract_delta(obj)
            if delta:
                self.text_buf.append(delta)
                events.append(FrameEvent("text", delta))
            return events

        if event_type == "response.output_item.added":
            item = obj.get("item")
            if _is_function_call_item(item):
                self._remember_in_progress_tool_item(obj, item)
            return events

        if event_type == "response.function_call_arguments.delta":
            key = _tool_event_key(obj)
            delta = _extract_delta(obj)
            if key and delta:
                self._tool_arg_deltas.setdefault(key, []).append(delta)
            return events

        if event_type in {
            "response.function_call_arguments.done",
            "response.output_item.done",
        }:
            item = _extract_tool_item(obj, self._tool_items)
            if item is None and event_type == "response.function_call_arguments.done":
                item = obj
            if item is not None:
                key = _tool_event_key(obj) or _tool_item_key(item)
                if key and not item.get("arguments"):
                    item = {
                        **item,
                        "arguments": "".join(self._tool_arg_deltas.get(key, [])),
                    }
                event = self._finalize_tool_call(item)
                if event is not None:
                    events.append(event)
            return events

        if event_type == "response.completed":
            for call in _extract_completed_tool_calls(obj.get("response")):
                event = self._finalize_tool_call(call)
                if event is not None:
                    events.append(event)
            if not self.text_buf:
                for text in _extract_completed_text(obj.get("response")):
                    self.text_buf.append(text)
                    events.append(FrameEvent("text", text))
            if not self.thinking_buf:
                for text in _extract_completed_reasoning(obj.get("response")):
                    self.thinking_buf.append(text)
                    events.append(FrameEvent("thinking", text))
            events.append(FrameEvent("soft_stop"))
            return events

        if event_type in {
            "response.failed",
            "response.cancelled",
            "response.incomplete",
        }:
            events.append(FrameEvent("soft_stop"))
            return events

        return events

    def _remember_in_progress_tool_item(
        self, obj: dict[str, Any], item: Any
    ) -> None:
        if not isinstance(item, dict):
            return
        key = _tool_event_key(obj) or _tool_item_key(item)
        if key:
            self._tool_items[key] = dict(item)

    def _finalize_tool_call(self, item: dict[str, Any]) -> FrameEvent | None:
        call = _to_parsed_tool_call(item)
        if call is None:
            return None
        if call.call_id in self._emitted_tool_call_ids:
            return None
        self._emitted_tool_call_ids.add(call.call_id)
        self.tool_calls.append(call)
        return FrameEvent("tool_call", tool_call=call)


def _normalize_tools(tools: list[dict] | None) -> list[dict]:
    if not tools:
        return []
    normalized: list[dict] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") != "function":
            normalized.append({k: v for k, v in tool.items() if v is not None})
            continue
        fn = tool.get("function")
        if isinstance(fn, dict):
            name = fn.get("name")
            if not name:
                continue
            item = {
                "type": "function",
                "name": name,
                "description": fn.get("description") or "",
                "parameters": fn.get("parameters") or {},
            }
            normalized.append(item)
            continue
        if tool.get("name"):
            normalized.append({k: v for k, v in tool.items() if v is not None})
    return normalized


def _normalize_tool_choice(tool_choice: Any) -> Any:
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        return tool_choice
    if not isinstance(tool_choice, dict):
        return tool_choice
    if tool_choice.get("type") != "function":
        return tool_choice
    fn = tool_choice.get("function")
    if isinstance(fn, dict) and fn.get("name"):
        return {"type": "function", "name": fn["name"]}
    if tool_choice.get("name"):
        return {"type": "function", "name": tool_choice["name"]}
    return tool_choice


def _extract_delta(obj: dict[str, Any]) -> str:
    value = obj.get("delta")
    if value is None:
        value = obj.get("text")
    if value is None:
        value = obj.get("content")
    if isinstance(value, dict):
        value = value.get("text") or value.get("content")
    return str(value) if value is not None else ""


def _tool_event_key(obj: dict[str, Any]) -> str:
    for key in ("item_id", "output_index", "call_id"):
        value = obj.get(key)
        if value is not None:
            return str(value)
    item = obj.get("item")
    return _tool_item_key(item) if isinstance(item, dict) else ""


def _tool_item_key(item: dict[str, Any]) -> str:
    for key in ("id", "call_id"):
        value = item.get(key)
        if value is not None:
            return str(value)
    return ""


def _is_function_call_item(item: Any) -> bool:
    return isinstance(item, dict) and item.get("type") == "function_call"


def _extract_tool_item(
    obj: dict[str, Any], in_progress_items: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    item = obj.get("item")
    if _is_function_call_item(item):
        return dict(item)
    if obj.get("type") == "function_call" or (
        obj.get("name") and (obj.get("arguments") is not None or obj.get("call_id"))
    ):
        return dict(obj)
    key = _tool_event_key(obj)
    if key and key in in_progress_items:
        return dict(in_progress_items[key])
    return None


def _to_parsed_tool_call(item: dict[str, Any]) -> ParsedToolCall | None:
    name = item.get("name")
    if not name:
        return None
    raw_args = item.get("arguments")
    if raw_args is None:
        raw_args = "{}"
    if isinstance(raw_args, str):
        arguments = raw_args or "{}"
    else:
        try:
            arguments = orjson.dumps(raw_args).decode()
        except (TypeError, ValueError):
            arguments = "{}"
    call_id = item.get("call_id") or item.get("id")
    if not call_id:
        return ParsedToolCall.make(str(name), arguments)
    return ParsedToolCall(
        call_id=str(call_id),
        name=str(name),
        arguments=arguments,
    )


def _extract_completed_tool_calls(response: Any) -> list[dict[str, Any]]:
    if not isinstance(response, dict):
        return []
    out: list[dict[str, Any]] = []
    for item in response.get("output", []) or []:
        if _is_function_call_item(item):
            out.append(dict(item))
    return out


def _extract_completed_text(response: Any) -> list[str]:
    if not isinstance(response, dict):
        return []
    out: list[str] = []
    for item in response.get("output", []) or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content", []) or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") in {"output_text", "text"}:
                text = part.get("text") or part.get("content")
                if text:
                    out.append(str(text))
    return out


def _extract_completed_reasoning(response: Any) -> list[str]:
    if not isinstance(response, dict):
        return []
    out: list[str] = []
    for item in response.get("output", []) or []:
        if not isinstance(item, dict) or item.get("type") != "reasoning":
            continue
        for part in item.get("summary", []) or []:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if text:
                out.append(str(text))
    return out


__all__ = ["ConsoleResponsesStreamAdapter", "build_console_responses_payload"]
