import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.maintainer.grok_build_usage import (
    flush_usage,
    load_usage,
    publish_usage,
    record_usage,
    start_usage_writer,
    stop_usage_writer,
)


class GrokBuildUsageCompatibilityTests(unittest.TestCase):
    def test_record_usage_remains_immediately_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch(
                "app.maintainer.grok_build_usage.usage_db_path",
                return_value=Path(tmpdir) / "usage.db",
            ):
                self.assertTrue(
                    record_usage(
                        "sso:sync",
                        generation="gen-a",
                        status_code=200,
                        usage={"prompt_tokens": 3, "completion_tokens": 2},
                    )
                )

                row = load_usage()[("sso:sync", "gen-a")]

        self.assertEqual(row["request_count"], 1)
        self.assertEqual(row["total_tokens"], 5)


class GrokBuildUsageQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path_patch = patch(
            "app.maintainer.grok_build_usage.usage_db_path",
            return_value=Path(self.tmpdir.name) / "usage.db",
        )
        self.path_patch.start()

    async def asyncTearDown(self) -> None:
        await stop_usage_writer(flush=False)
        self.path_patch.stop()
        self.tmpdir.cleanup()

    async def test_publish_batches_and_flushes_accepted_events(self) -> None:
        await start_usage_writer(capacity=8, batch_size=8, flush_interval=60)

        self.assertTrue(
            publish_usage(
                "sso:async",
                generation="gen-a",
                status_code=200,
                usage={"input_tokens": 4, "output_tokens": 1},
                headers={"X-RateLimit-Remaining": "9"},
            )
        )
        self.assertTrue(
            publish_usage(
                "sso:async",
                generation="gen-a",
                status_code=429,
                usage={"total_tokens": 2},
            )
        )

        await flush_usage()
        row = load_usage()[("sso:async", "gen-a")]

        self.assertEqual(row["request_count"], 2)
        self.assertEqual(row["success_count"], 1)
        self.assertEqual(row["failure_count"], 1)
        self.assertEqual(row["total_tokens"], 7)
        self.assertEqual(row["last_status"], 429)
        self.assertEqual(row["quota_remaining"], "9")

    async def test_capacity_is_bounded_and_stop_flushes(self) -> None:
        await start_usage_writer(capacity=1, batch_size=10, flush_interval=60)

        self.assertTrue(publish_usage("sso:first", status_code=200))
        self.assertFalse(publish_usage("sso:dropped", status_code=200))

        await stop_usage_writer(flush=True)

        self.assertIn(("sso:first", "legacy"), load_usage())
        self.assertNotIn(("sso:dropped", "legacy"), load_usage())
        self.assertFalse(publish_usage("sso:stopped", status_code=200))

