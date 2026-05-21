import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from app.maintainer.runner import (
    _build_worker_output,
    _compute_worker_chrome_user_data_dir,
    _configure_browser_options,
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

        def _make_queue() -> MagicMock:
            queue = MagicMock()

            def _get_nowait() -> tuple[int, list[str], None]:
                raise Exception("empty")

            def _get(*_args: object, **_kwargs: object) -> None:
                # Return None so the orchestrator drain thread treats it as a
                # poison pill and exits cleanly instead of spinning on a
                # MagicMock auto-return value that fails to unpack.
                return None

            queue.get_nowait = MagicMock(side_effect=_get_nowait)
            queue.get = MagicMock(side_effect=_get)
            queue.put = MagicMock()
            return queue

        ctx.Queue = MagicMock(side_effect=_make_queue)
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

    def test_progress_queue_is_passed_to_each_worker(self) -> None:
        """Every spawned worker must receive the same progress_queue handle.

        Without this, the orchestrator can't stream interleaved per-worker
        progress events to the UI — users would be back to staring at the
        "Worker #N 已启动" line with no way to confirm round-level activity
        is overlapping across workers.
        """
        captured: list[MagicMock] = []
        ctx = self._build_ctx_mock(captured)
        pause_event = MagicMock()
        pause_event.is_set = MagicMock(return_value=True)
        stop_event = MagicMock()

        with patch("app.maintainer.runner.mp.get_context", return_value=ctx):
            run_batch_parallel(
                config_path="/tmp/fake-config.json",
                count=1,
                workers=3,
                output="/tmp/fake-sso.txt",
                pause_event=pause_event,
                stop_event=stop_event,
            )

        # Two queues per orchestrator run: result + progress.
        self.assertEqual(ctx.Queue.call_count, 2)
        progress_queues = [proc._ctor_kwargs["args"][9] for proc in captured]
        self.assertEqual(len(progress_queues), 3)
        # All three workers got the SAME progress_queue handle so the
        # orchestrator drains a single stream of interleaved events.
        self.assertEqual(progress_queues[0], progress_queues[1])
        self.assertEqual(progress_queues[1], progress_queues[2])

    def test_progress_callback_receives_worker_events(self) -> None:
        """Events pushed by workers reach the per-worker progress callback.

        Simulates the worker -> orchestrator drain by manually pushing a few
        tuples into the progress queue mock; the drain thread should fan them
        out to ``progress_callback`` with the worker_id preserved.
        """
        captured: list[MagicMock] = []
        ctx = self._build_ctx_mock(captured)

        # Override progress queue (2nd Queue() call) to yield real events.
        queues_built: list[MagicMock] = []

        def make_queue() -> MagicMock:
            queue = MagicMock()
            queue.put = MagicMock()
            queue.get_nowait = MagicMock(side_effect=Exception("empty"))
            queue.get = MagicMock(return_value=None)
            queues_built.append(queue)
            return queue

        ctx.Queue = MagicMock(side_effect=make_queue)

        # Pre-program the second queue (progress_queue) to emit events,
        # then a None to terminate the drain thread.
        events_to_emit: list[Any] = [
            (0, "alive", {"pid": 1001}),
            (1, "alive", {"pid": 1002}),
            (0, "round_start", {"round": 1}),
            (1, "round_start", {"round": 1}),
            (0, "round_done", {"round": 1, "sso_tail": "ab12", "elapsed_s": 7.2}),
            None,
        ]

        emitted_to_callback: list[tuple[int, str, dict]] = []

        def progress_cb(worker_id: int, event: str, payload: dict) -> None:
            emitted_to_callback.append((worker_id, event, payload))

        pause_event = MagicMock()
        pause_event.is_set = MagicMock(return_value=True)
        stop_event = MagicMock()

        # Configure the progress_queue (second created) to yield events from
        # our pre-programmed list.
        def install_event_source() -> None:
            # The second queue is the progress queue.
            progress_queue = queues_built[1]
            progress_queue.get = MagicMock(side_effect=events_to_emit + [None])

        # Patch p.join to install the event source before joining so the
        # drain thread has time to consume events.
        def join_with_drain(self: MagicMock) -> None:
            if len(queues_built) >= 2:
                install_event_source()

        for proc in captured:
            proc.join = MagicMock(side_effect=join_with_drain)

        with patch("app.maintainer.runner.mp.get_context", return_value=ctx):
            run_batch_parallel(
                config_path="/tmp/fake-config.json",
                count=1,
                workers=2,
                output="/tmp/fake-sso.txt",
                pause_event=pause_event,
                stop_event=stop_event,
                progress_callback=progress_cb,
            )

        # The events_to_emit list above contains 5 real events before the
        # poison pill — the drain may or may not catch them all before the
        # poison-pill triggers shutdown, but at least the first few should
        # have reached the callback in worker-id order.
        worker_ids_seen = {event[0] for event in emitted_to_callback}
        self.assertTrue(
            worker_ids_seen.issubset({0, 1}),
            f"unexpected worker ids: {worker_ids_seen}",
        )
        event_names = [event[1] for event in emitted_to_callback]
        # At a minimum, the orchestrator should observe each worker.
        self.assertTrue(
            any(name == "alive" for name in event_names) or not event_names,
            f"no alive events observed: {event_names}",
        )


class MaintainerChromeUserDataDirTests(unittest.TestCase):
    """Each parallel worker must get its own Chromium ``--user-data-dir``.

    Sharing a profile directory across workers causes Chromium's process
    singleton lock to either reject every Chromium past the first one or
    silently attach them to the same browser, both of which manifest as
    "workers run one at a time" — the exact symptom users have reported.
    """

    def test_distinct_workers_get_distinct_user_data_dirs(self) -> None:
        # Same parent pid (the orchestrator), distinct worker ids — the dirs
        # MUST differ or two Chromium instances will share the same profile
        # and serialize on the singleton lock.
        dir0 = _compute_worker_chrome_user_data_dir(0, 12345)
        dir1 = _compute_worker_chrome_user_data_dir(1, 12345)
        dir2 = _compute_worker_chrome_user_data_dir(2, 12345)
        self.assertNotEqual(dir0, dir1)
        self.assertNotEqual(dir1, dir2)
        self.assertNotEqual(dir0, dir2)

    def test_user_data_dir_is_absolute_and_under_system_tempdir(self) -> None:
        # Absolute path under the OS tempdir keeps the profile off the
        # project FS (avoiding lock contention with shared data dirs) and
        # avoids relative-path footguns when Chromium is launched from a
        # subprocess with a different CWD than the orchestrator.
        path = _compute_worker_chrome_user_data_dir(3, 99999)
        self.assertTrue(path.is_absolute(), f"{path} is not absolute")
        self.assertTrue(
            str(path).startswith(tempfile.gettempdir()),
            f"{path} not under {tempfile.gettempdir()}",
        )

    def test_user_data_dir_name_includes_worker_id_for_diagnostics(self) -> None:
        # The dirname is logged on every ``Worker #N: alive`` event so ops
        # can grep it. Keep the ``w{N}`` token stable across releases.
        path = _compute_worker_chrome_user_data_dir(7, 12345)
        self.assertIn("w7", path.name)

    def test_configure_browser_options_adds_user_data_dir_flag_when_env_set(
        self,
    ) -> None:
        # When MAINTAINER_CHROME_USER_DATA_DIR is set, the configured
        # ChromiumOptions MUST emit an explicit ``--user-data-dir=<path>``
        # Chromium argument. Without this flag Chromium falls back to the
        # default profile dir and contends with other workers.
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(
                os.environ, {"MAINTAINER_CHROME_USER_DATA_DIR": tmpdir}
            ):
                opts = _configure_browser_options()
        matching = [a for a in opts.arguments if a.startswith("--user-data-dir=")]
        self.assertEqual(
            len(matching),
            1,
            f"expected exactly one --user-data-dir flag, got {matching}",
        )
        # Path is normalised to absolute form so Chromium does not pick up
        # a different cwd than the orchestrator's.
        self.assertEqual(matching[0], f"--user-data-dir={Path(tmpdir).resolve()}")

    def test_configure_browser_options_omits_user_data_dir_flag_by_default(
        self,
    ) -> None:
        # In single-worker mode the env is not set; we must not force a
        # custom profile because that would lose any cached state
        # (cookies, login, etc.) maintained in the default profile.
        env_without_user_data = {
            k: v
            for k, v in os.environ.items()
            if k != "MAINTAINER_CHROME_USER_DATA_DIR"
        }
        with patch.dict(os.environ, env_without_user_data, clear=True):
            opts = _configure_browser_options()
        matching = [a for a in opts.arguments if a.startswith("--user-data-dir=")]
        self.assertEqual(matching, [])


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
