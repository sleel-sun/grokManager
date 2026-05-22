import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.control.account.enums import FeedbackKind
from app.platform.errors import UpstreamError
from app.products.openai.images import _prepare_edit_reference, _prepare_edit_references


class _FakeConfig:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get(self, key: str, default=None):
        return self._values.get(key, default)

    def get_str(self, key: str, default: str = "") -> str:
        return self._values.get(key, default)

    def get_bool(self, key: str, default: bool = False) -> bool:
        return bool(self._values.get(key, default))

    def get_float(self, key: str, default: float = 0.0) -> float:
        return float(self._values.get(key, default))

    def get_int(self, key: str, default: int = 0) -> int:
        return int(self._values.get(key, default))


class _FakeLease:
    def __init__(self, token: str) -> None:
        self.token = token


class _FakeImageDirectory:
    def __init__(self) -> None:
        self.leases = [_FakeLease("bad-token"), _FakeLease("good-token")]
        self.reserved: list[tuple[str, ...]] = []
        self.released: list[str] = []
        self.feedback_calls: list[tuple[str, FeedbackKind, int]] = []

    async def reserve(self, *, exclude_tokens=None, **_kwargs):
        excluded = tuple(exclude_tokens or ())
        self.reserved.append(excluded)
        for lease in self.leases:
            if lease.token not in excluded:
                return lease
        return None

    async def reserve_any(self, *, exclude_tokens=None, **kwargs):
        return await self.reserve(exclude_tokens=exclude_tokens, **kwargs)

    async def release(self, lease) -> None:
        self.released.append(lease.token)

    async def feedback(self, token, kind, mode_id, **_kwargs) -> None:
        self.feedback_calls.append((token, kind, mode_id))


class ImageEditReferenceTests(unittest.TestCase):
    def test_upstream_asset_content_url_skips_reupload(self) -> None:
        url = "https://assets.grok.com/users/user-1/asset-1/content"

        with patch(
            "app.products.openai.images.upload_from_input",
            side_effect=AssertionError("should not re-upload upstream asset content URL"),
        ):
            resolved = asyncio.run(_prepare_edit_reference("token", url, 0))

        self.assertEqual(resolved, url)

    def test_prepare_edit_references_unwraps_taskgroup_upstream_error(self) -> None:
        async def fake_prepare_reference(token: str, image_input: str, index: int) -> str:
            if index == 1:
                raise UpstreamError("reference 2 upload failed", status=403)
            return f"resolved-{image_input}"

        with patch(
            "app.products.openai.images._prepare_edit_reference",
            side_effect=fake_prepare_reference,
        ):
            with self.assertRaises(UpstreamError) as ctx:
                asyncio.run(_prepare_edit_references("token", ["img-a", "img-b"]))

        self.assertEqual(ctx.exception.message, "reference 2 upload failed")
        self.assertEqual(ctx.exception.status, 403)


class ImageGenerationErrorTests(unittest.TestCase):
    def test_cloudflare_403_error_mentions_clearance_configuration(self) -> None:
        from app.products.openai import images

        message = images._image_generation_upstream_error_message(
            403,
            '<!DOCTYPE html><title>Just a moment...</title><p>Cloudflare</p>',
        )

        self.assertIn("Cloudflare challenge", message)
        self.assertIn("proxy.clearance", message)

    def test_non_cloudflare_403_error_keeps_plain_status(self) -> None:
        from app.products.openai import images

        message = images._image_generation_upstream_error_message(403, "")

        self.assertEqual(message, "Image-generation upstream returned 403")


class ImageGenerationOutputTests(unittest.TestCase):
    def test_grok_url_format_does_not_download_even_when_app_url_is_configured(self) -> None:
        from app.products.openai import images

        url = "https://assets.grok.com/users/user-1/image-1/content"
        cfg = _FakeConfig(
            {
                "app.app_url": "http://127.0.0.1:8000",
                "features.image_format": "grok_url",
            }
        )

        with patch("app.products.openai.images.get_config", return_value=cfg), patch(
            "app.products.openai.images._download_image_bytes",
            side_effect=AssertionError("grok_url must not download assets"),
        ):
            output = asyncio.run(
                images._resolve_image_output(
                    token="token",
                    url=url,
                    response_format="url",
                )
            )

        self.assertEqual(output.api_value, url)
        self.assertEqual(output.markdown_value, f"![image]({url})")

    def test_local_url_format_falls_back_to_upstream_url_when_download_fails(self) -> None:
        from app.products.openai import images

        url = "https://assets.grok.com/users/user-1/image-1/content"
        cfg = _FakeConfig(
            {
                "app.app_url": "http://127.0.0.1:8000",
                "features.image_format": "local_url",
            }
        )

        with patch("app.products.openai.images.get_config", return_value=cfg), patch(
            "app.products.openai.images._download_image_bytes",
            side_effect=UpstreamError("download returned 403", status=403),
        ):
            output = asyncio.run(
                images._resolve_image_output(
                    token="token",
                    url=url,
                    response_format="url",
                )
            )

        self.assertEqual(output.api_value, url)
        self.assertEqual(output.markdown_value, f"![image]({url})")


