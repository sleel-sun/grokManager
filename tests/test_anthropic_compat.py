"""Tests for the Anthropic-compatible surface (count_tokens + model listing).

These tests exercise the request/response layer in isolation by invoking the
FastAPI route handler functions directly via ``asyncio.run`` and inspecting
the resulting ``JSONResponse`` bodies. This avoids depending on
``fastapi.testclient`` (which would pull in ``httpx``) and keeps the test
surface minimal — the routers' behaviour is the same either way because
FastAPI just delegates to these coroutines once parameters are bound.
"""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from app.control.account.enums import AccountStatus


# ---------------------------------------------------------------------------
# Stubs for the OpenAI router's ``_available_pools`` dependency
# ---------------------------------------------------------------------------


class _StubRecord:
    """Minimal stand-in for ``AccountRecord`` so ``is_manageable`` returns True.

    ``is_manageable`` calls ``record.is_deleted()`` and ``derive_status(record)``;
    the latter inspects ``record.status`` (and ``record.ext`` for cooling
    accounts). ACTIVE + empty ``ext`` short-circuits to ACTIVE.
    """

    def __init__(self, pool: str):
        self.pool = pool
        self.status = AccountStatus.ACTIVE
        self.ext: dict = {}

    def is_deleted(self) -> bool:
        return False


def _build_request(pools: tuple[str, ...] = ("basic", "super", "heavy")):
    """Fake ``fastapi.Request`` exposing ``app.state.repository`` only."""
    repo = MagicMock()
    snapshot = MagicMock()
    snapshot.items = tuple(_StubRecord(p) for p in pools)
    repo.runtime_snapshot = AsyncMock(return_value=snapshot)

    request = MagicMock()
    request.app.state.repository = repo
    return request


def _body(json_response) -> dict:
    """Decode the body of a ``JSONResponse`` returned by a handler."""
    raw = json_response.body
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode()
    return json.loads(raw)


# ---------------------------------------------------------------------------
# /v1/messages/count_tokens
# ---------------------------------------------------------------------------


class CountTokensEndpointTests(unittest.TestCase):
    @staticmethod
    def _call(payload: dict):
        from app.products.anthropic.router import (
            CountTokensRequest,
            count_tokens_endpoint,
        )

        req = CountTokensRequest.model_validate(payload)
        return asyncio.run(count_tokens_endpoint(req))

    def test_count_tokens_returns_input_tokens_for_simple_message(self) -> None:
        resp = self._call(
            {
                "model": "grok-4.20-0309",
                "messages": [{"role": "user", "content": "Hello, world!"}],
            }
        )
        body = _body(resp)
        self.assertIn("input_tokens", body)
        self.assertIsInstance(body["input_tokens"], int)
        self.assertGreater(body["input_tokens"], 0)

    def test_count_tokens_with_system_and_tools_is_larger_than_baseline(self) -> None:
        base = _body(
            self._call(
                {
                    "model": "grok-4.20-0309",
                    "messages": [{"role": "user", "content": "Hello"}],
                }
            )
        )["input_tokens"]

        enriched = _body(
            self._call(
                {
                    "model": "grok-4.20-0309",
                    "system": "You are a helpful assistant that talks a lot.",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "tools": [
                        {
                            "name": "get_weather",
                            "description": "Lookup the weather for a city.",
                            "input_schema": {
                                "type": "object",
                                "properties": {"city": {"type": "string"}},
                                "required": ["city"],
                            },
                        }
                    ],
                }
            )
        )["input_tokens"]

        self.assertGreater(enriched, base)

    def test_count_tokens_rejects_empty_messages(self) -> None:
        from app.platform.errors import ValidationError

        with self.assertRaises(ValidationError):
            self._call({"model": "grok-4.20-0309", "messages": []})

    def test_count_tokens_accepts_payload_without_model(self) -> None:
        """`messages.count_tokens()` in the Anthropic Python SDK requires
        ``model`` only for routing/pricing — the count itself is purely
        content-driven. We accept requests that omit it so SDK-free clients
        can call the endpoint directly without picking a Grok model name.
        """
        resp = self._call(
            {"messages": [{"role": "user", "content": "Hello"}]},
        )
        body = _body(resp)
        self.assertGreater(body["input_tokens"], 0)


# ---------------------------------------------------------------------------
# /v1/models content negotiation
# ---------------------------------------------------------------------------


