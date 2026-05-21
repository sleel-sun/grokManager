"""Tests for ``X-Upstream-*`` response headers exposed by the relay.

Why these matter: LLMs are unreliable at self-identification, so the same
prompt to ``grok-4.3`` can return "grok", "grok-4", or "grok-1.5" in different
runs. These headers expose the deterministic upstream routing for clients
that need to verify which model and endpoint the relay actually used.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.control.model.spec import ModelSpec
from app.control.model.enums import Capability, ModeId, Tier
from app.products._upstream_headers import build_upstream_response_headers


class BuildUpstreamHeadersTests(unittest.TestCase):
    def test_console_responses_routes_to_console_endpoint(self) -> None:
        spec = ModelSpec(
            "grok-4.3", ModeId.AUTO, Tier.BASIC, Capability.CHAT,
            True, "Grok 4.3",
            upstream_profile="console_responses",
            upstream_model="grok-4.3",
        )
        hdr = build_upstream_response_headers(spec)
        self.assertEqual(hdr["X-Upstream-Profile"], "console_responses")
        self.assertEqual(hdr["X-Upstream-Model"], "grok-4.3")
        self.assertEqual(hdr["X-Upstream-Endpoint"], "https://console.x.ai/v1/responses")

    def test_grok_web_routes_to_app_chat(self) -> None:
        spec = ModelSpec(
            "grok-4.20-0309-non-reasoning", ModeId.FAST, Tier.BASIC, Capability.CHAT,
            True, "Grok 4.20",
        )
        hdr = build_upstream_response_headers(spec)
        self.assertEqual(hdr["X-Upstream-Profile"], "grok_web")
        self.assertEqual(hdr["X-Upstream-Model"], "grok-4.20-0309-non-reasoning")
        self.assertEqual(hdr["X-Upstream-Endpoint"], "https://grok.com/rest/app-chat/conversations/new")

    def test_image_model_routes_to_ws_imagine(self) -> None:
        spec = ModelSpec(
            "grok-imagine-image-lite", ModeId.FAST, Tier.BASIC, Capability.IMAGE,
            True, "Imagine",
        )
        hdr = build_upstream_response_headers(spec)
        self.assertEqual(hdr["X-Upstream-Profile"], "grok_web")
        self.assertEqual(hdr["X-Upstream-Model"], "grok-imagine-image-lite")
        self.assertEqual(hdr["X-Upstream-Endpoint"], "wss://grok.com/ws/imagine/listen")

    def test_upstream_model_override_takes_precedence(self) -> None:
        spec = ModelSpec(
            "my-public-alias", ModeId.AUTO, Tier.BASIC, Capability.CHAT,
            True, "Alias",
            upstream_profile="console_responses",
            upstream_model="grok-4.3",
        )
        hdr = build_upstream_response_headers(spec)
        self.assertEqual(hdr["X-Upstream-Model"], "grok-4.3")

    def test_no_override_falls_back_to_public_name(self) -> None:
        spec = ModelSpec(
            "grok-4.20-0309", ModeId.AUTO, Tier.BASIC, Capability.CHAT,
            True, "Grok 4.20",
        )
        hdr = build_upstream_response_headers(spec)
        self.assertEqual(hdr["X-Upstream-Model"], "grok-4.20-0309")


class OpenAIChatCompletionsHeaderTests(unittest.TestCase):
    """End-to-end: invoke ``chat_completions_endpoint`` and assert response carries the headers."""

    def test_non_stream_response_includes_upstream_headers(self) -> None:
        from app.products.openai.router import chat_completions_endpoint
        from app.products.openai.schemas import ChatCompletionRequest

        req = ChatCompletionRequest.model_validate({
            "model": "grok-4.3",
            "stream": False,
            "messages": [{"role": "user", "content": "hi"}],
        })

        fake_result = {"id": "x", "object": "chat.completion", "choices": []}
        with patch("app.products.openai.router.chat_completions",
                   new=AsyncMock(return_value=fake_result)):
            response = asyncio.run(chat_completions_endpoint(req))

        self.assertEqual(response.headers.get("x-upstream-profile"), "console_responses")
        self.assertEqual(response.headers.get("x-upstream-model"), "grok-4.3")
        self.assertEqual(response.headers.get("x-upstream-endpoint"), "https://console.x.ai/v1/responses")

    def test_stream_response_includes_upstream_headers(self) -> None:
        from app.products.openai.router import chat_completions_endpoint
        from app.products.openai.schemas import ChatCompletionRequest

        req = ChatCompletionRequest.model_validate({
            "model": "grok-4.20-0309-non-reasoning",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        })

        async def _gen():
            yield "data: {}\n\n"
            yield "data: [DONE]\n\n"

        with patch("app.products.openai.router.chat_completions",
                   new=AsyncMock(return_value=_gen())):
            response = asyncio.run(chat_completions_endpoint(req))

        self.assertEqual(response.headers.get("x-upstream-profile"), "grok_web")
        self.assertEqual(response.headers.get("x-upstream-model"), "grok-4.20-0309-non-reasoning")
        self.assertEqual(
            response.headers.get("x-upstream-endpoint"),
            "https://grok.com/rest/app-chat/conversations/new",
        )


class AnthropicMessagesHeaderTests(unittest.TestCase):
    def test_non_stream_messages_includes_upstream_headers(self) -> None:
        from app.products.anthropic.router import (
            MessagesRequest,
            messages_endpoint,
        )

        req = MessagesRequest.model_validate({
            "model": "grok-4.3",
            "stream": False,
            "messages": [{"role": "user", "content": "hi"}],
        })

        fake_result = {"id": "x", "type": "message", "content": []}
        with patch("app.products.anthropic.messages.create",
                   new=AsyncMock(return_value=fake_result)):
            response = asyncio.run(messages_endpoint(req))

        self.assertEqual(response.headers.get("x-upstream-profile"), "console_responses")
        self.assertEqual(response.headers.get("x-upstream-model"), "grok-4.3")
        self.assertEqual(response.headers.get("x-upstream-endpoint"), "https://console.x.ai/v1/responses")

    def test_stream_messages_includes_upstream_headers(self) -> None:
        from app.products.anthropic.router import (
            MessagesRequest,
            messages_endpoint,
        )

        req = MessagesRequest.model_validate({
            "model": "grok-4.20-0309-non-reasoning",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        })

        async def _gen():
            yield "event: message_start\ndata: {}\n\n"

        with patch("app.products.anthropic.messages.create",
                   new=AsyncMock(return_value=_gen())):
            response = asyncio.run(messages_endpoint(req))

        self.assertEqual(response.headers.get("x-upstream-profile"), "grok_web")
        self.assertEqual(response.headers.get("x-upstream-model"), "grok-4.20-0309-non-reasoning")


if __name__ == "__main__":
    unittest.main()
