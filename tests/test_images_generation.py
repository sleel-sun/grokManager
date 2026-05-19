import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.dataplane import account as account_module
from app.products.openai import images


class ImageGenerationRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_lite_image_generation_uses_app_chat_before_ws(self) -> None:
        calls: list[dict] = []

        class FakeDirectory:
            async def reserve_any(self, *args, **kwargs):
                return SimpleNamespace(token="token")

            async def release(self, acct):
                return None

        async def fake_app_chat(**kwargs):
            calls.append(kwargs)
            return {"created": 123, "data": [{"url": "app-chat-image"}]}

        async def ws_should_not_be_primary(*args, **kwargs):
            raise AssertionError("ws imagine should not be the primary image path")
            yield {}

        with (
            patch.object(account_module, "_directory", FakeDirectory()),
            patch.object(images, "_generate_app_chat", fake_app_chat),
            patch.object(images, "stream_images", ws_should_not_be_primary),
        ):
            result = await images.generate(
                model="grok-imagine-image",
                prompt="cat",
                n=1,
                size="1024x1024",
                response_format="url",
                stream=False,
                chat_format=False,
            )

        self.assertEqual(result["data"][0]["url"], "app-chat-image")
        self.assertEqual(calls[0]["model"], "grok-imagine-image")


if __name__ == "__main__":
    unittest.main()
