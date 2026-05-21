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


if __name__ == "__main__":
    unittest.main()
