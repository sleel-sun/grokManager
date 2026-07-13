import unittest

from app.control.account.enums import FeedbackKind
from app.control.account.invalid_credentials import feedback_kind_for_error
from app.platform.errors import UpstreamError


class UpstreamErrorClassificationTests(unittest.TestCase):
    def test_cloudflare_403_is_not_account_forbidden(self) -> None:
        exc = UpstreamError(
            "Image-generation upstream returned 403",
            status=403,
            body='<!DOCTYPE html><title>Just a moment...</title><p>Cloudflare</p>',
        )

        self.assertEqual(feedback_kind_for_error(exc), FeedbackKind.SERVER_ERROR)

    def test_plain_waf_block_403_is_not_account_forbidden(self) -> None:
        exc = UpstreamError(
            "Image-generation upstream returned 403",
            status=403,
            body="403 Your request was blocked.",
        )

        self.assertEqual(feedback_kind_for_error(exc), FeedbackKind.SERVER_ERROR)

    def test_generic_403_still_counts_as_account_forbidden(self) -> None:
        exc = UpstreamError("Image-generation upstream returned 403", status=403)

        self.assertEqual(feedback_kind_for_error(exc), FeedbackKind.FORBIDDEN)


if __name__ == "__main__":
    unittest.main()
