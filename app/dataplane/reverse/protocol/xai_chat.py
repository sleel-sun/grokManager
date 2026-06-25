"""XAI app-chat protocol — payload builder and SSE stream adapter."""

import hashlib
import re
from dataclasses import dataclass
from typing import Any

import orjson

from app.platform.logging.logger import logger
from app.platform.config.snapshot import get_config
from app.control.model.enums import ModeId
from app.dataplane.reverse.protocol.xai_chat_reasoning import ReasoningAggregator


def build_chat_payload(
    *,
    message:               str,
    mode_id:               ModeId,
    file_attachments:      list[str]        = (),
    tool_overrides:        dict[str, Any]   | None = None,
    model_config_override: dict[str, Any]   | None = None,
    request_overrides:     dict[str, Any]   | None = None,
) -> dict[str, Any]:
    """Build the JSON payload for POST /rest/app-chat/conversations/new."""
    cfg = get_config()

    payload: dict[str, Any] = {
        "collectionIds":               [],
        "connectors":                  [],
        "deviceEnvInfo": {
            "darkModeEnabled":  False,
            "devicePixelRatio": 2,
            "screenHeight":     1329,
            "screenWidth":      2056,
            "viewportHeight":   1083,
            "viewportWidth":    2056,
        },
        "disableMemory":               not cfg.get_bool("features.memory", False),
        "disableSearch":               False,
        "disableSelfHarmShortCircuit": False,
        "disableTextFollowUps":        False,
        "enableImageGeneration":       True,
        "enableImageStreaming":        True,
        "enableSideBySide":            True,
        "fileAttachments":             list(file_attachments),
        "forceConcise":                False,
        "forceSideBySide":             False,
        "imageAttachments":            [],
        "imageGenerationCount":        2,
        "isAsyncChat":                 False,
        "message":                     message,
        "modeId":                      mode_id.to_api_str(),
        "responseMetadata":            {},
        "returnImageBytes":            False,
        "returnRawGrokInXaiRequest":   False,
        "searchAllConnectors":         False,
        "sendFinalMetadata":           True,
        "temporary":                   cfg.get_bool("features.temporary", True),
        "toolOverrides": tool_overrides or {
            "gmailSearch":           False,
            "googleCalendarSearch":  False,
            "outlookSearch":         False,
            "outlookCalendarSearch": False,
            "googleDriveSearch":     False,
        },
    }

    custom = cfg.get_str("features.custom_instruction", "").strip()
    if custom:
        payload["customPersonality"] = custom

    if model_config_override:
        payload["responseMetadata"]["modelConfigOverride"] = model_config_override

    if request_overrides:
        payload.update({k: v for k, v in request_overrides.items() if v is not None})

    logger.debug(
        "chat payload built: mode={} message_len={} file_count={}",
        mode_id.to_api_str(), len(message), len(file_attachments),
    )
    return payload


# ---------------------------------------------------------------------------
# SSE line classification (unchanged)
# ---------------------------------------------------------------------------


def classify_line(line: str | bytes) -> tuple[str, str]:
    """Return (event_type, data) for a raw SSE line.

    event_type: 'data' | 'done' | 'skip'

    Handles both standard SSE ``data: {...}`` lines and raw JSON lines
    (upstream sometimes omits the ``data:`` prefix).
    """
    if isinstance(line, bytes):
        line = line.decode("utf-8", "replace")
    line = line.strip()
    if not line:
        return "skip", ""
    if line.startswith("data:"):
        data = line[5:].strip()
        if data == "[DONE]":
            return "done", ""
        return "data", data
    if line.startswith("event:"):
        return "skip", ""
    # Raw JSON line (no "data:" prefix) — treat as data.
    if line.startswith("{"):
        return "data", line
    return "skip", ""


