import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.maintainer.runner import (
    _build_worker_output,
    _ensure_browser_storage_ready,
    _resolve_browser_tmp_path,
    _split_count,
    _wait_while_paused,
    run_batch_parallel,
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
    def test_split_count_assigns_count_to_every_worker(self) -> None:
        # New semantic: ``count`` is per-worker, total = count * workers.
        self.assertEqual(_split_count(10, 3), [10, 10, 10])
        self.assertEqual(_split_count(1, 5), [1, 1, 1, 1, 1])

    def test_split_count_returns_one_entry_per_worker_even_when_small(self) -> None:
        # workers=4 always spawns 4 entries; previously the helper silently
        # dropped workers with a 0 share, which is the bug users reported as
        # "selected parallel but registration still runs one by one".
        self.assertEqual(_split_count(2, 4), [2, 2, 2, 2])

    def test_split_count_with_zero_total_uses_unbounded_sentinels(self) -> None:
        # ``count == 0`` means "loop until stopped"; every worker gets 0.
        self.assertEqual(_split_count(0, 3), [0, 0, 0])

    def test_split_count_negative_count_treated_as_zero(self) -> None:
        self.assertEqual(_split_count(-5, 2), [0, 0])

    def test_split_count_zero_workers_returns_empty(self) -> None:
        self.assertEqual(_split_count(10, 0), [])

    def test_build_worker_output_appends_worker_suffix(self) -> None:
        base = Path("/tmp/sso_20260520.txt")
        self.assertEqual(_build_worker_output(base, 0), Path("/tmp/sso_20260520.w0.txt"))
        self.assertEqual(_build_worker_output(base, 2), Path("/tmp/sso_20260520.w2.txt"))


class RunBatchParallelSpawnTests(unittest.TestCase):
    def _build_ctx_mock(self, captured_processes: list[MagicMock]) -> MagicMock:
        """Return a context-like object that records every Process(...) call."""
        ctx = MagicMock()

        def make_process(*args: object, **kwargs: object) -> MagicMock:
            proc = MagicMock(spec=["start", "join", "pid", "exitcode"])
            proc.start = MagicMock()
            proc.join = MagicMock()
            proc.pid = 10000 + len(captured_processes)
            proc.exitcode = 0
            proc._ctor_args = args  # for assertions
            proc._ctor_kwargs = kwargs
            captured_processes.append(proc)
            return proc

        ctx.Process = MagicMock(side_effect=make_process)
        # Result queue that returns no entries (workers reported nothing).
        empty_queue = MagicMock()

        def get_nowait() -> tuple[int, list[str], None]:
            raise Exception("empty")

        empty_queue.get_nowait = MagicMock(side_effect=get_nowait)
        ctx.Queue = MagicMock(return_value=empty_queue)
        return ctx

    def test_spawns_exactly_n_processes_for_workers_n(self) -> None:
        captured: list[MagicMock] = []
        ctx = self._build_ctx_mock(captured)
        pause_event = MagicMock()
        pause_event.is_set = MagicMock(return_value=True)
        stop_event = MagicMock()

        spawned_seen: list[int] = []

        with patch("app.maintainer.runner.mp.get_context", return_value=ctx):
            run_batch_parallel(
                config_path="/tmp/fake-config.json",
                count=2,
                workers=5,
                output="/tmp/fake-sso.txt",
                pause_event=pause_event,
                stop_event=stop_event,
                spawned_workers_callback=spawned_seen.append,
            )

        self.assertEqual(len(captured), 5)
        for proc in captured:
            proc.start.assert_called_once()
            proc.join.assert_called_once()
        # Each worker received the full per-worker count, not a split share.
        worker_counts = [proc._ctor_kwargs["args"][2] for proc in captured]
        self.assertEqual(worker_counts, [2, 2, 2, 2, 2])
        # Worker IDs are sequential 0..N-1 — each spawn gets a unique id.
        worker_ids = [proc._ctor_kwargs["args"][0] for proc in captured]
        self.assertEqual(worker_ids, [0, 1, 2, 3, 4])
        # The orchestrator reported the actual number of spawned workers
        # back to the callback so the admin UI can surface it as
        # "spawned_workers" in the status response.
        self.assertEqual(spawned_seen, [5])

    def test_workers_one_does_not_spawn_subprocesses(self) -> None:
        captured: list[MagicMock] = []
        ctx = self._build_ctx_mock(captured)
        spawned_seen: list[int] = []

        with patch("app.maintainer.runner.mp.get_context", return_value=ctx), patch(
            "app.maintainer.runner.run_batch", return_value=["sso-x"]
        ) as run_batch_mock:
            tokens = run_batch_parallel(
                config_path="/tmp/fake-config.json",
                count=3,
                workers=1,
                output="/tmp/fake-sso.txt",
                spawned_workers_callback=spawned_seen.append,
            )

        self.assertEqual(tokens, ["sso-x"])
        run_batch_mock.assert_called_once()
        ctx.Process.assert_not_called()
        self.assertEqual(spawned_seen, [1])


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