class ModelsContentNegotiationTests(unittest.TestCase):
    @staticmethod
    def _list_models(anthropic_version: str | None = None, pools=("basic", "super", "heavy")):
        from app.products.openai.router import list_models

        request = _build_request(pools)
        return asyncio.run(
            list_models(request=request, anthropic_version=anthropic_version)
        )

    @staticmethod
    def _get_model(model_id: str, anthropic_version: str | None = None):
        from app.products.openai.router import get_model_endpoint

        request = _build_request()
        return asyncio.run(
            get_model_endpoint(
                model_id=model_id,
                request=request,
                anthropic_version=anthropic_version,
            )
        )

    def test_list_models_default_returns_openai_format(self) -> None:
        body = _body(self._list_models())

        self.assertEqual(body["object"], "list")
        self.assertIsInstance(body["data"], list)
        self.assertGreater(len(body["data"]), 0)
        first = body["data"][0]
        self.assertEqual(first["object"], "model")
        self.assertEqual(first["owned_by"], "xai")
        self.assertIn("created", first)
        self.assertNotIn("display_name", first)

    def test_list_models_anthropic_header_returns_anthropic_format(self) -> None:
        body = _body(self._list_models(anthropic_version="2023-06-01"))

        self.assertNotIn("object", body)
        self.assertIn("has_more", body)
        self.assertFalse(body["has_more"])
        self.assertIsInstance(body["data"], list)
        self.assertGreater(len(body["data"]), 0)
        first = body["data"][0]
        self.assertEqual(first["type"], "model")
        self.assertIn("display_name", first)
        self.assertTrue(first["created_at"].endswith("Z"), first["created_at"])
        self.assertEqual(body["first_id"], first["id"])
        self.assertEqual(body["last_id"], body["data"][-1]["id"])

    def test_list_models_anthropic_empty_pool_returns_empty_envelope(self) -> None:
        # No manageable accounts → no models pass the pool filter.
        body = _body(self._list_models(anthropic_version="2023-06-01", pools=()))

        self.assertEqual(body["data"], [])
        self.assertFalse(body["has_more"])
        self.assertIsNone(body["first_id"])
        self.assertIsNone(body["last_id"])

    def test_get_model_anthropic_header_returns_anthropic_format(self) -> None:
        body = _body(
            self._get_model("grok-4.20-0309", anthropic_version="2023-06-01")
        )

        self.assertEqual(body["type"], "model")
        self.assertEqual(body["id"], "grok-4.20-0309")
        self.assertIn("display_name", body)
        self.assertTrue(body["created_at"].endswith("Z"))

    def test_get_model_default_returns_openai_format(self) -> None:
        body = _body(self._get_model("grok-4.20-0309"))

        self.assertEqual(body["object"], "model")
        self.assertEqual(body["id"], "grok-4.20-0309")
        self.assertIn("name", body)

    def test_get_model_unknown_returns_format_specific_404(self) -> None:
        resp_openai = self._get_model("__does_not_exist__")
        self.assertEqual(resp_openai.status_code, 404)
        body_openai = _body(resp_openai)
        self.assertEqual(body_openai["error"]["type"], "invalid_request_error")

        resp_anthropic = self._get_model(
            "__does_not_exist__", anthropic_version="2023-06-01"
        )
        self.assertEqual(resp_anthropic.status_code, 404)
        body_anthropic = _body(resp_anthropic)
        self.assertEqual(body_anthropic["type"], "error")
        self.assertEqual(body_anthropic["error"]["type"], "not_found_error")


# ---------------------------------------------------------------------------
# Helper-level checks (no FastAPI layer)
# ---------------------------------------------------------------------------


class ModelPayloadHelperTests(unittest.TestCase):
    def test_anthropic_model_payload_iso_timestamp(self) -> None:
        from app.control.model.registry import resolve
        from app.products.openai.router import _anthropic_model_payload

        spec = resolve("grok-4.20-0309")
        payload = _anthropic_model_payload(spec, 0)

        self.assertEqual(payload["type"], "model")
        self.assertEqual(payload["id"], "grok-4.20-0309")
        self.assertEqual(payload["created_at"], "1970-01-01T00:00:00Z")

    def test_openai_model_payload_shape(self) -> None:
        from app.control.model.registry import resolve
        from app.products.openai.router import _openai_model_payload

        spec = resolve("grok-4.20-0309")
        payload = _openai_model_payload(spec, 0)

        self.assertEqual(payload["object"], "model")
        self.assertEqual(payload["id"], "grok-4.20-0309")
        self.assertEqual(payload["owned_by"], "xai")
        self.assertEqual(payload["created"], 0)

    def test_is_anthropic_client_detects_header(self) -> None:
        from app.products.openai.router import _is_anthropic_client

        self.assertFalse(_is_anthropic_client(None))
        self.assertFalse(_is_anthropic_client(""))
        self.assertFalse(_is_anthropic_client("   "))
        self.assertTrue(_is_anthropic_client("2023-06-01"))
        self.assertTrue(_is_anthropic_client("bedrock-2023-05-31"))


if __name__ == "__main__":
    unittest.main()
