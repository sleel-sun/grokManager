import unittest

from app.products.web.webui.imagine import _image_event_error_payload


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


if __name__ == "__main__":
    unittest.main()
