import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch


def _sse_json(frame: str) -> dict:
    for line in frame.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    raise AssertionError(f"SSE frame has no data line: {frame!r}")


async def _collect_async(stream) -> list[str]:
    return [chunk async for chunk in stream]


class _FakeAccount:
    token = "test-token"


class _FakeDirectory:
    async def release(self, _acct) -> None:
        return None

    async def feedback(self, *_args, **_kwargs) -> None:
        return None


class StreamCoalescingTests(unittest.TestCase):
    def test_openai_stream_coalesces_small_content_deltas(self) -> None:
        from app.products.openai.chat import _OpenAIStreamChunkCoalescer

        coalescer = _OpenAIStreamChunkCoalescer(
            "chatcmpl_test",
            "grok-test",
            min_chars=4,
            max_delay_s=999,
        )

        self.assertEqual(coalescer.add_text("a"), [])
        self.assertEqual(coalescer.add_text("b"), [])
        chunks = coalescer.add_text("cd")

        self.assertEqual(len(chunks), 1)
        payload = _sse_json(chunks[0])
        self.assertEqual(payload["choices"][0]["delta"]["content"], "abcd")

    def test_openai_stream_flushes_reasoning_before_content(self) -> None:
        from app.products.openai.chat import _OpenAIStreamChunkCoalescer

        coalescer = _OpenAIStreamChunkCoalescer(
            "chatcmpl_test",
            "grok-test",
            min_chars=8,
            max_delay_s=999,
        )

        self.assertEqual(coalescer.add_thinking("think"), [])
        chunks = coalescer.add_text("answer")

        self.assertEqual(len(chunks), 1)
        self.assertEqual(
            _sse_json(chunks[0])["choices"][0]["delta"]["reasoning_content"],
            "think",
        )
        self.assertEqual(
            _sse_json(coalescer.flush_all()[0])["choices"][0]["delta"]["content"],
            "answer",
        )

    def test_anthropic_stream_coalesces_small_text_deltas(self) -> None:
        from app.products.anthropic.messages import _AnthropicStreamDeltaCoalescer

        coalescer = _AnthropicStreamDeltaCoalescer(
            "text_delta",
            "text",
            min_chars=4,
            max_delay_s=999,
        )

        self.assertEqual(coalescer.add(0, "a"), [])
        self.assertEqual(coalescer.add(0, "b"), [])
        chunks = coalescer.add(0, "cd")

        self.assertEqual(len(chunks), 1)
        payload = _sse_json(chunks[0])
        self.assertEqual(payload["type"], "content_block_delta")
        self.assertEqual(payload["index"], 0)
        self.assertEqual(payload["delta"], {"type": "text_delta", "text": "abcd"})

    def test_openai_chat_stream_path_coalesces_upstream_single_character_events(self) -> None:
        from app.control.model.enums import ModeId
        from app.products.openai import chat

        async def fake_stream_chat(**_kwargs):
            for char in "abcd":
                yield (
                    "data: "
                    + json.dumps({
                        "type": "response.output_text.delta",
                        "delta": char,
                    })
                    + "\n\n"
                )
            yield 'data: {"type":"response.completed","response":{}}\n\n'

        async def fake_reserve_account(*_args, **_kwargs):
            return _FakeAccount(), ModeId.CONSOLE

        with (
            patch("app.dataplane.account._directory", _FakeDirectory()),
            patch("app.products.openai.chat._stream_chat", new=fake_stream_chat),
            patch(
                "app.products.openai.chat.reserve_account",
                new=AsyncMock(side_effect=fake_reserve_account),
            ),
            patch("app.products.openai.chat._quota_sync", new=AsyncMock()),
            patch("app.products.openai.chat._fail_sync", new=AsyncMock()),
        ):
            stream = asyncio.run(
                chat.completions(
                    model="grok-4.3",
                    messages=[{"role": "user", "content": "hi"}],
                    stream=True,
                    emit_think=False,
                )
            )
            chunks = asyncio.run(_collect_async(stream))

        content_deltas = []
        for chunk in chunks:
            if not chunk.startswith("data: {"):
                continue
            payload = _sse_json(chunk)
            choice = payload.get("choices", [{}])[0]
            content = choice.get("delta", {}).get("content")
            if content:
                content_deltas.append(content)

        self.assertEqual(content_deltas, ["abcd"])


if __name__ == "__main__":
    unittest.main()
