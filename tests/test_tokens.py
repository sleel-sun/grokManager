import unittest
from unittest.mock import patch

from app.platform import tokens


class TokenEstimatorTests(unittest.TestCase):
    def setUp(self) -> None:
        tokens._get_encoding.cache_clear()

    def tearDown(self) -> None:
        tokens._get_encoding.cache_clear()

    def test_estimator_falls_back_when_tiktoken_encodings_are_missing(self) -> None:
        with patch(
            "app.platform.tokens.tiktoken.get_encoding",
            side_effect=ValueError("Unknown encoding o200k_base"),
        ):
            self.assertGreater(tokens.estimate_tokens("Reply OK"), 0)
            self.assertGreater(tokens.estimate_prompt_tokens("Reply OK"), 0)


if __name__ == "__main__":
    unittest.main()