class ImageGenerationRetryTests(unittest.TestCase):
    def test_lite_generation_retries_next_account_after_403(self) -> None:
        from app.dataplane import account as account_module
        from app.products.openai import images

        directory = _FakeImageDirectory()

        async def fake_stream_lite(token: str, *_args, **_kwargs):
            if token == "bad-token":
                raise UpstreamError("Image-generation upstream returned 403", status=403)
            yield 'data: {"ok":true}'

        class FakeStreamAdapter:
            def feed(self, _data: str):
                return [SimpleNamespace(kind="image", content="https://assets.grok.com/image.jpg")]

        async def fake_resolve(**_kwargs):
            return images._ImageOutput("https://assets.grok.com/image.jpg", "![image](ok)")

        async def run():
            with (
                patch.object(account_module, "_directory", directory),
                patch.object(images, "selection_max_retries", return_value=1),
                patch.object(images, "get_config", return_value=_FakeConfig({"retry.on_codes": "429"})),
                patch.object(images, "_stream_lite_generate", side_effect=fake_stream_lite),
                patch.object(images, "StreamAdapter", FakeStreamAdapter),
                patch.object(images, "_resolve_image_output", new=AsyncMock(side_effect=fake_resolve)),
                patch.object(images, "_quota_sync", new=AsyncMock(return_value=None)),
                patch.object(images, "_fail_sync", new=AsyncMock(return_value=None)),
            ):
                return await images._run_lite_request(
                    spec=images.resolve_model("grok-imagine-image-lite"),
                    prompt="draw a cat",
                    timeout_s=1,
                    response_format="url",
                )

        output = asyncio.run(run())

        self.assertEqual(output.api_value, "https://assets.grok.com/image.jpg")
        self.assertEqual(directory.reserved, [(), ("bad-token",)])
        self.assertEqual(directory.released, ["bad-token", "good-token"])
        self.assertEqual(directory.feedback_calls[0][1], FeedbackKind.FORBIDDEN)
        self.assertEqual(directory.feedback_calls[1][1], FeedbackKind.SUCCESS)

    def test_ws_generation_retries_next_account_after_rate_limit_event(self) -> None:
        from app.dataplane import account as account_module
        from app.products.openai import images

        directory = _FakeImageDirectory()
        cfg = _FakeConfig(
            {
                "features.enable_nsfw": True,
                "features.image_format": "grok_url",
                "retry.on_codes": "429",
            }
        )

        async def fake_stream_images(token: str, *_args, **_kwargs):
            if token == "bad-token":
                yield {
                    "type": "error",
                    "error_code": "rate_limit_exceeded",
                    "error": "Image rate limit exceeded",
                }
                return
            yield {
                "type": "image",
                "is_final": True,
                "url": "https://assets.grok.com/users/user-1/image-1/content",
                "image_id": "image-1",
            }

        async def run():
            with (
                patch.object(account_module, "_directory", directory),
                patch.object(images, "selection_max_retries", return_value=1),
                patch.object(images, "get_config", return_value=cfg),
                patch.object(images, "stream_images", side_effect=fake_stream_images),
                patch.object(
                    images,
                    "_resolve_image_output",
                    new=AsyncMock(return_value=images._ImageOutput("ok-url", "![image](ok-url)")),
                ),
            ):
                return await images.generate(
                    model="grok-imagine-image",
                    prompt="draw a cat",
                    n=1,
                    stream=False,
                )

        payload = asyncio.run(run())

        self.assertEqual(payload["data"], [{"url": "ok-url"}])
        self.assertEqual(directory.reserved, [(), ("bad-token",)])
        self.assertEqual(directory.released, ["bad-token", "good-token"])
        self.assertEqual(directory.feedback_calls[0][1], FeedbackKind.RATE_LIMITED)
        self.assertEqual(directory.feedback_calls[1][1], FeedbackKind.SUCCESS)


if __name__ == "__main__":
    unittest.main()
