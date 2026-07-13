import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch


def _body(json_response) -> dict:
    raw = json_response.body
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode()
    return json.loads(raw)


async def _collect_stream(response) -> str:
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        if isinstance(chunk, (bytes, bytearray)):
            chunks.append(chunk.decode())
        else:
            chunks.append(str(chunk))
    return "".join(chunks)


class OpenAICompletionsEndpointTests(unittest.TestCase):
    def test_completions_returns_legacy_text_completion_shape(self) -> None:
        from app.products.openai.router import CompletionRequest, completions_endpoint

        req = CompletionRequest.model_validate({
            "model": "grok-4.3",
            "prompt": "Say hi",
        })
        fake_result = {
            "id": "chatcmpl_test",
            "object": "chat.completion",
            "model": "grok-4.3",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 1,
                "total_tokens": 3,
            },
        }

        with patch(
            "app.products.openai.router.chat_completions",
            new=AsyncMock(return_value=fake_result),
        ) as mocked:
            response = asyncio.run(completions_endpoint(req))

        body = _body(response)
        self.assertEqual(body["object"], "text_completion")
        self.assertEqual(body["model"], "grok-4.3")
        self.assertEqual(body["choices"][0]["text"], "hi")
        self.assertEqual(body["choices"][0]["finish_reason"], "stop")
        self.assertEqual(body["usage"]["total_tokens"], 3)
        mocked.assert_awaited_once()
        self.assertEqual(
            mocked.await_args.kwargs["messages"],
            [{"role": "user", "content": "Say hi"}],
        )
        self.assertFalse(mocked.await_args.kwargs["stream"])

    def test_completions_stream_converts_chat_chunks(self) -> None:
        from app.products.openai.router import CompletionRequest, completions_endpoint

        async def fake_stream():
            yield (
                'data: {"id":"chatcmpl_test","object":"chat.completion.chunk",'
                '"created":1,"model":"grok-4.3","choices":[{"index":0,'
                '"delta":{"content":"hi"}}]}\n\n'
            )
            yield (
                'data: {"id":"chatcmpl_test","object":"chat.completion.chunk",'
                '"created":1,"model":"grok-4.3","choices":[{"index":0,'
                '"delta":{},"finish_reason":"stop"}]}\n\n'
            )
            yield "data: [DONE]\n\n"

        req = CompletionRequest.model_validate({
            "model": "grok-4.3",
            "prompt": "Say hi",
            "stream": True,
        })

        with patch(
            "app.products.openai.router.chat_completions",
            new=AsyncMock(return_value=fake_stream()),
        ):
            response = asyncio.run(completions_endpoint(req))
            text = asyncio.run(_collect_stream(response))

        self.assertIn('"object":"text_completion.chunk"', text)
        self.assertIn('"text":"hi"', text)
        self.assertIn('"finish_reason":"stop"', text)
        self.assertIn("data: [DONE]", text)

    def test_completions_rejects_image_models(self) -> None:
        from app.platform.errors import ValidationError
        from app.products.openai.router import CompletionRequest, completions_endpoint

        req = CompletionRequest.model_validate({
            "model": "gpt-image-2",
            "prompt": "draw a cat",
        })

        with self.assertRaises(ValidationError):
            asyncio.run(completions_endpoint(req))


if __name__ == "__main__":
    unittest.main()
