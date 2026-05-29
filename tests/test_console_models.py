import unittest

from app.control.model.enums import ModeId, Tier
from app.control.model.registry import list_enabled, resolve
from app.dataplane.reverse.planner import build_plan
from app.dataplane.reverse.protocol.xai_console import (
    ConsoleResponsesStreamAdapter,
    build_console_responses_payload,
)
from app.dataplane.reverse.runtime.endpoint_table import CONSOLE_RESPONSES
from app.products.openai.router import _openai_model_payload


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
        self.assertEqual(spec.mode_id, ModeId.AUTO)
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
        }

        for public_model, upstream_model in expected.items():
            with self.subTest(model=public_model):
                spec = resolve(public_model)

                self.assertTrue(spec.uses_console_responses())
                self.assertEqual(spec.upstream_model_name(), upstream_model)

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


if __name__ == "__main__":
    unittest.main()
