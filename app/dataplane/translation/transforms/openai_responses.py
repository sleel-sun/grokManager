"""OpenAI Responses request transforms."""

from typing import Any

from ..types import TranslationContext


def responses_tools_to_chat(tools: list[dict]) -> list[dict]:
    """Convert flat Responses function tools to Chat Completions tools."""
    normalized = []
    for tool in tools:
        if tool.get("type") == "function" and "function" not in tool and "name" in tool:
            normalized.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.get("name", ""),
                        "description": tool.get("description", ""),
                        "parameters": tool.get("parameters"),
                    },
                }
            )
        else:
            normalized.append(tool)
    return normalized


def _parse_input(input_value: str | list) -> list[dict]:
    if isinstance(input_value, str):
        return [{"role": "user", "content": input_value}]

    messages: list[dict] = []
    for item in input_value:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type", "message" if "role" in item else None)

        if item_type == "function_call":
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": item.get("call_id", ""),
                            "type": "function",
                            "function": {
                                "name": item.get("name", ""),
                                "arguments": item.get("arguments", "{}"),
                            },
                        }
                    ],
                }
            )
            continue

        if item_type == "function_call_output":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.get("call_id", ""),
                    "content": item.get("output", ""),
                }
            )
            continue

        if item_type != "message":
            continue

        content = item.get("content", "")
        if isinstance(content, list):
            normalized: list[dict] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type", "")
                if part_type in ("input_text", "output_text"):
                    normalized.append({"type": "text", "text": part.get("text", "")})
                elif part_type in ("image", "input_image"):
                    source = part.get("image_url") or part.get("source") or {}
                    url = (
                        source.get("url", "")
                        if isinstance(source, dict)
                        else str(source)
                    )
                    if url:
                        normalized.append(
                            {
                                "type": "image_url",
                                "image_url": {"url": url},
                            }
                        )
                else:
                    normalized.append(part)
            content = normalized

        messages.append({"role": item.get("role", "user"), "content": content})
    return messages


def translate_openai_responses_request(
    body: Any,
    _context: TranslationContext,
) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise TypeError("OpenAI Responses request must be a dict")
    messages: list[dict] = []
    instructions = body.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": str(instructions)})
    messages.extend(_parse_input(body.get("input", "")))
    tools = body.get("tools")
    return {
        "messages": messages,
        "tools": responses_tools_to_chat(tools) if isinstance(tools, list) else [],
        "tool_choice": body.get("tool_choice"),
    }


__all__ = ["responses_tools_to_chat", "translate_openai_responses_request"]
