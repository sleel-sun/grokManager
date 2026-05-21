import asyncio
import unittest
from unittest.mock import patch

from app.products.openai.images import _prepare_edit_reference


class ImageEditReferenceTests(unittest.TestCase):
    def test_upstream_asset_content_url_skips_reupload(self) -> None:
        url = "https://assets.grok.com/users/user-1/asset-1/content"

        with patch(
            "app.products.openai.images.upload_from_input",
            side_effect=AssertionError("should not re-upload upstream asset content URL"),
        ):
            resolved = asyncio.run(_prepare_edit_reference("token", url, 0))

        self.assertEqual(resolved, url)


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


if __name__ == "__main__":
    unittest.main()
