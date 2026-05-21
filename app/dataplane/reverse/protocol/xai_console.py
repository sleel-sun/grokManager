"""xAI Console Responses protocol helpers."""

from typing import Any

import orjson

from app.dataplane.reverse.protocol.xai_chat import FrameEvent


def _display_model_name(model: str) -> str:
    return model.replace("grok-", "Grok ", 1)


def _identity_instructions(model: str) -> str:
    display = _display_model_name(model)
    return (
        f"You are {display}. The selected public model id for this conversation is "
        f"{model}. If the user asks what model you are, answer {display}. "
        "Do not identify yourself as Grok 1.5 or any other legacy model."
    )


def build_console_responses_payload(
    *,
    model: str,
    message: str,
    stream: bool = True,
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
    identity = _identity_instructions(model)
    existing_instructions = str(payload.get("instructions") or "").strip()
    payload["instructions"] = (
        f"{existing_instructions}\n\n{identity}"
        if existing_instructions
        else identity
    )
    payload["model"] = model
    payload["input"] = message
    payload["stream"] = stream
    return payload


class ConsoleResponsesStreamAdapter:
    """Parse xAI Console Responses SSE JSON into FrameEvent objects."""

    __slots__ = ("thinking_buf", "text_buf", "image_urls")

    def __init__(self) -> None:
        self.thinking_buf: list[str] = []
        self.text_buf: list[str] = []
        self.image_urls: list[tuple[str, str]] = []

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

        if event_type == "response.completed":
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


def _extract_delta(obj: dict[str, Any]) -> str:
    value = obj.get("delta")
    if value is None:
        value = obj.get("text")
    if value is None:
        value = obj.get("content")
    if isinstance(value, dict):
        value = value.get("text") or value.get("content")
    return str(value) if value is not None else ""


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
