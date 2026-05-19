import unittest
from unittest.mock import patch

from app.platform import tokens


class TokenEstimationTests(unittest.TestCase):
    def test_estimate_tokens_falls_back_when_tiktoken_encodings_missing(self) -> None:
        tokens._get_encoding.cache_clear()
        try:
            with patch("app.platform.tokens.tiktoken.get_encoding", side_effect=ValueError):
                self.assertGreater(tokens.estimate_tokens("hello 北京"), 0)
        finally:
            tokens._get_encoding.cache_clear()


if __name__ == "__main__":
    unittest.main()