# ---------------------------------------------------------------------------
# FrameEvent — single output event from StreamAdapter.feed()
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class FrameEvent:
    """One parsed event produced by StreamAdapter."""

    kind: str
    """Event kind:
    - ``text``      — cleaned final text token  (content = token string)
    - ``thinking``  — Grok main-model thinking   (content = raw token)
    - ``image``     — generated image final URL   (content = full URL, image_id = upstream UUID)
    - ``image_progress`` — generated image progress (content = percent string, image_id = upstream UUID)
    - ``annotation`` — url citation annotation   (annotation_data = annotation dict)
    - ``soft_stop`` — stream end signal
    - ``skip``      — filtered frame, do nothing
    """
    content: str = ""
    image_id: str = ""
    rollout_id: str = ""
    message_tag: str = ""
    message_step_id: int | None = None
    annotation_data: dict | None = None
    tool_calls: list[Any] | None = None


# ---------------------------------------------------------------------------
# StreamAdapter — stateful SSE frame parser
# ---------------------------------------------------------------------------

_GROK_RENDER_RE = re.compile(
    r'<grok:render\s+card_id="([^"]+)"\s+card_type="([^"]+)"\s+type="([^"]+)"'
    r'[^>]*>.*?</grok:render>',
    re.DOTALL,
)

_IMAGE_BASE = "https://assets.grok.com/"
_IMAGE_URL_KEYS = {
    "imageUrl",
    "image_url",
    "generatedImageUrl",
    "generated_image_url",
    "assetUrl",
    "asset_url",
    "contentUrl",
    "content_url",
    "cdnUrl",
    "cdn_url",
    "downloadUrl",
    "download_url",
    "mediaUrl",
    "media_url",
    "src",
    "url",
}
_IMAGE_ID_KEYS = {
    "imageUuid",
    "image_uuid",
    "imageId",
    "image_id",
    "assetId",
    "asset_id",
    "itemId",
    "item_id",
    "id",
}
_IMAGE_ID_FROM_URL_RE = re.compile(r"(?:/images/|/)([0-9a-fA-F\-]{16,36})(?:[./]|$)")
_MARKDOWN_IMAGE_RE = re.compile(
    r"!\[[^\]]*\]\(\s*(?P<url><[^>]+>|[^)\s]+)(?:\s+['\"][^'\"]*['\"])?\s*\)"
)
_BARE_IMAGE_URL_RE = re.compile(r"(?P<url>https?://[^\s<>\]\)\"']+)")
_TRAILING_URL_RE = re.compile(r"https?://[^\s<>\]\)\"']*$")
_MARKDOWN_IMAGE_OPENER_RE = re.compile(r"!\[[^\]]*\]\($")
_COMPLETE_IMAGE_URL_RE = re.compile(
    r"\.(?:png|jpe?g|gif|webp|bmp)(?:[?#][^\s<>\]\)\"']*)?$",
    re.IGNORECASE,
)
_IMAGE_HOST_MARKERS = (
    "assets.grok.com",
    "grok.x.ai",
    "imgen.x.ai",
    "imagine-public.x.ai",
)

# 工具使用卡片 → emoji 单行格式化映射（详细模式专用）
# 格式: tool_name → (emoji, (可展示的参数 key 列表))
_TOOL_FMT: dict[str, tuple[str, tuple[str, ...]]] = {
    "web_search":          ("🔍", ("query", "q")),
    "x_search":            ("🔍", ("query",)),
    "x_keyword_search":    ("🔍", ("query",)),
    "x_semantic_search":   ("🔍", ("query",)),
    "browse_page":         ("🌐", ("url",)),
    "search_images":       ("🖼️", ("image_description", "imageDescription")),
    "image_search":        ("🖼️", ("image_description", "imageDescription")),
    "chatroom_send":       ("📋", ("message",)),
    "code_execution":      ("💻", ()),
}


