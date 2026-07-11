import asyncio
import unittest
from unittest.mock import patch

from app.control.model.registry import resolve
from app.dataplane.reverse.protocol.grok_build import (
    _headers,
    _restore_custom_tool_response,
    _restore_custom_tool_stream,
    _select_entry,
    sanitize_responses_payload,
)
from app.dataplane.reverse.protocol.xai_console import split_console_server_tools
from app.control.model.enums import ModeId
from app.products.openai.chat import _stream_chat
from app.products.openai.schemas import ResponsesCreateRequest


async def _collect_stream(source):
    return [chunk async for chunk in source]


class GrokBuildTests(unittest.TestCase):
    def test_model_uses_isolated_build_profile(self) -> None:
        spec = resolve("grok-4.5")

        self.assertTrue(spec.uses_grok_build_responses())
        self.assertFalse(spec.uses_console_responses())
        self.assertEqual(spec.upstream_model_name(), "grok-4.5")

    def test_grok_build_uses_native_function_tools(self) -> None:
        local_tools, upstream_tools = split_console_server_tools(
            [{
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "Lookup an order",
                    "parameters": {"type": "object"},
                },
            }],
            resolve("grok-4.5"),
        )

        self.assertIsNone(local_tools)
        self.assertEqual(upstream_tools, [{
            "type": "function",
            "name": "lookup",
            "description": "Lookup an order",
            "parameters": {"type": "object"},
        }])

    def test_composer_fast_uses_build_profile(self) -> None:
        spec = resolve("grok-composer-2.5-fast")

        self.assertTrue(spec.uses_grok_build_responses())
        self.assertEqual(spec.upstream_model_name(), "grok-composer-2.5-fast")

    def test_grok_43_build_uses_build_profile(self) -> None:
        spec = resolve("grok-4.3-build")

        self.assertTrue(spec.uses_grok_build_responses())
        self.assertEqual(spec.upstream_model_name(), "grok-4.3")

    def test_cli_headers_select_grok_45_without_sso_cookie(self) -> None:
        headers = _headers("access-token", "grok-4.5", True, "conv-1")

        self.assertEqual(headers["Authorization"], "Bearer access-token")
        self.assertEqual(headers["X-XAI-Token-Auth"], "xai-grok-cli")
        self.assertEqual(headers["x-grok-model-override"], "grok-4.5")
        self.assertEqual(headers["x-grok-conv-id"], "conv-1")
        self.assertEqual(headers["Accept"], "text/event-stream")
        self.assertNotIn("Cookie", headers)

    def test_grok_cli_auth_document_is_supported(self) -> None:
        key, entry = _select_entry({
            "https://auth.x.ai::client": {
                "key": "access-token",
                "refresh_token": "refresh-token",
            }
        })

        self.assertEqual(key, "https://auth.x.ai::client")
        self.assertEqual(entry["key"], "access-token")

    def test_responses_schema_preserves_future_codex_fields(self) -> None:
        req = ResponsesCreateRequest.model_validate({
            "model": "grok-4.5",
            "input": "hello",
            "prompt_cache_key": "cache-1",
            "text": {"verbosity": "high"},
        })

        payload = req.model_dump(exclude_none=True)
        self.assertEqual(payload["prompt_cache_key"], "cache-1")
        self.assertEqual(payload["text"], {"verbosity": "high"})

    def test_codex_developer_input_is_moved_to_instructions(self) -> None:
        payload = sanitize_responses_payload({
            "model": "grok-4.5",
            "input": [
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "Follow repo rules"}],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Hello"}],
                },
            ],
            "reasoning_effort": "high",
            "max_tokens": 100,
        })

        self.assertEqual(payload["instructions"], "Follow repo rules")
        self.assertEqual(len(payload["input"]), 1)
        self.assertEqual(payload["input"][0]["role"], "user")
        self.assertEqual(payload["reasoning"], {"effort": "high"})
        self.assertEqual(payload["max_output_tokens"], 100)
        self.assertNotIn("reasoning_effort", payload)
        self.assertNotIn("max_tokens", payload)

    def test_custom_tools_are_normalized_for_grok_build(self) -> None:
        payload = sanitize_responses_payload({
            "model": "grok-4.5",
            "input": [
                {
                    "type": "custom_tool_call",
                    "call_id": "call-1",
                    "name": "exec",
                    "input": "text('ok')",
                },
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call-1",
                    "output": "ok",
                },
            ],
            "tools": [{
                "type": "custom",
                "name": "exec",
                "description": "Run JavaScript",
                "format": {"type": "text"},
            }],
            "tool_choice": {"type": "custom", "name": "exec"},
        })

        tool = payload["tools"][0]
        self.assertEqual(tool["type"], "function")
        self.assertEqual(tool["parameters"]["required"], ["input"])
        self.assertTrue(tool["strict"])
        self.assertEqual(payload["tool_choice"], {"type": "function", "name": "exec"})
        self.assertEqual(payload["input"][0]["type"], "function_call")
        self.assertEqual(payload["input"][0]["arguments"], '{"input":"text(\'ok\')"}')
        self.assertEqual(payload["input"][1]["type"], "function_call_output")

    def test_codex_deferred_tools_are_flattened_for_grok_build(self) -> None:
        payload = sanitize_responses_payload({
            "model": "grok-4.5",
            "input": "hello",
            "tools": [
                {
                    "type": "function",
                    "name": "lookup",
                    "description": "Lookup an order",
                    "strict": False,
                    "defer_loading": True,
                    "parameters": {"type": "object"},
                },
                {
                    "type": "namespace",
                    "name": "inventory",
                    "description": "Inventory tools",
                    "tools": [{
                        "type": "function",
                        "name": "reserve",
                        "description": "Reserve stock",
                        "strict": False,
                        "defer_loading": True,
                        "parameters": {"type": "object"},
                    }],
                },
                {
                    "type": "tool_search",
                    "execution": "client",
                    "description": "Find deferred tools",
                    "parameters": {"type": "object"},
                },
            ],
        })

        self.assertEqual(
            [tool["name"] for tool in payload["tools"]],
            ["lookup", "reserve"],
        )
        self.assertTrue(all(tool["type"] == "function" for tool in payload["tools"]))
        self.assertTrue(all("defer_loading" not in tool for tool in payload["tools"]))

    def test_codex_web_search_and_internal_metadata_are_sanitized(self) -> None:
        payload = sanitize_responses_payload({
            "model": "grok-4.5",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                    "internal_chat_message_metadata_passthrough": {"turn_id": "turn-1"},
                },
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": "checking"}],
                    "content": None,
                    "encrypted_content": None,
                },
            ],
            "tools": [{
                "type": "web_search",
                "external_web_access": False,
                "indexed_web_access": True,
                "search_content_types": ["text"],
                "search_context_size": "medium",
            }],
        })

        self.assertNotIn(
            "internal_chat_message_metadata_passthrough",
            payload["input"][0],
        )
        self.assertEqual(len(payload["input"]), 1)
        self.assertEqual(payload["tools"], [{
            "type": "web_search",
            "search_context_size": "medium",
        }])

    def test_custom_tool_response_is_restored_for_codex(self) -> None:
        restored = _restore_custom_tool_response({
            "id": "resp-1",
            "output": [{
                "id": "fc-1",
                "type": "function_call",
                "call_id": "call-1",
                "name": "exec",
                "arguments": '{"input":"text(1)"}',
                "status": "completed",
            }],
        }, {"exec"})

        item = restored["output"][0]
        self.assertEqual(item["type"], "custom_tool_call")
        self.assertEqual(item["input"], "text(1)")
        self.assertNotIn("arguments", item)

    def test_custom_tool_stream_events_are_restored_for_codex(self) -> None:
        async def source():
            yield (
                'event: response.output_item.added\n'
                'data: {"type":"response.output_item.added","item":'
            )
            yield (
                '{"id":"fc-1","type":"function_call","call_id":"call-1",'
                '"name":"exec","arguments":"","status":"in_progress"}}\n\n'
                'event: response.function_call_arguments.delta\n'
                'data: {"type":"response.function_call_arguments.delta",'
                '"sequence_number":4,"output_index":0,"item_id":"fc-1",'
                '"delta":"{\\"input\\":\\"text(1)\\"}"}\n\n'
                'event: response.function_call_arguments.done\n'
                'data: {"type":"response.function_call_arguments.done",'
                '"sequence_number":5,"output_index":0,"item_id":"fc-1",'
                '"name":"exec",'
                '"arguments":"{\\"input\\":\\"text(1)\\"}"}\n\n'
                'data: [DONE]\n\n'
            )

        chunks = asyncio.run(_collect_stream(
            _restore_custom_tool_stream(source(), {"exec"})
        ))
        output = "".join(chunks)

        self.assertIn('"type":"custom_tool_call"', output)
        self.assertIn("event: response.custom_tool_call_input.delta", output)
        self.assertIn("event: response.custom_tool_call_input.done", output)
        self.assertNotIn('"delta":"{\\"input\\"', output)
        self.assertIn('"delta":""', output)
        self.assertIn('"sequence_number":4', output)
        self.assertIn('"input":"text(1)"', output)
        self.assertNotIn('"name":"exec","input":"text(1)"', output)
        self.assertIn("data: [DONE]", output)

    def test_chat_compatibility_uses_build_transport(self) -> None:
        async def fake_post(payload, *, model, stream):
            self.assertEqual(payload["model"], "grok-4.5")
            self.assertEqual(model, "grok-4.5")
            self.assertTrue(stream)

            async def chunks():
                yield 'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
                yield 'data: {"type":"response.completed","response":{}}\n\n'

            return chunks()

        async def run():
            with patch(
                "app.dataplane.reverse.protocol.grok_build.post_responses",
                new=fake_post,
            ):
                return [line async for line in _stream_chat(
                    token="unused-sso-token",
                    mode_id=ModeId.CONSOLE,
                    message="hello",
                    files=[],
                    spec=resolve("grok-4.5"),
                )]

        lines = asyncio.run(run())
        self.assertTrue(any("response.output_text.delta" in line for line in lines))
        self.assertTrue(any("response.completed" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
