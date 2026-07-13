"""WebUI MCP server management and tool execution.

Implements a minimal MCP client for stdio servers:
``initialize`` -> ``notifications/initialized`` -> ``tools/list`` /
``tools/call`` over newline-delimited JSON-RPC.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncGenerator, Literal

import orjson
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.control.model import registry as model_registry
from app.platform.auth.middleware import WebUIUser, verify_webui_key
from app.platform.errors import AppError, ValidationError
from app.platform.logging.logger import logger
from app.platform.paths import data_path
from app.products._upstream_headers import build_upstream_response_headers
from app.products.openai.chat import completions as chat_completions
from app.products.openai.router import _SSE_HEADERS, _safe_sse, _sse_with_heartbeat
from app.products.openai.schemas import ChatCompletionRequest


router = APIRouter(
    prefix="/webui/api",
    tags=["WebUI - MCP"],
)

_STORE_PATH = data_path("webui", "mcp_servers.json")
_MCP_PROTOCOL_VERSION = "2025-06-18"
_MAX_TOOL_RESULT_CHARS = 12000
_MAX_MODEL_TOOL_STEPS = 4
_VALID_TOOL_CHOICE = {"auto", "required", "none"}
_WEBUI_CHAT_REQUEST_OVERRIDES = {"temporary": True, "disableMemory": True}


class McpServerConfig(BaseModel):
    id: str | None = None
    name: str = Field(..., min_length=1, max_length=80)
    description: str = ""
    enabled: bool = True
    transport: Literal["stdio"] = "stdio"
    command: str = Field("", max_length=500)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    timeout_s: float = Field(30.0, ge=1.0, le=300.0)


class McpServerPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=80)
    description: str | None = None
    enabled: bool | None = None
    transport: Literal["stdio"] | None = None
    command: str | None = Field(None, max_length=500)
    args: list[str] | None = None
    env: dict[str, str] | None = None
    cwd: str | None = None
    timeout_s: float | None = Field(None, ge=1.0, le=300.0)


class McpServerImportRequest(BaseModel):
    config: Any
    replace: bool = False


@dataclass(slots=True)
class McpToolRef:
    server: dict[str, Any]
    tool_name: str
    function_name: str


@dataclass(slots=True)
class ToolPassResult:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    done: bool = False


def _new_server_id(name: str) -> str:
    base = _slug(name) or "mcp"
    return f"{base}-{uuid.uuid4().hex[:8]}"


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", (value or "").strip().lower())
    return slug.strip("-_")[:48]


def _tool_function_name(server_id: str, tool_name: str, used: set[str]) -> str:
    server_part = re.sub(r"[^a-zA-Z0-9_]+", "_", server_id).strip("_") or "server"
    tool_part = re.sub(r"[^a-zA-Z0-9_]+", "_", tool_name).strip("_") or "tool"
    base = f"mcp__{server_part}__{tool_part}"[:64].strip("_") or "mcp_tool"
    name = base
    suffix = 2
    while name in used:
        trim = max(1, 64 - len(str(suffix)) - 1)
        name = f"{base[:trim]}_{suffix}"
        suffix += 1
    used.add(name)
    return name


def _store_path_for_user(user: WebUIUser | None = None) -> Path:
    if user is None or user.legacy or user.anonymous:
        return _STORE_PATH
    return data_path("webui", "users", user.id, "mcp_servers.json")


def _read_store_sync(path: Path | None = None) -> list[dict[str, Any]]:
    store_path = path or _STORE_PATH
    try:
        raw = store_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    servers = parsed.get("servers") if isinstance(parsed, dict) else parsed
    return servers if isinstance(servers, list) else []


def _write_store_sync(servers: list[dict[str, Any]], path: Path | None = None) -> None:
    store_path = path or _STORE_PATH
    store_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"servers": servers, "updated_at": int(time.time())}
    store_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


async def _load_servers(user: WebUIUser | None = None) -> list[dict[str, Any]]:
    servers = await asyncio.to_thread(_read_store_sync, _store_path_for_user(user))
    return [_normalize_server(item) for item in servers if isinstance(item, dict)]


async def _save_servers(servers: list[dict[str, Any]], user: WebUIUser | None = None) -> None:
    await asyncio.to_thread(_write_store_sync, servers, _store_path_for_user(user))


def _normalize_server(item: dict[str, Any]) -> dict[str, Any]:
    name = str(item.get("name") or "MCP Server").strip()[:80] or "MCP Server"
    command = str(item.get("command") or "").strip()
    raw_args = item.get("args") if isinstance(item.get("args"), list) else []
    raw_env = item.get("env") if isinstance(item.get("env"), dict) else {}
    timeout_s = item.get("timeout_s", 30.0)
    try:
        timeout_s = min(max(float(timeout_s), 1.0), 300.0)
    except (TypeError, ValueError):
        timeout_s = 30.0
    server_id = str(item.get("id") or "").strip() or _new_server_id(name)
    return {
        "id": server_id,
        "name": name,
        "description": str(item.get("description") or ""),
        "enabled": bool(item.get("enabled", True)),
        "transport": "stdio",
        "command": command,
        "args": [str(arg) for arg in raw_args],
        "env": {str(k): str(v) for k, v in raw_env.items()},
        "cwd": str(item.get("cwd") or "").strip() or None,
        "timeout_s": timeout_s,
    }


def _server_from_model(model: McpServerConfig) -> dict[str, Any]:
    data = _normalize_server(model.model_dump(exclude_none=True))
    data["id"] = model.id.strip() if model.id and model.id.strip() else _new_server_id(data["name"])
    return data


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
    return default


def _coerce_args(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    if isinstance(value, str):
        if not value.strip():
            return []
        try:
            return shlex.split(value)
        except ValueError:
            return [value]
    return []


def _coerce_env(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(key): str(val) for key, val in value.items() if key}
    if isinstance(value, list):
        env: dict[str, str] = {}
        for item in value:
            key, sep, val = str(item).partition("=")
            if sep and key.strip():
                env[key.strip()] = val
        return env
    return {}


def _import_entries(config: Any) -> list[tuple[str, dict[str, Any]]]:
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except json.JSONDecodeError:
            return []
    if isinstance(config, list):
        return [(str(index + 1), item) for index, item in enumerate(config) if isinstance(item, dict)]
    if not isinstance(config, dict):
        return []

    for key in ("mcpServers", "mcp_servers", "servers"):
        raw = config.get(key)
        if isinstance(raw, dict):
            return [(str(name), item) for name, item in raw.items() if isinstance(item, dict)]
        if isinstance(raw, list):
            return [(str(index + 1), item) for index, item in enumerate(raw) if isinstance(item, dict)]

    if isinstance(config.get("command"), (str, list)):
        return [(str(config.get("name") or config.get("id") or "MCP Server"), config)]

    if config and all(isinstance(value, dict) for value in config.values()):
        return [(str(name), item) for name, item in config.items()]

    return []


def _server_from_import_entry(name_hint: str, item: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    transport = str(item.get("transport") or item.get("type") or "stdio").strip().lower()
    if transport and transport != "stdio":
        return None, f"{name_hint}: unsupported transport {transport}"

    raw_command = item.get("command") or item.get("cmd")
    args = _coerce_args(item.get("args") if "args" in item else item.get("arguments"))
    if isinstance(raw_command, list):
        command_parts = [str(part) for part in raw_command if part is not None]
        raw_command = command_parts[0] if command_parts else ""
        args = command_parts[1:] + args
    command = str(raw_command or "").strip()
    if not command:
        return None, f"{name_hint}: command is required"

    name = str(item.get("name") or name_hint or item.get("id") or "MCP Server").strip()[:80] or "MCP Server"
    raw_enabled = item.get("enabled", None)
    enabled = _coerce_bool(raw_enabled, default=True)
    if "disabled" in item:
        enabled = not _coerce_bool(item.get("disabled"), default=False)

    server = _normalize_server(
        {
            "id": str(item.get("id") or "").strip(),
            "name": name,
            "description": str(item.get("description") or ""),
            "enabled": enabled,
            "transport": "stdio",
            "command": command,
            "args": args,
            "env": _coerce_env(item.get("env") if "env" in item else item.get("environment")),
            "cwd": item.get("cwd") or item.get("workingDirectory") or item.get("working_directory"),
            "timeout_s": (
                item.get("timeout_s")
                if "timeout_s" in item
                else item.get("timeout")
                if "timeout" in item
                else item.get("timeoutSeconds", 30.0)
            ),
        }
    )
    return server, None


def _parse_imported_servers(config: Any) -> tuple[list[dict[str, Any]], list[str]]:
    imported: list[dict[str, Any]] = []
    skipped: list[str] = []
    entries = _import_entries(config)
    if not entries:
        return [], ["No MCP server entries found"]
    for name_hint, item in entries:
        server, error = _server_from_import_entry(name_hint, item)
        if error:
            skipped.append(error)
            continue
        if server is not None:
            imported.append(server)
    return imported, skipped


def _merge_imported_servers(
    existing: list[dict[str, Any]],
    imported: list[dict[str, Any]],
    *,
    replace: bool,
) -> tuple[list[dict[str, Any]], int, int]:
    if replace:
        return imported, len(imported), 0

    servers = [dict(server) for server in existing]
    created = 0
    updated = 0

    def rebuild_indexes() -> tuple[dict[str, int], dict[str, int]]:
        by_id = {str(server.get("id")): index for index, server in enumerate(servers) if server.get("id")}
        by_name = {str(server.get("name") or "").strip().lower(): index for index, server in enumerate(servers) if server.get("name")}
        return by_id, by_name

    for server in imported:
        by_id, by_name = rebuild_indexes()
        server_id = str(server.get("id") or "")
        name_key = str(server.get("name") or "").strip().lower()
        index = by_id.get(server_id) if server_id else None
        if index is None and name_key:
            index = by_name.get(name_key)
        if index is None:
            servers.append(server)
            created += 1
            continue
        if server_id not in by_id and servers[index].get("id"):
            server["id"] = servers[index]["id"]
        servers[index] = server
        updated += 1

    return servers, created, updated


def _validate_server_for_run(server: dict[str, Any]) -> None:
    if server.get("transport") != "stdio":
        raise ValidationError("Only stdio MCP servers are supported", param="transport")
    if not str(server.get("command") or "").strip():
        raise ValidationError("MCP server command is required", param="command")
    cwd = server.get("cwd")
    if cwd and not Path(str(cwd)).exists():
        raise ValidationError(f"MCP server cwd does not exist: {cwd}", param="cwd")


class McpStdioClient:
    def __init__(self, server: dict[str, Any]) -> None:
        self.server = server
        self.proc: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._stderr_task: asyncio.Task | None = None
        self._stderr: list[str] = []

    async def __aenter__(self) -> "McpStdioClient":
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        _validate_server_for_run(self.server)
        env = os.environ.copy()
        env.update(self.server.get("env") or {})
        command = str(self.server.get("command") or "").strip()
        args = [str(arg) for arg in self.server.get("args") or []]
        try:
            self.proc = await asyncio.create_subprocess_exec(
                command,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self.server.get("cwd") or None,
            )
        except FileNotFoundError as exc:
            raise ValidationError(f"MCP command not found: {command}", param="command") from exc
        except Exception as exc:
            raise ValidationError(f"Failed to start MCP server: {exc}", param="command") from exc
        self._stderr_task = asyncio.create_task(self._read_stderr(), name="mcp-stderr")
        await self.initialize()

    async def close(self) -> None:
        proc = self.proc
        if proc is None:
            return
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        if self._stderr_task:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
        self.proc = None

    async def _read_stderr(self) -> None:
        if not self.proc or not self.proc.stderr:
            return
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            if text:
                self._stderr.append(text[:500])
                self._stderr[:] = self._stderr[-20:]

    async def initialize(self) -> None:
        await self.request(
            "initialize",
            {
                "protocolVersion": _MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "grokManager WebUI", "version": "1.0.0"},
            },
        )
        await self.notify("notifications/initialized", {})

    async def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = await self.request("tools/list", params)
            raw_tools = result.get("tools") if isinstance(result, dict) else []
            if isinstance(raw_tools, list):
                tools.extend([tool for tool in raw_tools if isinstance(tool, dict)])
            cursor = result.get("nextCursor") if isinstance(result, dict) else None
            if not cursor:
                return tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self.request(
            "tools/call",
            {"name": tool_name, "arguments": arguments or {}},
        )
        return result if isinstance(result, dict) else {"content": [{"type": "text", "text": str(result)}]}

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        request_id = self._next_id
        self._next_id += 1
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }
        )
        return await self._read_response(request_id)

    async def _send(self, payload: dict[str, Any]) -> None:
        if not self.proc or not self.proc.stdin:
            raise ValidationError("MCP server is not running", param="command")
        raw = orjson.dumps(payload) + b"\n"
        self.proc.stdin.write(raw)
        await self.proc.stdin.drain()

    async def _read_response(self, request_id: int) -> Any:
        if not self.proc or not self.proc.stdout:
            raise ValidationError("MCP server stdout is not available", param="command")
        timeout_s = float(self.server.get("timeout_s") or 30.0)
        while True:
            try:
                line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=timeout_s)
            except asyncio.TimeoutError as exc:
                stderr = "\n".join(self._stderr[-5:])
                suffix = f"; stderr: {stderr}" if stderr else ""
                raise ValidationError(
                    f"MCP server timed out waiting for response{suffix}",
                    param="timeout_s",
                ) from exc
            if not line:
                stderr = "\n".join(self._stderr[-5:])
                suffix = f"; stderr: {stderr}" if stderr else ""
                raise ValidationError(f"MCP server exited before responding{suffix}", param="command")
            try:
                message = orjson.loads(line)
            except orjson.JSONDecodeError:
                continue
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue
            if "error" in message:
                err = message.get("error") or {}
                if isinstance(err, dict):
                    raise ValidationError(str(err.get("message") or err), param="mcp")
                raise ValidationError(str(err), param="mcp")
            return message.get("result")


async def _discover_server_tools(server: dict[str, Any]) -> list[dict[str, Any]]:
    async with McpStdioClient(server) as client:
        return await client.list_tools()


async def _build_openai_tools(
    selected_servers: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, McpToolRef]]:
    tools: list[dict[str, Any]] = []
    mapping: dict[str, McpToolRef] = {}
    used_names: set[str] = set()
    for server in selected_servers:
        try:
            mcp_tools = await _discover_server_tools(server)
        except Exception as exc:
            logger.warning(
                "mcp tools/list failed: server_id={} server={} error={}",
                server.get("id"),
                server.get("name"),
                exc,
            )
            continue
        for tool in mcp_tools:
            original_name = str(tool.get("name") or "").strip()
            if not original_name:
                continue
            function_name = _tool_function_name(str(server.get("id") or ""), original_name, used_names)
            title = str(tool.get("title") or original_name)
            desc = str(tool.get("description") or "")
            input_schema = tool.get("inputSchema") or tool.get("input_schema")
            if not isinstance(input_schema, dict):
                input_schema = {"type": "object", "properties": {}}
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": function_name,
                        "description": f"MCP {server.get('name')} / {title}. {desc}".strip(),
                        "parameters": input_schema,
                    },
                }
            )
            mapping[function_name] = McpToolRef(
                server=server,
                tool_name=original_name,
                function_name=function_name,
            )
    return tools, mapping


def _parse_sse_event(chunk: str) -> tuple[str, str]:
    event = "message"
    data_lines: list[str] = []
    for line in chunk.replace("\r\n", "\n").split("\n"):
        if line.startswith("event:"):
            event = line[6:].strip() or "message"
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    return event, "\n".join(data_lines)


def _accumulate_tool_calls(
    result: ToolPassResult,
    tool_deltas: list[dict[str, Any]],
) -> None:
    if result.tool_calls is None:
        result.tool_calls = []
    for delta in tool_deltas:
        if not isinstance(delta, dict):
            continue
        index = int(delta.get("index") or 0)
        while len(result.tool_calls) <= index:
            result.tool_calls.append(
                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
            )
        target = result.tool_calls[index]
        if delta.get("id"):
            target["id"] = str(delta.get("id"))
        if delta.get("type"):
            target["type"] = str(delta.get("type"))
        func_delta = delta.get("function") or {}
        if not isinstance(func_delta, dict):
            continue
        target_func = target.setdefault("function", {})
        if func_delta.get("name"):
            target_func["name"] = str(func_delta.get("name"))
        if "arguments" in func_delta:
            target_func["arguments"] = str(target_func.get("arguments") or "") + str(func_delta.get("arguments") or "")


async def _model_pass_stream(
    *,
    req: ChatCompletionRequest,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    tool_choice: Any,
    result: ToolPassResult,
) -> AsyncGenerator[str, None]:
    stream = await chat_completions(
        model=req.model,
        messages=messages,
        stream=True,
        emit_think=None if req.reasoning_effort is None else req.reasoning_effort != "none",
        tools=tools,
        tool_choice=tool_choice,
        tool_scope="client_only",
        temperature=req.temperature or 0.8,
        top_p=req.top_p or 0.95,
        request_overrides=_request_overrides(req),
    )
    if isinstance(stream, dict):
        content = (((stream.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        result.content += str(content)
        yield f"data: {orjson.dumps(stream).decode()}\n\n"
        result.done = True
        return

    async for chunk in stream:
        event, payload = _parse_sse_event(chunk)
        if event == "error":
            yield chunk
            result.done = True
            return
        if not payload:
            yield chunk
            continue
        if payload.strip() == "[DONE]":
            if result.tool_calls:
                result.done = True
                return
            yield chunk
            result.done = True
            return
        try:
            parsed = orjson.loads(payload)
        except orjson.JSONDecodeError:
            yield chunk
            continue
        choice = (parsed.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        finish_reason = choice.get("finish_reason")
        if isinstance(delta, dict) and delta.get("tool_calls"):
            _accumulate_tool_calls(result, delta.get("tool_calls") or [])
            continue
        if finish_reason == "tool_calls":
            result.done = True
            return
        if isinstance(delta, dict):
            if isinstance(delta.get("content"), str):
                result.content += delta["content"]
            if isinstance(delta.get("reasoning_content"), str):
                result.reasoning += delta["reasoning_content"]
        yield chunk


def _request_overrides(req: ChatCompletionRequest) -> dict[str, Any] | None:
    spec = model_registry.get(req.model)
    overrides: dict[str, Any] = dict(_WEBUI_CHAT_REQUEST_OVERRIDES)
    if req.deepsearch:
        overrides["deepsearchPreset"] = req.deepsearch
    if spec and spec.uses_console_responses() and req.reasoning_effort is not None:
        overrides["_reasoning_effort"] = req.reasoning_effort
    return overrides or None


def _tool_arguments(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _format_tool_result(result: dict[str, Any]) -> str:
    parts: list[str] = []
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
            else:
                parts.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
    if result.get("structuredContent") is not None:
        parts.append(json.dumps(result.get("structuredContent"), ensure_ascii=False, indent=2))
    if not parts:
        parts.append(json.dumps(result, ensure_ascii=False, indent=2))
    text = "\n\n".join(parts).strip()
    if len(text) > _MAX_TOOL_RESULT_CHARS:
        return text[:_MAX_TOOL_RESULT_CHARS] + "\n...[truncated]"
    return text


def _mcp_event(payload: dict[str, Any]) -> str:
    return f"event: mcp\ndata: {orjson.dumps(payload).decode()}\n\n"


async def _execute_tool_call(
    call: dict[str, Any],
    mapping: dict[str, McpToolRef],
) -> dict[str, Any]:
    call_id = str(call.get("id") or f"call_{uuid.uuid4().hex[:8]}")
    func = call.get("function") or {}
    function_name = str(func.get("name") or "")
    arguments = _tool_arguments(str(func.get("arguments") or ""))
    ref = mapping.get(function_name)
    if ref is None:
        return {
            "role": "tool",
            "tool_call_id": call_id,
            "content": f"MCP tool {function_name!r} is not available.",
        }
    async with McpStdioClient(ref.server) as client:
        raw_result = await client.call_tool(ref.tool_name, arguments)
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": _format_tool_result(raw_result),
    }


def _selected_mcp_options(req: ChatCompletionRequest) -> dict[str, Any]:
    raw = req.mcp if isinstance(req.mcp, dict) else {}
    enabled = bool(raw.get("enabled"))
    server_ids = raw.get("server_ids")
    if not isinstance(server_ids, list):
        server_ids = []
    auto = bool(raw.get("auto", True))
    tool_choice = str(raw.get("tool_choice") or "auto").lower()
    if tool_choice not in _VALID_TOOL_CHOICE:
        tool_choice = "auto"
    try:
        max_steps = int(raw.get("max_steps") or 2)
    except (TypeError, ValueError):
        max_steps = 2
    max_steps = min(max(max_steps, 1), _MAX_MODEL_TOOL_STEPS)
    return {
        "enabled": enabled,
        "auto": auto,
        "server_ids": [str(item) for item in server_ids if str(item).strip()],
        "tool_choice": tool_choice,
        "max_steps": max_steps,
    }


def should_handle_mcp(req: ChatCompletionRequest) -> bool:
    options = _selected_mcp_options(req)
    if not options["enabled"] or options["tool_choice"] == "none":
        return False
    spec = model_registry.get(req.model)
    return bool(spec and spec.enabled and spec.is_chat())


async def webui_chat_completions_with_mcp(
    req: ChatCompletionRequest,
    *,
    user: WebUIUser | None = None,
):
    spec = model_registry.get(req.model)
    if spec is None or not spec.enabled:
        raise ValidationError(
            f"Model {req.model!r} does not exist or you do not have access to it.",
            param="model",
            code="model_not_found",
        )
    upstream_headers = build_upstream_response_headers(spec)
    return StreamingResponse(
        _sse_with_heartbeat(_safe_sse(_mcp_agent_stream(req, user=user))),
        media_type="text/event-stream",
        headers={**_SSE_HEADERS, **upstream_headers},
    )


async def _mcp_agent_stream(
    req: ChatCompletionRequest,
    *,
    user: WebUIUser | None = None,
) -> AsyncGenerator[str, None]:
    options = _selected_mcp_options(req)
    servers = await (_load_servers(user) if user is not None else _load_servers())
    selected_ids = set(options["server_ids"])
    selected = [
        server
        for server in servers
        if server.get("enabled")
        and (options["auto"] or server.get("id") in selected_ids)
    ]
    if not selected:
        yield _mcp_event({"status": "empty", "message": "No enabled MCP servers selected"})
        selected = []

    tools, mapping = await _build_openai_tools(selected)
    if tools:
        yield _mcp_event({"status": "ready", "tool_count": len(tools)})
    elif selected:
        yield _mcp_event({"status": "empty", "message": "Selected MCP servers exposed no tools"})

    messages = [m.model_dump(exclude_none=True) for m in req.messages]
    if not tools:
        result = ToolPassResult()
        async for chunk in _model_pass_stream(
            req=req,
            messages=messages,
            tools=None,
            tool_choice=None,
            result=result,
        ):
            yield chunk
        return

    tool_choice: Any = options["tool_choice"]
    if tool_choice == "none":
        tool_choice = None
        tools = None

    for step in range(options["max_steps"]):
        pass_result = ToolPassResult()
        async for chunk in _model_pass_stream(
            req=req,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice if step == 0 else "auto",
            result=pass_result,
        ):
            yield chunk
        calls = pass_result.tool_calls or []
        if not calls:
            return

        messages.append(
            {
                "role": "assistant",
                "content": pass_result.content or "",
                "tool_calls": calls,
            }
        )
        for call in calls:
            func = call.get("function") or {}
            function_name = str(func.get("name") or "")
            ref = mapping.get(function_name)
            label = ref.tool_name if ref else function_name
            server_name = ref.server.get("name") if ref else ""
            yield _mcp_event(
                {
                    "status": "running",
                    "tool": label,
                    "server": server_name,
                    "step": step + 1,
                }
            )
            try:
                tool_message = await _execute_tool_call(call, mapping)
            except Exception as exc:
                logger.warning("mcp tool call failed: tool={} error={}", function_name, exc)
                tool_message = {
                    "role": "tool",
                    "tool_call_id": str(call.get("id") or ""),
                    "content": f"MCP tool call failed: {exc}",
                }
                yield _mcp_event(
                    {
                        "status": "error",
                        "tool": label,
                        "server": server_name,
                        "message": str(exc),
                    }
                )
            else:
                yield _mcp_event(
                    {
                        "status": "done",
                        "tool": label,
                        "server": server_name,
                    }
                )
            messages.append(tool_message)

    yield _mcp_event(
        {
            "status": "limit",
            "message": "MCP tool step limit reached; generating final answer without more tool calls",
        }
    )
    final_result = ToolPassResult()
    async for chunk in _model_pass_stream(
        req=req,
        messages=messages,
        tools=None,
        tool_choice=None,
        result=final_result,
    ):
        yield chunk


@router.get("/mcp/servers")
async def list_mcp_servers(user: WebUIUser = Depends(verify_webui_key)):
    servers = await _load_servers(user)
    return JSONResponse({"servers": servers})


@router.post("/mcp/servers")
async def create_mcp_server(
    config: McpServerConfig,
    user: WebUIUser = Depends(verify_webui_key),
):
    server = _server_from_model(config)
    servers = await _load_servers(user)
    if any(item.get("id") == server["id"] for item in servers):
        raise HTTPException(status_code=409, detail="MCP server id already exists")
    servers.append(server)
    await _save_servers(servers, user)
    return JSONResponse({"server": server})


@router.post("/mcp/servers/import")
async def import_mcp_servers(
    req: McpServerImportRequest,
    user: WebUIUser = Depends(verify_webui_key),
):
    imported, skipped = _parse_imported_servers(req.config)
    existing = await _load_servers(user)
    if not imported:
        return JSONResponse(
            {
                "created": 0,
                "updated": 0,
                "skipped": skipped,
                "servers": existing,
            }
        )
    servers, created, updated = _merge_imported_servers(existing, imported, replace=req.replace)
    await _save_servers(servers, user)
    return JSONResponse(
        {
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "servers": servers,
        }
    )


@router.put("/mcp/servers/{server_id}")
async def update_mcp_server(
    server_id: str,
    patch: McpServerPatch,
    user: WebUIUser = Depends(verify_webui_key),
):
    servers = await _load_servers(user)
    for index, server in enumerate(servers):
        if server.get("id") != server_id:
            continue
        next_server = dict(server)
        updates = patch.model_dump(exclude_none=True)
        next_server.update(updates)
        next_server["id"] = server_id
        next_server = _normalize_server(next_server)
        servers[index] = next_server
        await _save_servers(servers, user)
        return JSONResponse({"server": next_server})
    raise HTTPException(status_code=404, detail="MCP server not found")


@router.delete("/mcp/servers/{server_id}")
async def delete_mcp_server(
    server_id: str,
    user: WebUIUser = Depends(verify_webui_key),
):
    servers = await _load_servers(user)
    next_servers = [server for server in servers if server.get("id") != server_id]
    if len(next_servers) == len(servers):
        raise HTTPException(status_code=404, detail="MCP server not found")
    await _save_servers(next_servers, user)
    return JSONResponse({"status": "ok"})


@router.get("/mcp/tools")
async def list_mcp_tools(user: WebUIUser = Depends(verify_webui_key)):
    servers = [server for server in await _load_servers(user) if server.get("enabled")]
    result: list[dict[str, Any]] = []
    for server in servers:
        try:
            tools = await _discover_server_tools(server)
        except (AppError, Exception) as exc:
            result.append(
                {
                    "server_id": server.get("id"),
                    "server_name": server.get("name"),
                    "error": str(exc),
                    "tools": [],
                }
            )
            continue
        result.append(
            {
                "server_id": server.get("id"),
                "server_name": server.get("name"),
                "tools": tools,
            }
        )
    return JSONResponse({"servers": result})


__all__ = [
    "router",
    "should_handle_mcp",
    "webui_chat_completions_with_mcp",
]