class StreamAdapter:
    """Parse upstream SSE frames and emit :class:`FrameEvent` objects.

    One instance per HTTP request.  Call :meth:`feed` for every ``data:``
    line; iterate over the returned list of events.
    """

    __slots__ = (
        "_card_cache",
        "_citation_order",
        "_citation_map",
        "_last_citation_index",
        "_pending_citations",
        "_annotations",
        "_text_offset",
        "_emitted_reasoning_keys",
        "_reasoning",
        "_summary_mode",
        "_last_rollout",
        "_content_started",
        "_web_search_results",
        "_web_search_urls_seen",
        "_seen_image_urls",
        "_pending_text",
        "thinking_buf",
        "text_buf",
        "image_urls",
    )

    def __init__(self) -> None:
        self._card_cache: dict[str, dict] = {}
        self._citation_order: list[str] = []
        self._citation_map: dict[str, int] = {}
        self._last_citation_index: int = -1
        self._pending_citations: list[dict] = []       # _render_replace 产出的待定位引用
        self._annotations: list[dict] = []             # 已定位的完整 annotations（绝对位置）
        self._text_offset: int = 0                     # 累计文本长度（仅 text 事件）
        self._emitted_reasoning_keys: set[str] = set()
        # 思维链模式：精简摘要 / 详细原始流
        self._summary_mode: bool = get_config().get_bool("features.thinking_summary", False)
        self._last_rollout: str = ""
        self._content_started: bool = False
        self._reasoning = ReasoningAggregator() if self._summary_mode else None
        self._web_search_results: list[dict] = []
        self._web_search_urls_seen: set[str] = set()
        self._seen_image_urls: set[str] = set()
        self._pending_text = ""
        self.thinking_buf: list[str] = []
        self.text_buf: list[str] = []
        self.image_urls: list[tuple[str, str]] = []   # [(url, imageUuid), ...]

    # 搜索信源追加：当配置启用且有 webSearchResults 时，格式化为 ## Sources 段落
    # 标记行 [grok2api-sources]: # 是 markdown link reference definition，渲染器不显示，
    # 用于 _extract_message() 在多轮对话中精确识别并剥离前轮的 Sources 段落
    def references_suffix(self) -> str:
        """当有搜索信源且配置启用时，格式化为 ## Sources markdown 段落。"""
        if not self._web_search_results:
            return ""
        if not get_config().get_bool("features.show_search_sources", False):
            return ""
        lines = ["\n\n## Sources", "[grok2api-sources]: #"]
        for item in self._web_search_results:
            title = item.get("title") or item.get("url", "")
            # 转义 Markdown 链接文本中的特殊字符，防止 []\ 打坏语法
            title = title.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
            lines.append(f"- [{title}]({item['url']})")
        return "\n".join(lines) + "\n"

    # 内联引用 annotations：生成时同步构建，含绝对位置
    def annotations_list(self) -> list[dict]:
        """已收集的 url_citation annotations（扁平格式，绝对位置）。无引用时返回 []。"""
        return list(self._annotations)

    # 结构化搜索信源：始终输出（不受配置开关控制），供 search_sources 字段使用
    def search_sources_list(self) -> list[dict] | None:
        """当有搜索信源时，返回结构化列表；无则返回 None。"""
        if not self._web_search_results:
            return None
        return [
            {
                "url": item["url"],
                "title": item.get("title") or item.get("url", ""),
                "type": item.get("type", "web"),
            }
            for item in self._web_search_results
        ]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def feed(self, data: str) -> list[FrameEvent]:
        """Parse one JSON ``data:`` payload; return 0-N events."""
        try:
            obj = orjson.loads(data)
        except (orjson.JSONDecodeError, ValueError, TypeError):
            return []

        result = obj.get("result")
        if not result:
            return []
        resp = result.get("response")
        if not resp:
            return []

        events: list[FrameEvent] = []

        # ── cache every cardAttachment first ──────────────────────
        card_raw = resp.get("cardAttachment")
        if card_raw:
            events.extend(self._handle_card(card_raw))

        # ── 采集 webSearchResults（搜索信源，多帧累积去重）───────
        wsr = resp.get("webSearchResults")
        if wsr and isinstance(wsr, dict):
            for item in wsr.get("results", []):
                if isinstance(item, dict) and item.get("url"):
                    url = item["url"]
                    if url not in self._web_search_urls_seen:
                        self._web_search_urls_seen.add(url)
                        self._web_search_results.append({**item, "type": "web"})

        # ── 采集 xSearchResults（X/Twitter 帖子信源，多帧累积去重）──
        xsr = resp.get("xSearchResults")
        if xsr and isinstance(xsr, dict):
            for item in xsr.get("results", []):
                if isinstance(item, dict) and item.get("postId") and item.get("username"):
                    url = f"https://x.com/{item['username']}/status/{item['postId']}"
                    if url not in self._web_search_urls_seen:
                        self._web_search_urls_seen.add(url)
                        # 构造 title：归一化空白，text 为空退回 @username
                        # Markdown 转义统一在 references_suffix() 中处理
                        raw = re.sub(r"\s+", " ", (item.get("text") or "")).strip()
                        if raw:
                            title = f"𝕏/@{item['username']}: {raw[:50]}{'...' if len(raw) > 50 else ''}"
                        else:
                            title = f"𝕏/@{item['username']}"
                        self._web_search_results.append({"url": url, "title": title, "type": "x_post"})

        token   = resp.get("token")
        think   = resp.get("isThinking")
        tag     = resp.get("messageTag")
        rollout = resp.get("rolloutId")
        step_id = resp.get("messageStepId")

        if tag == "tool_usage_card":
            # 正文已开始后的迟到 tool card：静默丢弃
            if self._content_started:
                return events
            if self._summary_mode:
                # 精简模式：走 ReasoningAggregator 提炼摘要
                for line in self._summarize_tool_usage_summary(
                    resp, rollout=rollout, step_id=step_id,
                ):
                    self._append_reasoning(
                        events, line,
                        rollout=rollout, tag=tag, step_id=step_id,
                    )
            else:
                # 详细模式：格式化为 emoji 单行（含 Agent 身份）
                line = self._format_tool_card(resp, rollout=rollout)
                if line:
                    # 同步 Agent 标识，确保后续 Grok summary 能正确插前缀
                    if rollout:
                        self._last_rollout = rollout
                    self._append_reasoning(
                        events, line,
                        rollout=rollout, tag=tag, step_id=step_id,
                    )
            return events   # card events (if any) already added

        # ── raw_function_result ───────────────────────────────────
        if tag == "raw_function_result":
            return events

        # ── toolUsageCardId-only follow-up frame ──────────────────
        if resp.get("toolUsageCardId") and not resp.get("webSearchResults") and not resp.get("codeExecutionResult"):
            return events

        # ── 思维链 token 处理 ──────────────────────────────────────
        if token is not None and think is True:
            # 正文已开始后的迟到 thinking：写入 buf（非流式可用）但不发事件（流式不显示）
            if self._content_started:
                raw = str(token).strip()
                if raw:
                    formatted = raw if raw.endswith("\n") else raw + "\n"
                    self.thinking_buf.append(formatted)
                return events
            if self._summary_mode:
                # 精简模式：走 ReasoningAggregator 提炼摘要
                for line in self._reasoning.on_thinking(
                    str(token), tag=tag, rollout=rollout,
                    step_id=step_id if isinstance(step_id, int) else None,
                ):
                    self._append_reasoning(
                        events, line,
                        rollout=rollout, tag=tag, step_id=step_id,
                    )
            else:
                # 详细模式：Agent 切换时插入身份前缀，原始 token 直接透传
                raw = str(token)
                # 去掉 Grok summary 自带的 "- " 前缀，避免触发 markdown 列表缩进
                if raw.startswith("- "):
                    raw = raw[2:]
                if not raw:
                    return events
                agent = rollout or ""
                if agent and agent != self._last_rollout:
                    self._last_rollout = agent
                    # Agent 切换标识：绕过去重，直接写 buf + 发 event（同一 Agent 可多次出现）
                    header = f"\n[{agent}]\n"
                    self.thinking_buf.append(header)
                    events.append(FrameEvent(
                        "thinking", header, rollout_id=agent,
                    ))
                self._append_reasoning(
                    events, raw,
                    rollout=rollout, tag=tag, step_id=step_id,
                )
            return events

        # ── final text token (needs cleaning) ─────────────────────
        if token is not None and think is not True and tag == "final":
            self._content_started = True
            raw_token = self._pending_text + str(token)
            self._pending_text = ""
            cleaned, local_anns = self._clean_token(raw_token)
            cleaned, self._pending_text = _split_trailing_incomplete_image_url(cleaned)
            if cleaned:
                # 先发 text 事件（OpenAI 顺序：text.delta 先，annotation.added 后）
                self.text_buf.append(cleaned)
                events.append(FrameEvent("text", cleaned))
                # 再发 annotation 事件：局部位置 → 绝对位置
                for ann in local_anns:
                    if ann.get("local_end", 0) > len(cleaned):
                        continue
                    ann["start_index"] = self._text_offset + ann.pop("local_start")
                    ann["end_index"] = self._text_offset + ann.pop("local_end")
                    self._annotations.append(ann)
                    events.append(FrameEvent("annotation", annotation_data=ann))
                self._text_offset += len(cleaned)
            return events

        # ── end signals ───────────────────────────────────────────
        if resp.get("isSoftStop"):
            self._flush_pending_reasoning(events)
            events.append(FrameEvent("soft_stop"))
            return events

        if resp.get("finalMetadata"):
            self._flush_pending_reasoning(events)
            events.append(FrameEvent("soft_stop"))
            return events

        return events

    # ------------------------------------------------------------------
    # Card attachment handling
    # ------------------------------------------------------------------

    def _handle_card(self, card_raw: dict) -> list[FrameEvent]:
        """Cache card data; emit image event on progress=100."""
        try:
            jd = orjson.loads(card_raw["jsonData"])
        except (orjson.JSONDecodeError, ValueError, TypeError, KeyError):
            return []

        card_id = jd.get("id", "")
        self._card_cache[card_id] = jd

        chunk = jd.get("image_chunk")
        if chunk:
            progress = chunk.get("progress")
            uuid = chunk.get("imageUuid", "")
            events: list[FrameEvent] = []
            try:
                if progress is not None and int(progress) < 100:
                    events.append(FrameEvent("image_progress", str(int(progress)), uuid))
            except (TypeError, ValueError):
                pass
            if chunk.get("progress") == 100 and not chunk.get("moderated"):
                self._remember_image_url(
                    events,
                    chunk.get("imageUrl", ""),
                    uuid,
                )
            return events

        if _card_may_contain_generated_image(jd):
            events: list[FrameEvent] = []
            for url, image_id in _iter_image_candidates(jd):
                self._remember_image_url(events, url, image_id)
            return events

        return []

    # ------------------------------------------------------------------
    # Token cleaning — <grok:render> → markdown
    # ------------------------------------------------------------------

    # 返回 (cleaned_text, local_annotations)，annotations 含局部 start/end
    def _clean_token(self, token: str) -> tuple[str, list[dict]]:
        if "<grok:render" not in token:
            return self._extract_markdown_images(token), []
        cleaned = _GROK_RENDER_RE.sub(self._render_replace, token)
        # 去除引用标签替换后残留的独占空白行（如 "\n [[1]](...)" → " [[1]](...)"）
        cleaned = cleaned.lstrip("\n") if cleaned.startswith("\n") and "[[" in cleaned else cleaned
        cleaned = self._extract_markdown_images(cleaned)

        # 从 cleaned 中定位 pending citations 的局部位置（游标递进防碰撞）
        local_annotations: list[dict] = []
        if self._pending_citations:
            search_start = 0
            for cite in self._pending_citations:
                pos = cleaned.find(cite["needle"], search_start)
                if pos != -1:
                    local_annotations.append({
                        "type": "url_citation",
                        "url": cite["url"],
                        "title": cite["title"],
                        "local_start": pos,
                        "local_end": pos + len(cite["needle"]),
                    })
                    search_start = pos + len(cite["needle"])
                # 找不到 → fail closed，跳过此 annotation
            self._pending_citations.clear()
        return cleaned, local_annotations

    def _render_replace(self, m: re.Match) -> str:
        card_id     = m.group(1)
        render_type = m.group(3)
        card = self._card_cache.get(card_id)
        if not card:
            return ""

        if render_type == "render_searched_image":
            img   = card.get("image", {})
            title = img.get("title", "image")
            thumb = img.get("thumbnail") or img.get("original", "")
            link  = img.get("link", "")
            if link:
                return f"[![{title}]({thumb})]({link})"
            return f"![{title}]({thumb})"

        if render_type == "render_generated_image":
            for url, image_id in _iter_image_candidates(card):
                self._remember_image_url([], url, image_id)
            return ""   # actual URL emitted by progress=100 card frame

        if render_type == "render_inline_citation":
            url = card.get("url", "")
            if not url:
                return ""
            index = self._citation_map.get(url)
            if index is None:
                self._citation_order.append(url)
                index = len(self._citation_order)
                self._citation_map[url] = index
            # 连续相同引用去重
            if index == self._last_citation_index:
                return ""
            self._last_citation_index = index
            citation_text = f" [[{index}]]({url})"
            # 解析标题：card → webSearchResults → URL fallback
            # Grok citation card 仅含 [id, type, cardType, url]，无 title 字段
            title = card.get("title", "")
            if not title:
                for item in self._web_search_results:
                    if item.get("url") == url:
                        title = item.get("title", "")
                        break
            # 记录引用元数据，位置在 _clean_token 返回后定位
            self._pending_citations.append({
                "url": url,
                "title": title or url,
                "needle": citation_text,
            })
            return citation_text

        return ""

    def _append_reasoning(
        self,
        events: list[FrameEvent],
        line: str,
        *,
        rollout: str | None,
        tag: str | None,
        step_id: Any,
    ) -> None:
        """将思维链文本追加到 thinking_buf 和事件列表（双模式去重）"""
        if self._summary_mode:
            # 精简模式：激进去重（移除标点/空格后比较）
            text = line.strip()
            if not text:
                return
            key = self._normalize_key(text)
        else:
            # 详细模式：精确去重（rollout + 原文）
            text = line
            if not text:
                return
            key = f"{rollout or ''}:{text}"

        if key in self._emitted_reasoning_keys:
            return
        self._emitted_reasoning_keys.add(key)

        # 统一用 \n 换行（去掉 "- " 前缀后不再有列表上下文，普通 \n 即可）
        formatted = text if text.endswith("\n") else text + "\n"
        self.thinking_buf.append(formatted)
        events.append(FrameEvent(
            "thinking",
            formatted,
            rollout_id=rollout or "",
            message_tag=tag or "",
            message_step_id=step_id if isinstance(step_id, int) else None,
        ))

    def _flush_pending_reasoning(self, events: list[FrameEvent]) -> None:
        """flush ReasoningAggregator 缓冲事件（仅精简模式有效）"""
        if self._summary_mode and self._reasoning is not None:
            for line in self._reasoning.finalize():
                self._append_reasoning(events, line, rollout="", tag="summary", step_id=None)

    @staticmethod
    def _extract_tool_info(resp: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """从 toolUsageCard 提取工具名（snake_case）和参数"""
        card = resp.get("toolUsageCard")
        if not isinstance(card, dict):
            return "", {}
        for key, value in card.items():
            if key == "toolUsageCardId" or not isinstance(value, dict):
                continue
            # camelCase → snake_case
            tool_name = re.sub(r"(?<!^)([A-Z])", r"_\1", key).lower()
            raw_args = value.get("args")
            return tool_name, (raw_args if isinstance(raw_args, dict) else {})
        return "", {}

    # 精简模式：走 ReasoningAggregator 提炼摘要
    def _summarize_tool_usage_summary(self, resp: dict[str, Any], *, rollout: str | None, step_id: int | None) -> list[str]:
        tool_name, args = self._extract_tool_info(resp)
        if not tool_name:
            return []
        return self._reasoning.on_tool_usage(tool_name, args, rollout=rollout, step_id=step_id)

    # 详细模式：格式化为 emoji 单行（含 Agent 身份）
    def _format_tool_card(self, resp: dict[str, Any], *, rollout: str | None) -> str:
        tool_name, args = self._extract_tool_info(resp)
        if not tool_name:
            return ""
        emoji, arg_keys = _TOOL_FMT.get(tool_name, ("🔧", ()))
        # 提取要展示的参数值
        display_arg = ""
        for ak in arg_keys:
            val = args.get(ak)
            if val:
                display_arg = str(val).strip()
                break
        # 构造 Agent 前缀（不加前导 \n，由 _append_reasoning 统一处理换行）
        prefix = f"[{rollout}] " if rollout else ""
        if display_arg:
            return f"{prefix}{emoji} {tool_name}: {display_arg}"
        return f"{prefix}{emoji} {tool_name}"

    def _normalize_key(self, text: str) -> str:
        lowered = text.lower()
        lowered = re.sub(r"https?://\S+", "", lowered)
        lowered = re.sub(r"[^\w\u4e00-\u9fff]+", "", lowered)
        return lowered

    def _remember_image_url(self, events: list[FrameEvent], url: str, image_id: str = "") -> None:
        normalized = _normalize_image_url(url)
        if not normalized or normalized in self._seen_image_urls:
            return
        resolved_id = image_id or _image_id_from_url(normalized)
        self._seen_image_urls.add(normalized)
        self.image_urls.append((normalized, resolved_id))
        events.append(FrameEvent("image", normalized, resolved_id))

    def _extract_markdown_images(self, text: str) -> str:
        def _remember(raw_url: str) -> bool:
            if not _looks_like_image_asset_url(raw_url):
                return False
            self._remember_image_url([], raw_url)
            return True

        def _replace(match: re.Match) -> str:
            raw_url = match.group("url").strip().strip("<>")
            if not _remember(raw_url):
                return match.group(0)
            return ""

        def _replace_bare(match: re.Match) -> str:
            raw_url = match.group("url").strip()
            image_url = raw_url.rstrip(".,;:!?")
            suffix = raw_url[len(image_url):]
            if not _looks_like_complete_image_url(image_url):
                return match.group(0)
            if not _remember(image_url):
                return match.group(0)
            return suffix

        cleaned = _MARKDOWN_IMAGE_RE.sub(_replace, text)
        return _BARE_IMAGE_URL_RE.sub(_replace_bare, cleaned)

    def extract_generated_images_from_text(self, text: str) -> str:
        """Extract generated image URLs after all text chunks have been joined."""
        return self._extract_markdown_images(text)


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
        return f"{_IMAGE_BASE.rstrip('/')}{url}"
    return f"{_IMAGE_BASE}{url.lstrip('/')}"


def _looks_like_image_asset_url(value: str, *, trust_url: bool = False) -> bool:
    url = str(value or "").strip()
    if not url:
        return False
    lowered = url.lower()
    if lowered.startswith("data:image/"):
        return True
    normalized = _normalize_image_url(url).lower()
    if trust_url and (
        lowered.startswith(("http://", "https://", "//", "/"))
        or lowered.startswith(("images/", "image/", "content/"))
    ):
        return True
    return (
        any(marker in normalized for marker in _IMAGE_HOST_MARKERS)
        or "/images/" in normalized
        or normalized.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))
        or "/content" in normalized
        or "image" in normalized
    )


