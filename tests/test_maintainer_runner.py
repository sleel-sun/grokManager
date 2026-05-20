import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.maintainer.runner import (
    _build_worker_output,
    _ensure_browser_storage_ready,
    _resolve_browser_tmp_path,
    _split_count,
    _wait_while_paused,
)


class MaintainerRunnerTests(unittest.TestCase):
    def test_browser_tmp_path_can_be_overridden_by_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"MAINTAINER_TMP_PATH": tmpdir}):
                self.assertEqual(_resolve_browser_tmp_path(), Path(tmpdir).resolve())

    def test_browser_storage_error_is_actionable_when_space_is_low(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"MAINTAINER_MIN_BROWSER_FREE_BYTES": str(10**18)}):
                with self.assertRaisesRegex(RuntimeError, "浏览器临时目录可用空间不足"):
                    _ensure_browser_storage_ready(tmpdir)


class MaintainerBatchHelpersTests(unittest.TestCase):
    def test_split_count_distributes_remainder_to_first_workers(self) -> None:
        self.assertEqual(_split_count(10, 3), [4, 3, 3])
        self.assertEqual(_split_count(5, 5), [1, 1, 1, 1, 1])

    def test_split_count_drops_workers_with_zero_share(self) -> None:
        self.assertEqual(_split_count(2, 4), [1, 1])

    def test_split_count_with_zero_total_uses_unbounded_sentinels(self) -> None:
        # ``count == 0`` means "loop until stopped"; every worker gets 0.
        self.assertEqual(_split_count(0, 3), [0, 0, 0])

    def test_build_worker_output_appends_worker_suffix(self) -> None:
        base = Path("/tmp/sso_20260520.txt")
        self.assertEqual(_build_worker_output(base, 0), Path("/tmp/sso_20260520.w0.txt"))
        self.assertEqual(_build_worker_output(base, 2), Path("/tmp/sso_20260520.w2.txt"))


class MaintainerPauseCheckTests(unittest.TestCase):
    def test_wait_while_paused_returns_false_when_not_paused(self) -> None:
        self.assertFalse(_wait_while_paused(lambda: False, lambda: False))

    def test_wait_while_paused_returns_true_when_stop_signalled(self) -> None:
        self.assertTrue(_wait_while_paused(lambda: True, lambda: True, poll_interval=0.01))

    def test_wait_while_paused_releases_when_pause_cleared(self) -> None:
        calls = {"count": 0}

        def pause_check() -> bool:
            calls["count"] += 1
            return calls["count"] < 3

        self.assertFalse(_wait_while_paused(pause_check, lambda: False, poll_interval=0.01))
        self.assertGreaterEqual(calls["count"], 3)

    def test_wait_while_paused_noop_without_pause_check(self) -> None:
        self.assertFalse(_wait_while_paused(None, lambda: False))
        self.assertTrue(_wait_while_paused(None, lambda: True))


if __name__ == "__main__":
    unittest.main()
