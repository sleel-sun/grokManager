import asyncio
import json
import unittest

from app.products.openai.responses import _guard_response_stream


async def _collect(source):
    return [chunk async for chunk in source]


class ResponsesStreamErrorTests(unittest.TestCase):
    def test_eof_before_terminal_event_emits_response_failed(self) -> None:
        async def truncated_stream():
            yield 'event: response.created\ndata: {"type":"response.created"}\n\n'

        chunks = asyncio.run(_collect(_guard_response_stream(
            truncated_stream(), response_id="resp_test", model="grok-4.5"
        )))

        self.assertEqual(len(chunks), 3)
        self.assertTrue(chunks[1].startswith("event: response.failed\n"))
        self.assertEqual(chunks[2], "data: [DONE]\n\n")

    def test_exception_before_terminal_event_emits_response_failed(self) -> None:
        async def broken_stream():
            yield 'event: response.created\ndata: {"type":"response.created"}\n\n'
            raise RuntimeError("upstream closed")

        chunks = asyncio.run(_collect(_guard_response_stream(
            broken_stream(), response_id="resp_test", model="grok-4.5"
        )))

        self.assertEqual(len(chunks), 3)
        self.assertTrue(chunks[1].startswith("event: response.failed\n"))
        payload = json.loads(chunks[1].split("data: ", 1)[1])
        self.assertEqual(payload["response"]["status"], "failed")
        self.assertEqual(payload["response"]["error"]["code"], "server_error")
        self.assertEqual(chunks[2], "data: [DONE]\n\n")

    def test_exception_after_completed_does_not_emit_second_terminal_event(self) -> None:
        async def completed_then_broken():
            yield 'event: response.completed\ndata: {"type":"response.completed"}\n\n'
            raise RuntimeError("feedback failed")

        chunks = asyncio.run(_collect(_guard_response_stream(
            completed_then_broken(), response_id="resp_test", model="grok-4.5"
        )))

        self.assertEqual(len(chunks), 1)
        self.assertTrue(chunks[0].startswith("event: response.completed\n"))

    def test_terminal_event_split_across_transport_chunks_is_detected(self) -> None:
        async def split_completed():
            yield "event: response.com"
            yield 'pleted\ndata: {"type":"response.completed"}\n\n'

        chunks = asyncio.run(_collect(_guard_response_stream(
            split_completed(), response_id="resp_test", model="grok-4.5"
        )))

        self.assertEqual(len(chunks), 2)
        self.assertEqual("".join(chunks).count("response.failed"), 0)

    def test_large_spaced_terminal_event_is_detected(self) -> None:
        async def large_completed():
            yield (
                "event: response.completed\n"
                'data: {"type": "response.completed", "response": {'
                f'"output": [{{"text": "{"x" * 1024}"}}]'
                "}}\n\n"
            )

        chunks = asyncio.run(_collect(_guard_response_stream(
            large_completed(), response_id="resp_test", model="grok-4.5"
        )))

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].count("response.failed"), 0)


if __name__ == "__main__":
    unittest.main()