def _looks_like_complete_image_url(value: str) -> bool:
    url = str(value or "").strip()
    if not url:
        return False
    lowered = url.lower()
    return (
        lowered.startswith("data:image/")
        or bool(_COMPLETE_IMAGE_URL_RE.search(url))
        or lowered.endswith("/content")
        or "/content?" in lowered
    )


def _looks_like_incomplete_image_url(value: str) -> bool:
    url = str(value or "").strip().lower()
    if not url:
        return False
    normalized = _normalize_image_url(url).lower()
    return (
        any(marker in normalized for marker in _IMAGE_HOST_MARKERS)
        or "/images/" in normalized
        or "/content" in normalized
        or "generated-image" in normalized
    )


def _split_trailing_incomplete_image_url(text: str) -> tuple[str, str]:
    """Hold a trailing image URL fragment until a later chunk completes it."""
    match = _TRAILING_URL_RE.search(text or "")
    if not match:
        return text, ""
    url = match.group(0)
    if _looks_like_complete_image_url(url) or not _looks_like_incomplete_image_url(url):
        return text, ""

    safe = text[: match.start()]
    pending = text[match.start():]
    opener = _MARKDOWN_IMAGE_OPENER_RE.search(safe)
    if opener:
        pending = safe[opener.start():] + pending
        safe = safe[: opener.start()]
    return safe, pending


