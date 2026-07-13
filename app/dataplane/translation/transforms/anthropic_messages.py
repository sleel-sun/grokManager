"""Anthropic Messages request transforms."""

import json
import secrets
import time
from typing import Any

from ..types import TranslationContext


def _make_tool_id() -> str:
    return f"toolu_{int(time.time() * 1000)}{secrets.token_hex(3)}"


def _content_to_chat(content: Any, role: str) -> list[dict]:
    if isinstance(content, str):
        return [{"role": role, "content": content}]
    if not isinstance(content, list):
        return []

    tool_results = [
        block
        for block in content
        if isinstance(block, dict) and block.get("type") == "tool_result"
    ]
    if tool_results:
        messages = []
        for block in tool_results:
            result_content = block.get("content", "")
            if isinstance(result_content, list):
                result_content = "\n".join(
                    item.get("text", "")
                    for item in result_content
                    if isinstance(item, dict) and item.get("type") == "text"
                )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id", ""),
                    "content": result_content or "",
                }
            )
        return messages

    if any(
        isinstance(block, dict) and block.get("type") == "tool_use" for block in content
    ):
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    {
                        "id": block.get("id") or _make_tool_id(),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(
                                block.get("input") or {},
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                    }
                )
        return [
            {
                "role": "assistant",
                "content": " ".join(text_parts) if text_parts else None,
                "tool_calls": tool_calls,
            }
        ]

    normalized: list[dict] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = (block.get("text") or "").strip()
            if text:
                normalized.append({"type": "text", "text": text})
        elif block_type == "image":
            source = block.get("source") or {}
            if source.get("type") == "base64":
                media = source.get("media_type", "image/jpeg")
                normalized.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{media};base64,{source.get('data', '')}"
                        },
                    }
                )
            elif source.get("type") == "url":
                normalized.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": source.get("url", "")},
                    }
                )
        elif block_type == "document":
            source = block.get("source") or {}
            if source.get("type") == "base64":
                media = source.get("media_type", "application/pdf")
                normalized.append(
                    {
                        "type": "file",
                        "file": {
                            "data": f"data:{media};base64,{source.get('data', '')}"
                        },
                    }
                )

    if not normalized:
        return []
    return [{"role": role, "content": normalized}]


def _messages_to_chat(messages: list[dict], system: str | list | None) -> list[dict]:
    internal: list[dict] = []
    if system:
        if isinstance(system, str):
            system_text = system
        elif isinstance(system, list):
            system_text = "\n".join(
                block.get("text", "")
                for block in system
                if isinstance(block, dict) and block.get("type") == "text"
            )
        else:
            system_text = str(system)
        if system_text.strip():
            internal.append({"role": "system", "content": system_text})

    for message in messages:
        internal.extend(
            _content_to_chat(
                message.get("content", ""),
                message.get("role", "user"),
            )
        )
    return internal


def _tools_to_chat(tools: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema"),
            },
        }
        for tool in tools
    ]


def _tool_choice_to_chat(tool_choice: Any) -> Any:
    if tool_choice is None:
        return "auto"
    if isinstance(tool_choice, str):
        return tool_choice
    if isinstance(tool_choice, dict):
        choice_type = tool_choice.get("type", "auto")
        if choice_type == "auto":
            return "auto"
        if choice_type == "any":
            return "required"
        if choice_type == "tool":
            return {
                "type": "function",
                "function": {"name": tool_choice.get("name", "")},
            }
    return "auto"


def translate_anthropic_messages_request(
    body: Any,
    _context: TranslationContext,
) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise TypeError("Anthropic Messages request must be a dict")
    tools = body.get("tools")
    return {
        "messages": _messages_to_chat(
            body.get("messages") or [],
            body.get("system"),
        ),
        "tools": _tools_to_chat(tools) if isinstance(tools, list) else [],
        "tool_choice": _tool_choice_to_chat(body.get("tool_choice")),
    }


__all__ = ["translate_anthropic_messages_request"]
