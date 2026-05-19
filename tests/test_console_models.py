import unittest

from app.control.model.enums import ModeId, Tier
from app.control.model.registry import resolve
from app.dataplane.reverse.planner import build_plan
from app.dataplane.reverse.protocol.xai_console import (
    ConsoleResponsesStreamAdapter,
    build_console_responses_payload,
)
from app.dataplane.reverse.runtime.endpoint_table import CONSOLE_RESPONSES


class ConsoleModelRoutingTests(unittest.TestCase):
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

    def test_grok_42_reasoning_alias_uses_console_responses_route(self) -> None:
        spec = resolve("grok-4.2reasoning")

        self.assertEqual(spec.tier, Tier.BASIC)
        self.assertEqual(spec.mode_id, ModeId.EXPERT)
        self.assertTrue(spec.uses_console_responses())
        self.assertEqual(spec.upstream_model_name(), "grok-4.2-reasoning")

        plan = build_plan(spec, {})
        self.assertEqual(plan.endpoint, CONSOLE_RESPONSES)
        self.assertEqual(plan.extra["upstream_model"], "grok-4.2-reasoning")

    def test_build_console_responses_payload(self) -> None:
        payload = build_console_responses_payload(
            model="grok-4.3",
            message="[user]: hi",
            stream=True,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Get weather",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"],
                        },
                    },
                }
            ],
            tool_choice={"type": "function", "function": {"name": "get_weather"}},
            request_overrides={"temperature": 0.2, "stream": None},
        )

        self.assertEqual(payload["model"], "grok-4.3")
        self.assertEqual(payload["input"], "[user]: hi")
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["temperature"], 0.2)
        self.assertEqual(
            payload["tools"],
            [
                {
                    "type": "function",
                    "name": "get_weather",
                    "description": "Get weather",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                }
            ],
        )
        self.assertEqual(
            payload["tool_choice"], {"type": "function", "name": "get_weather"}
        )

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

    def test_console_responses_stream_adapter_parses_tool_call_events(self) -> None:
        adapter = ConsoleResponsesStreamAdapter()

        added = adapter.feed(
            '{"type":"response.output_item.added","output_index":0,'
            '"item":{"id":"fc_1","type":"function_call","call_id":"call_1",'
            '"name":"get_weather","arguments":""}}'
        )
        delta = adapter.feed(
            '{"type":"response.function_call_arguments.delta","output_index":0,'
            '"item_id":"fc_1","delta":"{\\"city\\":\\"北京\\"}"}'
        )
        done = adapter.feed(
            '{"type":"response.output_item.done","output_index":0,'
            '"item":{"id":"fc_1","type":"function_call","call_id":"call_1",'
            '"name":"get_weather","arguments":"{\\"city\\":\\"北京\\"}"}}'
        )

        self.assertEqual(added, [])
        self.assertEqual(delta, [])
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0].kind, "tool_call")
        self.assertEqual(done[0].tool_call.call_id, "call_1")
        self.assertEqual(done[0].tool_call.name, "get_weather")
        self.assertEqual(done[0].tool_call.arguments, '{"city":"北京"}')
        self.assertEqual(len(adapter.tool_calls), 1)

    def test_console_responses_completed_extracts_function_call(self) -> None:
        adapter = ConsoleResponsesStreamAdapter()

        events = adapter.feed(
            '{"type":"response.completed","response":{"output":[{'
            '"id":"fc_1","type":"function_call","call_id":"call_1",'
            '"name":"get_weather","arguments":"{\\"city\\":\\"北京\\"}"'
            '}]}}'
        )

        self.assertEqual([ev.kind for ev in events], ["tool_call", "soft_stop"])
        self.assertEqual(events[0].tool_call.name, "get_weather")
        self.assertEqual(events[0].tool_call.arguments, '{"city":"北京"}')


if __name__ == "__main__":
    unittest.main()
