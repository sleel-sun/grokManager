import unittest

from app.control.model.enums import ModeId, Tier
from app.control.model.registry import list_enabled, resolve
from app.dataplane.reverse.planner import build_plan
from app.dataplane.reverse.protocol.xai_console import (
    ConsoleResponsesStreamAdapter,
    build_console_responses_payload,
)
from app.dataplane.reverse.runtime.endpoint_table import CONSOLE_RESPONSES


class ConsoleModelRoutingTests(unittest.TestCase):
    VERIFIED_CHAT_MODELS = (
        "grok-4.3",
    )
    UNAVAILABLE_CHAT_MODELS = (
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
        "grok-4.3-beta",
    )
    AVAILABLE_MEDIA_MODELS = (
        "grok-imagine-image-lite",
        "grok-imagine-image",
        "grok-imagine-image-pro",
        "grok-imagine-image-edit",
        "grok-imagine-video",
    )

    def test_verified_chat_models_are_registered_and_enabled(self) -> None:
        enabled_ids = {spec.model_name for spec in list_enabled()}

        for model in self.VERIFIED_CHAT_MODELS:
            with self.subTest(model=model):
                spec = resolve(model)
                self.assertIn(model, enabled_ids)
                self.assertTrue(spec.is_chat())

    def test_real_unavailable_chat_models_are_hidden_and_rejected(self) -> None:
        enabled_ids = {spec.model_name for spec in list_enabled()}

        for model in self.UNAVAILABLE_CHAT_MODELS:
            with self.subTest(model=model):
                self.assertNotIn(model, enabled_ids)
                with self.assertRaises(ValueError):
                    resolve(model)

    def test_media_models_stay_visible_for_runtime_account_retry(self) -> None:
        enabled_ids = {spec.model_name for spec in list_enabled()}

        for model in self.AVAILABLE_MEDIA_MODELS:
            with self.subTest(model=model):
                spec = resolve(model)
                self.assertIn(model, enabled_ids)
                self.assertTrue(spec.is_image() or spec.is_image_edit() or spec.is_video())

    def test_grok_43_uses_free_sso_console_responses_route(self) -> None:
        spec = resolve("grok-4.3")

        self.assertEqual(spec.tier, Tier.BASIC)
        self.assertEqual(spec.mode_id, ModeId.AUTO)
        self.assertTrue(spec.uses_console_responses())
        self.assertEqual(spec.upstream_model_name(), "grok-4.3")

        plan = build_plan(spec, {})
        self.assertEqual(plan.endpoint, CONSOLE_RESPONSES)
        self.assertEqual(plan.origin, "https://console.x.ai")
        self.assertEqual(plan.referer, "https://console.x.ai/")
        self.assertEqual(plan.extra["upstream_model"], "grok-4.3")

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


if __name__ == "__main__":
    unittest.main()
