import json
import unittest

from app.platform.errors import UpstreamError
from app.products.openai.router import _safe_sse


class OpenAIRouterErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_safe_sse_unwraps_single_app_error_exception_group(self) -> None:
        async def broken_stream():
            if False:
                yield ""
            raise ExceptionGroup(
                "unhandled errors in a TaskGroup",
                [
                    UpstreamError(
                        "Image edit reference 1 upload failed: upstream rejected",
                        status=502,
                        body="upstream details",
                    )
                ],
            )

        chunks = [chunk async for chunk in _safe_sse(broken_stream())]
        payload = json.loads(chunks[0].split("data: ", 1)[1])

        self.assertEqual(
            payload["error"]["message"],
            "Image edit reference 1 upload failed: upstream rejected",
        )
        self.assertEqual(payload["error"]["type"], "upstream_error")
        self.assertNotIn("TaskGroup", chunks[0])


if __name__ == "__main__":
    unittest.main()
