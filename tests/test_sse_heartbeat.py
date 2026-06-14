import asyncio
import unittest

from app.products.openai.router import _sse_with_heartbeat


class SseHeartbeatTests(unittest.TestCase):
    def test_heartbeat_does_not_cancel_slow_upstream_stream(self) -> None:
        async def slow_stream():
            await asyncio.sleep(0.03)
            yield "data: ok\n\n"

        async def run():
            chunks = []
            async for chunk in _sse_with_heartbeat(slow_stream(), interval=0.01):
                chunks.append(chunk)
            return chunks

        chunks = asyncio.run(run())

        self.assertTrue(chunks[0].startswith(": heartbeat stream connected"))
        self.assertIn(": ping\n\n", chunks)
        self.assertIn("data: ok\n\n", chunks)


if __name__ == "__main__":
    unittest.main()
