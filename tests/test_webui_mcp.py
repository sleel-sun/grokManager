from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from app.products.openai.schemas import ChatCompletionRequest
from app.products.web.webui import mcp as mcp_module
from app.products.web.webui.mcp import (
    McpStdioClient,
    _build_openai_tools,
    _format_tool_result,
    _mcp_agent_stream,
    _selected_mcp_options,
    should_handle_mcp,
)


def _write_fake_mcp_server(path: Path) -> None:
    path.write_text(
        """
import json
import sys

for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    method = msg.get("method")
    if method == "initialize":
        result = {
            "protocolVersion": msg.get("params", {}).get("protocolVersion"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "1.0"},
        }
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": "echo",
                    "description": "Echo text",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                    },
                }
            ]
        }
    elif method == "tools/call":
        args = msg.get("params", {}).get("arguments", {})
        result = {"content": [{"type": "text", "text": "echo:" + args.get("text", "")}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\\n")
    sys.stdout.flush()
""".lstrip(),
        encoding="utf-8",
    )


def test_stdio_mcp_client_lists_and_calls_tools(tmp_path: Path) -> None:
    server_script = tmp_path / "fake_mcp_server.py"
    _write_fake_mcp_server(server_script)
    server = {
        "id": "fake-server",
        "name": "Fake",
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(server_script)],
        "env": {},
        "cwd": None,
        "timeout_s": 5,
        "enabled": True,
    }

    async def run() -> None:
        async with McpStdioClient(server) as client:
            tools = await client.list_tools()
            assert tools[0]["name"] == "echo"
            result = await client.call_tool("echo", {"text": "hello"})
            assert _format_tool_result(result) == "echo:hello"

        openai_tools, mapping = await _build_openai_tools([server])
        assert openai_tools[0]["function"]["name"] == "mcp__fake_server__echo"
        assert mapping["mcp__fake_server__echo"].tool_name == "echo"

    asyncio.run(run())


def test_webui_mcp_request_options_and_model_gate() -> None:
    req = ChatCompletionRequest(
        model="grok-4.20-0309",
        messages=[{"role": "user", "content": "hello"}],
        mcp={"enabled": True, "auto": False, "server_ids": ["srv"], "tool_choice": "required"},
    )

    assert should_handle_mcp(req)
    assert _selected_mcp_options(req) == {
        "enabled": True,
        "auto": False,
        "server_ids": ["srv"],
        "tool_choice": "required",
        "max_steps": 2,
    }

    disabled_req = ChatCompletionRequest(
        model="grok-4.20-0309",
        messages=[{"role": "user", "content": "hello"}],
        mcp={"enabled": True, "tool_choice": "none"},
    )
    assert not should_handle_mcp(disabled_req)

    image_req = ChatCompletionRequest(
        model="grok-imagine-image",
        messages=[{"role": "user", "content": "draw"}],
        mcp={"enabled": True},
    )
    assert not should_handle_mcp(image_req)


def test_mcp_agent_stream_executes_tool_and_returns_final_answer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    server_script = tmp_path / "fake_mcp_server.py"
    _write_fake_mcp_server(server_script)
    server = {
        "id": "fake-server",
        "name": "Fake",
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(server_script)],
        "env": {},
        "cwd": None,
        "timeout_s": 5,
        "enabled": True,
    }
    model_calls = []

    async def fake_load_servers():
        return [server]

    async def fake_chat_completions(**kwargs):
        model_calls.append(kwargs)

        async def gen():
            if len(model_calls) == 1:
                yield _chat_sse(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call_echo",
                                            "type": "function",
                                            "function": {
                                                "name": "mcp__fake_server__echo",
                                                "arguments": '{"text":"hello"}',
                                            },
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                )
                yield _chat_sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
                yield "data: [DONE]\n\n"
                return

            tool_messages = [
                item
                for item in kwargs.get("messages", [])
                if item.get("role") == "tool"
            ]
            content = f"final: {tool_messages[-1]['content']}"
            yield _chat_sse({"choices": [{"delta": {"content": content}}]})
            yield _chat_sse({"choices": [{"delta": {}, "finish_reason": "stop"}]})
            yield "data: [DONE]\n\n"

        return gen()

    monkeypatch.setattr(mcp_module, "_load_servers", fake_load_servers)
    monkeypatch.setattr(mcp_module, "chat_completions", fake_chat_completions)

    req = ChatCompletionRequest(
        model="grok-4.20-0309",
        messages=[{"role": "user", "content": "echo hello"}],
        mcp={"enabled": True, "auto": True, "tool_choice": "auto", "max_steps": 2},
    )

    async def run() -> list[str]:
        return [chunk async for chunk in _mcp_agent_stream(req)]

    chunks = asyncio.run(run())
    combined = "".join(chunks)

    assert "event: mcp" in combined
    assert '"status":"ready"' in combined
    assert '"status":"running"' in combined
    assert '"status":"done"' in combined
    assert "final: echo:hello" in combined
    assert combined.rstrip().endswith("data: [DONE]")
    assert len(model_calls) == 2
    assert model_calls[0]["tools"][0]["function"]["name"] == "mcp__fake_server__echo"
    assert model_calls[1]["messages"][-1]["role"] == "tool"
    assert model_calls[1]["messages"][-1]["content"] == "echo:hello"


def _chat_sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
