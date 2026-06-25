import asyncio
import unittest
from unittest.mock import patch

import orjson

from app.dataplane.account.lease import AccountLease
from app.control.model.enums import ModeId, Tier
from app.control.model.registry import list_enabled, resolve
from app.dataplane.reverse.planner import build_plan
from app.dataplane.reverse.protocol.xai_console import (
    ConsoleResponsesStreamAdapter,
    build_console_responses_payload,
    client_function_tool_names,
    console_tool_choice_override,
    split_console_server_tools,
)
from app.dataplane.reverse.runtime.endpoint_table import CHAT, CONSOLE_RESPONSES
from app.products.openai.router import _openai_model_payload


class _FakeConfig:
    def __init__(self, values: dict[str, object] | None = None) -> None:
        self._values = values or {}

    def get(self, _key, default=None):
        return self._values.get(_key, default)

    def get_bool(self, _key, default=False):
        return bool(self._values.get(_key, default))

    def get_float(self, _key, default=0.0):
        return float(self._values.get(_key, default))

    def get_int(self, _key, default=0):
        return int(self._values.get(_key, default))

    def get_list(self, _key, default=None):
        return list(self._values.get(_key, default or []))


class _FakeAccountDirectory:
    async def release(self, _acct):
        return None

    async def feedback(self, *_args, **_kwargs):
        return None


class _FakeProxyRuntime:
    async def acquire(self, **_kwargs):
        return None


class _FakeConsoleResponse:
    status_code = 200
    content = b""

    async def aiter_lines(self):
        yield 'data: {"type":"response.output_text.delta","delta":"ok"}'
        yield 'data: {"type":"response.completed","response":{}}'
        yield "data: [DONE]"


class _FakeChatResponse:
    status_code = 200
    content = b""

    async def aiter_lines(self):
        yield 'data: {"result":{"response":{"token":"ok","messageTag":"final"}}}'
        yield 'data: {"result":{"response":{"isSoftStop":true}}}'
        yield "data: [DONE]"


class _CaptureSession:
    def __init__(self, capture: dict[str, object], **_kwargs) -> None:
        self._capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, endpoint, *, headers, data, timeout, stream):
        self._capture["endpoint"] = endpoint
        self._capture["headers"] = headers
        self._capture["payload"] = orjson.loads(data)
        self._capture["timeout"] = timeout
        self._capture["stream"] = stream
        if endpoint == CONSOLE_RESPONSES:
            return _FakeConsoleResponse()
        return _FakeChatResponse()


async def _noop_quota_sync(*_args, **_kwargs):
    return None


async def _fake_prepare_file_attachments(*_args, **_kwargs):
    return ["uploaded-file-id"]