def _image_id_from_url(url: str) -> str:
    match = _IMAGE_ID_FROM_URL_RE.search(url or "")
    if match:
        return match.group(1).lower()
    return hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:32]


def _card_may_contain_generated_image(obj: Any) -> bool:
    if isinstance(obj, dict):
        for key, value in obj.items():
            lowered = str(key).lower()
            if (
                "generated" in lowered
                or lowered in {"image_chunk", "imagegeneration", "image_generation"}
                or lowered.endswith("imageurl")
            ):
                return True
            if isinstance(value, (dict, list)) and _card_may_contain_generated_image(value):
                return True
    elif isinstance(obj, list):
        return any(_card_may_contain_generated_image(item) for item in obj)
    return False


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


def _key_indicates_image_scope(key: str) -> bool:
    lowered = str(key or "").lower()
    return (
        "generated" in lowered
        or "image" in lowered
        or "asset" in lowered
        or "media" in lowered
    )


def _iter_image_candidates(obj: Any, inherited_id: str = "", in_image_scope: bool = False):
    if isinstance(obj, dict):
        obj_type = str(obj.get("type") or obj.get("cardType") or obj.get("card_type") or "")
        image_scope = in_image_scope or _key_indicates_image_scope(obj_type)
        local_id = inherited_id
        for key in _IMAGE_ID_KEYS:
            if key == "id" and not image_scope:
                continue
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                local_id = value.strip()
                break

        result = obj.get("result")
        if image_scope and isinstance(result, str) and _looks_like_image_asset_url(result, trust_url=True):
            yield result, local_id

        for key, value in obj.items():
            child_scope = image_scope or _key_indicates_image_scope(key)
            if (
                _key_indicates_image_url(key)
                and isinstance(value, str)
                and _looks_like_image_asset_url(value, trust_url=child_scope)
            ):
                yield value, local_id
            elif isinstance(value, (dict, list)):
                yield from _iter_image_candidates(value, local_id, child_scope)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_image_candidates(item, inherited_id, in_image_scope)


__all__ = [
    "build_chat_payload",
    "classify_line",
    "FrameEvent",
    "StreamAdapter",
]
