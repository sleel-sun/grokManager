import asyncio
import unittest
from unittest.mock import patch

from app.products.web.webui.imagine import (
    _acquire_token,
    _image_event_error_payload,
    _no_webui_image_accounts_message,
)


class WebuiImagineErrorPayloadTests(unittest.TestCase):
    def test_normalizes_stream_error_fields_for_masonry_frontend(self) -> None:
        payload = _image_event_error_payload(
            {
                "type": "error",
                "error_code": "rate_limit_exceeded",
                "error": "Image rate limit exceeded",
            },
            "run-1",
        )

        self.assertEqual(payload["type"], "error")
        self.assertEqual(payload["message"], "Image rate limit exceeded")
        self.assertEqual(payload["code"], "rate_limit_exceeded")
        self.assertEqual(payload["run_id"], "run-1")

    def test_no_webui_image_accounts_message_mentions_required_pool(self) -> None:
        message = _no_webui_image_accounts_message()

        self.assertIn("Super", message)
        self.assertIn("Heavy", message)


class _CaptureDirectory:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def reserve(self, **kwargs):
        self.calls.append(kwargs)
        return None


class WebuiImagineAccountSelectionTests(unittest.TestCase):
    def test_masonry_uses_super_heavy_pools_without_basic_fallback(self) -> None:
        from app.dataplane import account as account_module

        directory = _CaptureDirectory()

        async def run():
            with patch.object(account_module, "_directory", directory):
                return await _acquire_token(attempt=0)

        token, lease = asyncio.run(run())

        self.assertIsNone(token)
        self.assertIsNone(lease)
        self.assertEqual(directory.calls[0]["pool_candidates"], (1, 2))


if __name__ == "__main__":
    unittest.main()
