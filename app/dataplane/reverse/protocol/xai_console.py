"""xAI Console Responses protocol helpers."""

import hashlib
import re
from typing import Any

import orjson

from app.control.model.spec import ModelSpec
from app.dataplane.reverse.protocol.xai_chat import (
    FrameEvent,
    _split_trailing_incomplete_image_url,
)


_REQUESTED_REASONING_EFFORT_KEY = "_reasoning_effort"


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
    public_model: str | None = None,
    spec: ModelSpec | None = None,
    request_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a Responses-compatible payload for console.x.ai/v1/responses."""
    payload: dict[str, Any] = {}
    if request_overrides:
        payload.update(
            {
                key: value
                for key, value in request_overrides.items()
                if value is not None
                and key
                not in {
                    "model",
                    "input",
                    "reasoning",
                    "reasoning_effort",
                    _REQUESTED_REASONING_EFFORT_KEY,
                }
            }
        )
    requested_effort = _extract_requested_reasoning_effort(request_overrides)
    effort = (
        spec.console_reasoning_effort(requested_effort)
        if spec is not None
        else requested_effort
    )
    if effort:
        payload["reasoning"] = {"effort": effort}

    identity = _identity_instructions(public_model or model)
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


def _extract_requested_reasoning_effort(
    request_overrides: dict[str, Any] | None,
) -> str | None:
    if not request_overrides:
        return None
    if _REQUESTED_REASONING_EFFORT_KEY in request_overrides:
        value = request_overrides.get(_REQUESTED_REASONING_EFFORT_KEY)
        return str(value).strip().lower() if value is not None else None
    if "reasoning_effort" in request_overrides:
        value = request_overrides.get("reasoning_effort")
        return str(value).strip().lower() if value is not None else None

    reasoning = request_overrides.get("reasoning")
    if isinstance(reasoning, dict):
        value = reasoning.get("effort")
        return str(value).strip().lower() if value is not None else None
    return None


class ConsoleResponsesStreamAdapter:
    """Parse xAI Console Responses SSE JSON into FrameEvent objects."""

    __slots__ = (
        "thinking_buf",
        "text_buf",
        "image_urls",
        "_seen_image_urls",
        "_pending_text",
    )

    def __init__(self) -> None:
        self.thinking_buf: list[str] = []
        self.text_buf: list[str] = []
        self.image_urls: list[tuple[str, str]] = []
        self._seen_image_urls: set[str] = set()
        self._pending_text = ""

    def references_suffix(self) -> str:
        return ""

    def annotations_list(self) -> list[dict]:
        return []

    def search_sources_list(self) -> list[dict] | None:
        return None

    def _append_text(self, events: list[FrameEvent], text: str) -> None:
        combined = self._pending_text + text
        self._pending_text = ""
        cleaned, image_urls = _strip_image_candidates(combined)
        cleaned, self._pending_text = _split_trailing_incomplete_image_url(cleaned)
        for url in image_urls:
            self._append_response_images(events, {"type": "output_image", "url": url})
        if cleaned:
            self.text_buf.append(cleaned)
            events.append(FrameEvent("text", cleaned))

    def extract_generated_images_from_text(self, text: str) -> str:
        """Extract generated image URLs after all text chunks have been joined."""
        cleaned, image_urls = _strip_image_candidates(text)
        events: list[FrameEvent] = []
        for url in image_urls:
            self._append_response_images(events, {"type": "output_image", "url": url})
        return cleaned

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

        if event_type.endswith("output_item.done") or event_type.endswith("output_item.added"):
            self._append_response_images(events, obj.get("item") or obj.get("output_item"))
            if events:
                return events

        if "image" in event_type:
            self._append_response_images(events, obj)
            if events:
                return events

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
                self._append_text(events, delta)
            return events

        if event_type == "response.completed":
            if not self.text_buf:
                for text in _extract_completed_text(obj.get("response")):
                    self._append_text(events, text)
            if not self.thinking_buf:
                for text in _extract_completed_reasoning(obj.get("response")):
                    self.thinking_buf.append(text)
                    events.append(FrameEvent("thinking", text))
            self._append_response_images(events, obj.get("response"))
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

    def _append_response_images(self, events: list[FrameEvent], obj: Any) -> None:
        for url, image_id in _iter_response_images(obj):
            normalized = _normalize_image_url(url)
            if not normalized or normalized in self._seen_image_urls:
                continue
            resolved_id = image_id or _image_id_from_url(normalized)
            self._seen_image_urls.add(normalized)
            self.image_urls.append((normalized, resolved_id))
            events.append(FrameEvent("image", normalized, resolved_id))


def _extract_delta(obj: dict[str, Any]) -> str:
    value = obj.get("delta")
    if value is None:
        value = obj.get("text")
    if value is None:
        value = obj.get("content")
    if isinstance(value, dict):
        value = value.get("text") or value.get("content")
    return str(value) if value is not None else ""


_ASSETS_BASE = "https://assets.grok.com/"
_IMAGE_URL_KEYS = {
    "url",
    "image_url",
    "imageUrl",
    "asset_url",
    "assetUrl",
    "content_url",
    "contentUrl",
    "cdn_url",
    "cdnUrl",
    "download_url",
    "downloadUrl",
    "media_url",
    "mediaUrl",
    "src",
}
_IMAGE_ID_KEYS = {
    "id",
    "item_id",
    "itemId",
    "image_id",
    "imageId",
    "asset_id",
    "assetId",
    "file_id",
    "fileId",
}
_IMAGE_TYPES = {"image_generation_call", "output_image", "image"}
_IMAGE_ID_FROM_URL_RE = re.compile(r"(?:/images/|/)([0-9a-fA-F\-]{16,36})(?:[./]|$)")
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/=\s]+$")
_MARKDOWN_IMAGE_RE = re.compile(
    r"!\[[^\]]*\]\(\s*(?P<url><[^>]+>|[^)\s]+)(?:\s+['\"][^'\"]*['\"])?\s*\)"
)
_BARE_IMAGE_URL_RE = re.compile(r"(?P<url>https?://[^\s<>\]\)\"']+)")
_COMPLETE_IMAGE_URL_RE = re.compile(
    r"\.(?:png|jpe?g|gif|webp|bmp)(?:[?#][^\s<>\]\)\"']*)?$",
    re.IGNORECASE,
)


def _normalize_image_url(value: str) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    lowered = url.lower()
    if lowered.startswith(("http://", "https://", "data:image/")):
        return url
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("/"):
        return f"{_ASSETS_BASE.rstrip('/')}{url}"
    return f"{_ASSETS_BASE}{url.lstrip('/')}"


def _image_id_from_url(url: str) -> str:
    match = _IMAGE_ID_FROM_URL_RE.search(url or "")
    if match:
        return match.group(1).lower()
    return hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:32]


def _looks_like_image_url(value: str, *, trust_url: bool = False) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    lowered = raw.lower()
    if lowered.startswith("data:image/"):
        return True
    if trust_url and (
        lowered.startswith(("http://", "https://", "//", "/"))
        or lowered.startswith(("images/", "image/", "content/"))
    ):
        return True
    normalized = _normalize_image_url(raw).lower()
    return (
        "assets.grok.com" in normalized
        or "grok.x.ai" in normalized
        or "imgen.x.ai" in normalized
        or "imagine-public.x.ai" in normalized
        or "/images/" in normalized
        or "/content" in normalized
        or "image" in normalized
        or normalized.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))
    )


def _result_to_image_url(value: str, *, trust_url: bool = False) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    compact = "".join(raw.split())
    if len(compact) > 80 and _BASE64_RE.fullmatch(compact):
        return f"data:image/png;base64,{compact}"
    if _looks_like_image_url(raw, trust_url=trust_url):
        return raw
    return ""


def _looks_like_complete_image_url(value: str) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    lowered = raw.lower()
    return (
        lowered.startswith("data:image/")
        or bool(_COMPLETE_IMAGE_URL_RE.search(raw))
        or lowered.endswith("/content")
        or "/content?" in lowered
    )


def _key_indicates_image_url(key: str) -> bool:
    lowered = str(key or "").lower()
    return (
        key in _IMAGE_URL_KEYS
        or lowered in _IMAGE_URL_KEYS
        or lowered.endswith("imageurl")
        or lowered.endswith("image_url")
        or (
            ("image" in lowered or "asset" in lowered or "media" in lowered)
            and ("url" in lowered or lowered in {"src", "href"})
        )
    )


def _strip_image_candidates(text: str) -> tuple[str, list[str]]:
    urls: list[str] = []

    def _append_url(raw_url: str) -> bool:
        url = _result_to_image_url(raw_url, trust_url=False)
        if not url:
            return False
        urls.append(url)
        return True

    def _replace(match: re.Match) -> str:
        raw_url = match.group("url").strip().strip("<>")
        if not _append_url(raw_url):
            return match.group(0)
        return ""

    def _replace_bare(match: re.Match) -> str:
        raw_url = match.group("url").strip()
        image_url = raw_url.rstrip(".,;:!?")
        suffix = raw_url[len(image_url):]
        if not _looks_like_complete_image_url(image_url):
            return match.group(0)
        if not _append_url(image_url):
            return match.group(0)
        return suffix

    cleaned = _MARKDOWN_IMAGE_RE.sub(_replace, text)
    cleaned = _BARE_IMAGE_URL_RE.sub(_replace_bare, cleaned)
    return cleaned, urls


def _iter_response_images(obj: Any, *, in_image_scope: bool = False, inherited_id: str = ""):
    if isinstance(obj, dict):
        obj_type = str(obj.get("type") or "").strip()
        image_scope = in_image_scope or obj_type in _IMAGE_TYPES or "image" in obj_type
        local_id = inherited_id
        for key in _IMAGE_ID_KEYS:
            if key == "id" and not image_scope:
                continue
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                local_id = value.strip()
                break

        result = obj.get("result")
        if image_scope and isinstance(result, str):
            url = _result_to_image_url(result, trust_url=True)
            if url:
                yield url, local_id

        for key, value in obj.items():
            if image_scope and _key_indicates_image_url(key) and isinstance(value, str):
                url = _result_to_image_url(value, trust_url=True)
                if url:
                    yield url, local_id
            elif isinstance(value, (dict, list)):
                yield from _iter_response_images(
                    value,
                    in_image_scope=image_scope,
                    inherited_id=local_id,
                )
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_response_images(
                item,
                in_image_scope=in_image_scope,
                inherited_id=inherited_id,
            )


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
