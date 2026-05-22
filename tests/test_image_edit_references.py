import asyncio
import unittest
from unittest.mock import patch

from app.platform.errors import UpstreamError
from app.products.openai.images import _prepare_edit_reference, _prepare_edit_references


class _FakeConfig:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    def get_str(self, key: str, default: str = "") -> str:
        return self._values.get(key, default)


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


if __name__ == "__main__":
    unittest.main()