class ConsoleModelRoutingTests(unittest.TestCase):
    AVAILABLE_CHAT_MODELS = (
        "grok-4.3",
        "grok-4.20-0309-non-reasoning",
        "grok-4.20-0309",
        "grok-4.20-0309-reasoning",
        "grok-4.20-0309-non-reasoning-super",
        "grok-4.20-0309-super",
        "grok-4.20-0309-reasoning-super",
        "grok-4.20-0309-non-reasoning-heavy",
        "grok-4.20-0309-heavy",
        "grok-4.20-0309-reasoning-heavy",
        "grok-4.20-multi-agent-0309",
        "grok-4.20-fast",
        "grok-4.3-fast",
        "grok-4.20-auto",
        "grok-4.20-expert",
        "grok-4.20-heavy",
        "grok-4.20-0309-console",
        "grok-4.20-0309-non-reasoning-console",
        "grok-4.20-0309-reasoning-console",
        "grok-4.20-multi-agent-console",
        "grok-4.20-multi-agent-xhigh",
        "grok-4.20-multi-agent-high",
        "grok-4.20-multi-agent-medium",
        "grok-4.20-multi-agent-low",
        "grok-4.3-console",
        "grok-4.3-high",
        "grok-4.3-medium",
        "grok-4.3-low",
        "grok-build-console",
        "grok-composer-2.5-fast",
        "grok-4.3-beta",
    )
    AVAILABLE_MEDIA_MODELS = (
        "grok-imagine-image-lite",
        "grok-imagine-image",
        "grok-imagine-image-pro",
        "grok-imagine-image-edit",
        "grok-imagine-video",
    )

    def test_chat_models_are_registered_and_enabled(self) -> None:
        enabled_ids = {spec.model_name for spec in list_enabled()}

        for model in self.AVAILABLE_CHAT_MODELS:
            with self.subTest(model=model):
                spec = resolve(model)
                self.assertIn(model, enabled_ids)
                self.assertTrue(spec.is_chat())

    def test_media_models_stay_visible_for_runtime_account_retry(self) -> None:
        enabled_ids = {spec.model_name for spec in list_enabled()}

        for model in self.AVAILABLE_MEDIA_MODELS:
            with self.subTest(model=model):
                spec = resolve(model)
                self.assertIn(model, enabled_ids)
                self.assertTrue(spec.is_image() or spec.is_image_edit() or spec.is_video())

    def test_model_payload_reports_pool_availability_instead_of_hiding(self) -> None:
        spec = resolve("grok-4.3-beta")

        payload = _openai_model_payload(spec, created=123, available_pools=frozenset({"basic"}))

        self.assertEqual(payload["id"], "grok-4.3-beta")
        self.assertEqual(payload["availability"]["status"], "unavailable")
        self.assertIn("super", payload["availability"]["required_pools"])
        self.assertIn("heavy", payload["availability"]["required_pools"])
        self.assertIn("pool", payload["availability"]["reason"])
        self.assertEqual(payload["routing"]["upstream_profile"], "grok_web")

    def test_grok_43_uses_free_sso_console_responses_route(self) -> None:
        spec = resolve("grok-4.3")

        self.assertEqual(spec.tier, Tier.BASIC)
        self.assertEqual(spec.mode_id, ModeId.CONSOLE)
        self.assertTrue(spec.uses_console_responses())
        self.assertEqual(spec.upstream_model_name(), "grok-4.3")

        plan = build_plan(spec, {})
        self.assertEqual(plan.endpoint, CONSOLE_RESPONSES)
        self.assertEqual(plan.origin, "https://console.x.ai")
        self.assertEqual(plan.referer, "https://console.x.ai/")
        self.assertEqual(plan.extra["upstream_model"], "grok-4.3")

    def test_grok_420_auto_and_expert_require_paid_pools(self) -> None:
        for model, mode in (
            ("grok-4.20-auto", ModeId.AUTO),
            ("grok-4.20-expert", ModeId.EXPERT),
        ):
            with self.subTest(model=model):
                spec = resolve(model)
                payload = _openai_model_payload(
                    spec,
                    created=123,
                    available_pools=frozenset({"basic"}),
                )

                self.assertEqual(spec.tier, Tier.SUPER)
                self.assertEqual(spec.mode_id, mode)
                self.assertEqual(spec.pool_candidates(), (2, 1))
                self.assertEqual(payload["availability"]["status"], "unavailable")
                self.assertEqual(
                    payload["availability"]["required_pools"],
                    ["heavy", "super"],
                )

    def test_console_effort_aliases_route_to_real_upstream_models(self) -> None:
        expected = {
            "grok-4.20-0309-console": "grok-4.20-0309",
            "grok-4.20-0309-reasoning-console": "grok-4.20-0309-reasoning",
            "grok-4.20-0309-non-reasoning-console": "grok-4.20-0309-non-reasoning",
            "grok-4.20-multi-agent-console": "grok-4.20-multi-agent",
            "grok-4.20-multi-agent-xhigh": "grok-4.20-multi-agent",
            "grok-4.20-multi-agent-high": "grok-4.20-multi-agent",
            "grok-4.20-multi-agent-medium": "grok-4.20-multi-agent",
            "grok-4.20-multi-agent-low": "grok-4.20-multi-agent",
            "grok-4.3-console": "grok-4.3",
            "grok-4.3-high": "grok-4.3",
            "grok-4.3-medium": "grok-4.3",
            "grok-4.3-low": "grok-4.3",
            "grok-build-console": "grok-build-0.1",
            "grok-composer-2.5-fast": "grok-4.3",
        }

        for public_model, upstream_model in expected.items():
            with self.subTest(model=public_model):
                spec = resolve(public_model)

                self.assertTrue(spec.uses_console_responses())
                self.assertEqual(spec.upstream_model_name(), upstream_model)

    def test_composer_fast_uses_text_trigger_alias(self) -> None:
        spec = resolve("grok-composer-2.5-fast")

        payload = build_console_responses_payload(
            model=spec.upstream_model_name(),
            public_model=spec.model_name,
            spec=spec,
            message="[user]: write a short note",
            stream=True,
        )

        self.assertEqual(payload["model"], "grok-4.3")
        self.assertEqual(
            payload["input"],
            "[user]: grok-composer-2.5-fast\n\n[user]: write a short note",
        )
        self.assertIn("grok-composer-2.5-fast", payload["instructions"])

    def test_composer_fast_upstream_model_can_be_configured(self) -> None:
        spec = resolve("grok-composer-2.5-fast")

        def fake_get_config(key: str, default=None):
            if key == "models.composer_fast_upstream_model":
                return "grok-4.20-multi-agent"
            return default

        with patch("app.platform.config.snapshot.get_config", side_effect=fake_get_config):
            payload = build_console_responses_payload(
                model=spec.upstream_model_name(),
                public_model=spec.model_name,
                spec=spec,
                message="[user]: hi",
                stream=True,
            )

        self.assertEqual(payload["model"], "grok-4.20-multi-agent")
        self.assertEqual(
            payload["input"],
            "[user]: grok-composer-2.5-fast\n\n[user]: hi",
        )

    def test_console_reasoning_effort_payload_policy(self) -> None:
        cases = (
            ("grok-4.3-console", None, "medium"),
            ("grok-4.3-console", "high", "high"),
            ("grok-4.3-console", "none", None),
            ("grok-4.3-low", "high", "low"),
            ("grok-4.3-medium", None, "medium"),
            ("grok-4.3-high", "low", "high"),
            ("grok-4.20-multi-agent-console", None, "medium"),
            ("grok-4.20-multi-agent-console", "xhigh", "xhigh"),
            ("grok-4.20-multi-agent-low", "high", "low"),
            ("grok-4.20-multi-agent-medium", None, "medium"),
            ("grok-4.20-multi-agent-high", "low", "high"),
            ("grok-4.20-multi-agent-xhigh", None, "xhigh"),
            ("grok-4.20-0309-console", "high", None),
            ("grok-4.20-0309-reasoning-console", None, None),
            ("grok-4.20-0309-non-reasoning-console", None, None),
            ("grok-build-console", "high", None),
            ("grok-composer-2.5-fast", "high", None),
        )

        for model, requested_effort, expected_effort in cases:
            with self.subTest(model=model, requested_effort=requested_effort):
                spec = resolve(model)
                request_overrides = (
                    {"_reasoning_effort": requested_effort}
                    if requested_effort is not None
                    else None
                )

                payload = build_console_responses_payload(
                    model=spec.upstream_model_name(),
                    public_model=spec.model_name,
                    spec=spec,
                    message="[user]: hi",
                    stream=True,
                    request_overrides=request_overrides,
                )

                if expected_effort is None:
                    self.assertNotIn("reasoning", payload)
                else:
                    self.assertEqual(payload["reasoning"], {"effort": expected_effort})

    def test_grok_42_reasoning_is_not_registered(self) -> None:
        # grok-4.2-reasoning / grok-4.2reasoning were stale registry entries:
        # xAI Console returns 404 for those model names (no "4.2" line exists
        # on the xAI roadmap; releases went 4.20 -> 4.3). They must no longer
        # be exposed via /v1/models or resolvable via the registry.
        with self.assertRaises(ValueError):
            resolve("grok-4.2-reasoning")
        with self.assertRaises(ValueError):
            resolve("grok-4.2reasoning")

    def test_build_console_responses_payload(self) -> None:
        payload = build_console_responses_payload(
            model="grok-4.3",
            message="[user]: hi",
            stream=True,
            request_overrides={"temperature": 0.2, "stream": None},
        )

        self.assertEqual(payload["model"], "grok-4.3")
        self.assertEqual(payload["input"], "[user]: hi")
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["temperature"], 0.2)

    def test_console_tools_are_forwarded_as_native_tools(self) -> None:
        spec = resolve("grok-4.3")
        local_tools, console_tools = split_console_server_tools(
            [
                {"type": "web_search"},
                {"type": "x_search", "enable_video_understanding": True},
                {
                    "type": "function",
                    "function": {"name": "lookup", "parameters": {"type": "object"}},
                },
            ],
            spec,
        )

        self.assertEqual(
            console_tools,
            [
                {"type": "web_search"},
                {"type": "x_search", "enable_video_understanding": True},
                {
                    "type": "function",
                    "name": "lookup",
                    "parameters": {"type": "object"},
                },
            ],
        )
        self.assertIsNone(local_tools)

    def test_console_search_tools_are_not_split_for_grok_web_models(self) -> None:
        spec = resolve("grok-4.20-fast")

        local_tools, console_tools = split_console_server_tools(
            [{"type": "web_search"}],
            spec,
        )

        self.assertEqual(local_tools, [{"type": "web_search"}])
        self.assertEqual(console_tools, [])

    def test_console_function_named_search_tools_are_mapped_to_server_tools(self) -> None:
        spec = resolve("grok-4.3")

        local_tools, console_tools = split_console_server_tools(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "description": "Search the web",
                        "parameters": {"type": "object"},
                        "search_context_size": "high",
                    },
                },
                {
                    "type": "function",
                    "function": {"name": "x_search", "parameters": {"type": "object"}},
                    "enable_video_understanding": True,
                },
            ],
            spec,
        )

        self.assertIsNone(local_tools)
        self.assertEqual(
            console_tools,
            [
                {"type": "web_search", "search_context_size": "high"},
                {"type": "x_search", "enable_video_understanding": True},
            ],
        )

    def test_console_tool_choice_defaults_to_auto_for_server_side_tools(self) -> None:
        self.assertEqual(console_tool_choice_override(None), "auto")
        self.assertEqual(console_tool_choice_override("required"), "required")
        self.assertEqual(
            console_tool_choice_override(
                {"type": "function", "function": {"name": "web_search"}},
            ),
            {"type": "web_search"},
        )
        self.assertEqual(
            console_tool_choice_override(
                {"type": "function", "function": {"name": "x_search"}},
            ),
            {"type": "x_search"},
        )
        self.assertEqual(
            console_tool_choice_override(
                {"type": "function", "function": {"name": "lookup"}},
            ),
            {"type": "function", "name": "lookup"},
        )
        self.assertEqual(
            console_tool_choice_override(
                {"type": "function", "function": {"name": "lookup"}},
                local_tools=[
                    {
                        "type": "function",
                        "function": {"name": "lookup"},
                    }
                ],
            ),
            {"type": "function", "name": "lookup"},
        )

    def test_build_console_responses_payload_passes_search_tools(self) -> None:
        payload = build_console_responses_payload(
            model="grok-4.3",
            message="[user]: hi",
            stream=True,
            request_overrides={
                "tools": [{"type": "web_search"}, {"type": "x_search"}],
                "tool_choice": "auto",
            },
        )

        self.assertEqual(
            payload["tools"],
            [{"type": "web_search"}, {"type": "x_search"}],
        )
        self.assertEqual(payload["tool_choice"], "auto")

    def test_build_console_responses_payload_maps_deepsearch_to_search_tools(self) -> None:
        payload = build_console_responses_payload(
            model="grok-4.3",
            message="[user]: search latest xAI news",
            stream=True,
            request_overrides={
                "temporary": True,
                "disableMemory": True,
                "deepsearchPreset": "default",
            },
        )

        self.assertEqual(
            payload["tools"],
            [{"type": "web_search"}, {"type": "x_search"}],
        )
        self.assertEqual(payload["tool_choice"], "auto")
        self.assertNotIn("deepsearchPreset", payload)

    def test_build_console_responses_payload_preserves_explicit_search_tools(self) -> None:
        payload = build_console_responses_payload(
            model="grok-4.3",
            message="[user]: search latest xAI news",
            stream=True,
            request_overrides={
                "deepsearchPreset": "deeper",
                "tools": [{"type": "web_search", "search_context_size": "high"}],
                "tool_choice": "required",
            },
        )

        self.assertEqual(
            payload["tools"],
            [
                {"type": "web_search", "search_context_size": "high"},
                {"type": "x_search"},
            ],
        )
        self.assertEqual(payload["tool_choice"], "required")
        self.assertNotIn("deepsearchPreset", payload)

    def test_build_console_responses_payload_maps_tool_call_history(self) -> None:
        payload = build_console_responses_payload(
            model="grok-4.3",
            message="ignored when messages are structured",
            stream=True,
            messages=[
                {"role": "user", "content": "lookup order"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "arguments": '{"order_id":"A1"}',
                        },
                    }],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "shipped"},
            ],
        )

        self.assertEqual(
            payload["input"],
            [
                {"role": "user", "content": [{"type": "input_text", "text": "lookup order"}]},
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "lookup",
                    "arguments": '{"order_id":"A1"}',
                    "status": "completed",
                },
                {"type": "function_call_output", "call_id": "call_1", "output": "shipped"},
            ],
        )

    def test_chat_completions_deepsearch_reaches_console_payload_as_tools(self) -> None:
        capture = self._run_console_chat_capture(
            request_overrides={"deepsearchPreset": "default"},
        )

        self.assertEqual(capture["endpoint"], CONSOLE_RESPONSES)
        self.assertEqual(
            capture["payload"]["tools"],
            [{"type": "web_search"}, {"type": "x_search"}],
        )
        self.assertEqual(capture["payload"]["tool_choice"], "auto")
        self.assertNotIn("deepsearchPreset", capture["payload"])

    def test_chat_completions_console_default_search_disabled_by_default(self) -> None:
        capture = self._run_console_chat_capture(
            messages=[{"role": "user", "content": "latest xAI news"}],
        )

        self.assertEqual(capture["endpoint"], CONSOLE_RESPONSES)
        self.assertNotIn("tools", capture["payload"])
        self.assertNotIn("tool_choice", capture["payload"])

    def test_chat_completions_console_default_search_config_adds_tools(self) -> None:
        capture = self._run_console_chat_capture(
            messages=[{"role": "user", "content": "latest xAI news"}],
            config_values={"features.console_default_search": True},
        )

        self.assertEqual(capture["endpoint"], CONSOLE_RESPONSES)
        self.assertEqual(
            capture["payload"]["tools"],
            [{"type": "web_search"}, {"type": "x_search"}],
        )
        self.assertEqual(capture["payload"]["tool_choice"], "auto")

    def test_console_default_search_preserves_explicit_search_tool_options(self) -> None:
        capture = self._run_console_chat_capture(
            tools=[{"type": "web_search", "search_context_size": "high"}],
            config_values={"features.console_default_search": True},
        )

        self.assertEqual(
            capture["payload"]["tools"],
            [
                {"type": "web_search", "search_context_size": "high"},
                {"type": "x_search"},
            ],
        )
        self.assertEqual(capture["payload"]["tool_choice"], "auto")

    def test_chat_completions_explicit_search_tools_reach_console_payload(self) -> None:
        capture = self._run_console_chat_capture(
            tools=[
                {"type": "web_search"},
                {"type": "x_search", "enable_video_understanding": True},
                {
                    "type": "function",
                    "function": {"name": "lookup", "parameters": {"type": "object"}},
                },
            ],
            tool_choice="auto",
        )

        self.assertEqual(
            capture["payload"]["tools"],
            [
                {"type": "web_search"},
                {"type": "x_search", "enable_video_understanding": True},
                {
                    "type": "function",
                    "name": "lookup",
                    "parameters": {"type": "object"},
                },
            ],
        )
        self.assertEqual(capture["payload"]["tool_choice"], "auto")
        self.assertEqual(
            [tool["type"] for tool in capture["payload"].get("tools", [])],
            ["web_search", "x_search", "function"],
        )

    def test_console_native_adapter_filters_internal_tool_calls(self) -> None:
        adapter = ConsoleResponsesStreamAdapter(function_tool_names={"lookup"})

        ignored = adapter.feed(orjson.dumps({
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "id": "builtin",
                "type": "function_call",
                "call_id": "call_builtin",
                "name": "web_search_with_snippets",
                "arguments": '{"query":"xai"}',
            },
        }).decode())
        emitted = adapter.feed(orjson.dumps({
            "type": "response.output_item.done",
            "output_index": 1,
            "item": {
                "id": "fc_1",
                "type": "function_call",
                "call_id": "call_1",
                "name": "lookup",
                "arguments": '{"id":"A1"}',
            },
        }).decode())

        self.assertEqual(ignored, [])
        self.assertEqual(client_function_tool_names([
            {"type": "function", "function": {"name": "lookup"}},
            {"type": "function", "function": {"name": "web_search"}},
        ]), {"lookup"})
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].kind, "tool_calls")
        self.assertEqual(emitted[0].tool_calls[0].name, "lookup")
        self.assertEqual(emitted[0].tool_calls[0].arguments, '{"id":"A1"}')

    def test_console_native_adapter_keeps_arguments_delta_before_item_id(self) -> None:
        adapter = ConsoleResponsesStreamAdapter(function_tool_names={"lookup"})

        adapter.feed(orjson.dumps({
            "type": "response.function_call_arguments.delta",
            "output_index": 0,
            "delta": '{"id"',
        }).decode())
        adapter.feed(orjson.dumps({
            "type": "response.function_call_arguments.delta",
            "output_index": 0,
            "delta": ':"A1"}',
        }).decode())
        emitted = adapter.feed(orjson.dumps({
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "id": "fc_1",
                "type": "function_call",
                "call_id": "call_1",
                "name": "lookup",
                "arguments": '{"id":"A1"}',
            },
        }).decode())

        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].tool_calls[0].arguments, '{"id":"A1"}')

    def test_chat_completions_function_named_search_tools_reach_console_payload(self) -> None:
        capture = self._run_console_chat_capture(
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "parameters": {"type": "object"},
                        "search_context_size": "high",
                    },
                },
                {
                    "type": "function",
                    "function": {"name": "x_search", "parameters": {"type": "object"}},
                    "enable_video_understanding": True,
                },
            ],
            tool_choice={"type": "function", "function": {"name": "web_search"}},
        )

        self.assertEqual(
            capture["payload"]["tools"],
            [
                {"type": "web_search", "search_context_size": "high"},
                {"type": "x_search", "enable_video_understanding": True},
            ],
        )
        self.assertEqual(capture["payload"]["tool_choice"], {"type": "web_search"})

    def test_chat_completions_image_attachment_falls_back_to_grok_chat(self) -> None:
        image = "data:image/png;base64,AA=="
        capture = self._run_console_chat_capture(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe this image"},
                        {"type": "image_url", "image_url": {"url": image}},
                    ],
                }
            ],
            request_overrides={
                "_reasoning_effort": "xhigh",
                "tools": [{"type": "web_search"}],
                "temporary": True,
            },
        )

        self.assertEqual(capture["endpoint"], CHAT)
        self.assertEqual(capture["payload"]["message"], "[user]: describe this image")
        self.assertEqual(capture["payload"]["modeId"], ModeId.FAST.to_api_str())
        self.assertEqual(capture["payload"]["fileAttachments"], ["uploaded-file-id"])
        self.assertTrue(capture["payload"]["temporary"])
        self.assertNotIn("_reasoning_effort", capture["payload"])
        self.assertNotIn("tools", capture["payload"])

    def _run_console_chat_capture(
        self,
        *,
        messages: list[dict] | None = None,
        tools: list[dict] | None = None,
        tool_choice=None,
        request_overrides: dict | None = None,
        config_values: dict[str, object] | None = None,
    ) -> dict[str, object]:
        from app.products.openai import chat

        capture: dict[str, object] = {}
        account = AccountLease(
            lease_id=1,
            idx=0,
            token="test-sso-token",
            pool_id=int(Tier.BASIC),
            mode_id=int(ModeId.CONSOLE),
            selected_at=0,
        )

        async def fake_reserve_account(*_args, **_kwargs):
            return account, int(ModeId.CONSOLE)

        async def run():
            return await chat.completions(
                model="grok-4.3",
                messages=messages
                or [{"role": "user", "content": "search latest xAI news"}],
                stream=False,
                emit_think=False,
                tools=tools,
                tool_choice=tool_choice,
                request_overrides=request_overrides,
            )

        with (
            patch("app.dataplane.account._directory", _FakeAccountDirectory()),
            patch.object(chat, "get_config", return_value=_FakeConfig(config_values)),
            patch.object(chat, "selection_max_retries", return_value=0),
            patch.object(chat, "reserve_account", side_effect=fake_reserve_account),
            patch.object(chat, "get_proxy_runtime", return_value=_FakeProxyRuntime()),
            patch.object(chat, "build_session_kwargs", return_value={}),
            patch.object(
                chat,
                "build_http_headers",
                return_value={"authorization": "Bearer test"},
            ),
            patch.object(
                chat,
                "ResettableSession",
                side_effect=lambda **kwargs: _CaptureSession(capture, **kwargs),
            ),
            patch.object(chat, "_quota_sync", side_effect=_noop_quota_sync),
            patch.object(chat, "_fail_sync", side_effect=_noop_quota_sync),
            patch.object(
                chat,
                "_prepare_file_attachments",
                side_effect=_fake_prepare_file_attachments,
            ),
        ):
            result = asyncio.run(run())

        self.assertEqual(
            ((result.get("choices") or [{}])[0].get("message") or {}).get("content"),
            "ok",
        )
        return capture

    def test_build_console_responses_payload_pins_public_model_identity(self) -> None:
        payload = build_console_responses_payload(
            model="grok-4.3",
            message="[user]: 你是什么模型？",
            stream=True,
        )

        instructions = payload.get("instructions")
        self.assertIsInstance(instructions, str)
        self.assertIn("Grok 4.3", instructions)
        self.assertIn("grok-4.3", instructions)
        self.assertIn("Do not identify yourself as Grok 1.5", instructions)

    def test_console_responses_stream_adapter_parses_text_and_reasoning(self) -> None:
        adapter = ConsoleResponsesStreamAdapter()

        thinking = adapter.feed(
            '{"type":"response.reasoning_summary_text.delta","delta":"checking"}'
        )
        text = adapter.feed('{"type":"response.output_text.delta","delta":"done"}')
        completed = adapter.feed('{"type":"response.completed","response":{}}')

        self.assertEqual([(ev.kind, ev.content) for ev in thinking], [("thinking", "checking")])
        self.assertEqual([(ev.kind, ev.content) for ev in text], [("text", "done")])
        self.assertEqual([(ev.kind, ev.content) for ev in completed], [("soft_stop", "")])
        self.assertEqual(adapter.thinking_buf, ["checking"])
        self.assertEqual(adapter.text_buf, ["done"])

    def test_console_responses_stream_adapter_parses_image_generation_result(self) -> None:
        adapter = ConsoleResponsesStreamAdapter()

        events = adapter.feed(
            '{"type":"response.output_item.done","item":{'
            '"id":"ig_123","type":"image_generation_call","result":"'
            + ("a" * 120)
            + '"}}'
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "image")
        self.assertTrue(events[0].content.startswith("data:image/png;base64,"))
        self.assertEqual(adapter.image_urls, [(events[0].content, "ig_123")])

    def test_console_responses_stream_adapter_parses_image_generation_url_result(self) -> None:
        adapter = ConsoleResponsesStreamAdapter()

        events = adapter.feed(
            '{"type":"response.image_generation_call.completed",'
            '"item_id":"ig_123",'
            '"result":"https://imgen.x.ai/generated/image-content?token=abc"}'
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "image")
        self.assertEqual(
            events[0].content,
            "https://imgen.x.ai/generated/image-content?token=abc",
        )
        self.assertEqual(adapter.image_urls, [(events[0].content, "ig_123")])

    def test_console_responses_stream_adapter_extracts_markdown_image_delta(self) -> None:
        adapter = ConsoleResponsesStreamAdapter()

        events = adapter.feed(
            '{"type":"response.output_text.delta",'
            '"delta":"Here is the image: ![image](https://imgen.x.ai/generated/image-content?token=abc)"}'
        )

        self.assertEqual([(ev.kind, ev.content) for ev in events], [
            ("image", "https://imgen.x.ai/generated/image-content?token=abc"),
            ("text", "Here is the image: "),
        ])
        self.assertEqual(adapter.text_buf, ["Here is the image: "])
        self.assertEqual(
            adapter.image_urls,
            [("https://imgen.x.ai/generated/image-content?token=abc", events[0].image_id)],
        )

    def test_console_responses_stream_adapter_extracts_bare_grok_image_delta(self) -> None:
        adapter = ConsoleResponsesStreamAdapter()
        url = "https://grok.x.ai/generated-image-city-skyline-dawn-mist-cinematic-wide.jpg"

        events = adapter.feed(
            '{"type":"response.output_text.delta",'
            f'"delta":"Generated: {url}."'
            '}'
        )

        self.assertEqual([(ev.kind, ev.content) for ev in events], [
            ("image", url),
            ("text", "Generated: ."),
        ])
        self.assertEqual(adapter.image_urls, [(url, events[0].image_id)])

    def test_console_responses_stream_adapter_extracts_split_bare_grok_image_after_join(self) -> None:
        adapter = ConsoleResponsesStreamAdapter()

        adapter.feed(
            '{"type":"response.output_text.delta",'
            '"delta":"Generated: https://grok.x.ai/generated-image-shanghai-"}'
        )
        adapter.feed(
            '{"type":"response.output_text.delta",'
            '"delta":"pudong-golden-hour-realistic-cinematic-wide.jpg"}'
        )
        cleaned = adapter.extract_generated_images_from_text("".join(adapter.text_buf))

        self.assertEqual(cleaned, "Generated: ")
        self.assertEqual(
            adapter.image_urls[0][0],
            "https://grok.x.ai/generated-image-shanghai-pudong-golden-hour-realistic-cinematic-wide.jpg",
        )

    def test_console_responses_stream_adapter_does_not_emit_partial_image_url(self) -> None:
        adapter = ConsoleResponsesStreamAdapter()

        first_events = adapter.feed(
            '{"type":"response.output_text.delta",'
            '"delta":"Generated: https://grok.x.ai/generated-image-shanghai-"}'
        )
        second_events = adapter.feed(
            '{"type":"response.output_text.delta",'
            '"delta":"pudong-golden-hour-realistic-cinematic-wide.jpg"}'
        )

        self.assertEqual([(ev.kind, ev.content) for ev in first_events], [
            ("text", "Generated: "),
        ])
        self.assertEqual([(ev.kind, ev.content) for ev in second_events], [
            (
                "image",
                "https://grok.x.ai/generated-image-shanghai-pudong-golden-hour-realistic-cinematic-wide.jpg",
            ),
        ])
        self.assertEqual(adapter.text_buf, ["Generated: "])
        self.assertEqual(
            adapter.image_urls[0][0],
            "https://grok.x.ai/generated-image-shanghai-pudong-golden-hour-realistic-cinematic-wide.jpg",
        )

    def test_console_responses_stream_adapter_parses_completed_image_url(self) -> None:
        adapter = ConsoleResponsesStreamAdapter()

        events = adapter.feed(
            '{"type":"response.completed","response":{"output":[{'
            '"type":"output_image","image_url":"/images/123e4567-e89b-12d3-a456-426614174000.jpg"'
            '}]}}'
        )

        image_events = [ev for ev in events if ev.kind == "image"]
        self.assertEqual(len(image_events), 1)
        self.assertEqual(
            image_events[0].content,
            "https://assets.grok.com/images/123e4567-e89b-12d3-a456-426614174000.jpg",
        )
        self.assertIn(("soft_stop", ""), [(ev.kind, ev.content) for ev in events])

    def test_console_responses_stream_adapter_collects_search_sources(self) -> None:
        adapter = ConsoleResponsesStreamAdapter()

        adapter.feed(
            '{"type":"response.output_item.done","item":{'
            '"type":"web_search_call","action":{"sources":[{'
            '"url":"https://example.com/post","title":"Example source"'
            '}]}}}'
        )
        adapter.feed(
            '{"type":"response.output_item.done","item":{'
            '"type":"x_search_call","results":[{'
            '"username":"xai","postId":"123","text":"Latest update from xAI"'
            '}]}}'
        )

        self.assertEqual(
            adapter.search_sources_list(),
            [
                {
                    "url": "https://example.com/post",
                    "title": "Example source",
                    "type": "web",
                },
                {
                    "url": "https://x.com/xai/status/123",
                    "title": "X/@xai: Latest update from xAI",
                    "type": "x_post",
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()
