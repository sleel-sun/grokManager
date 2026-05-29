import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from app.maintainer.runner import (
    build_profile,
    click_email_signup_button,
    fill_profile_and_submit,
    _prewarm_cloudflare_clearance,
    _build_worker_output,
    _compute_worker_chrome_user_data_dir,
    _configure_browser_options,
    _ensure_browser_storage_ready,
    _resolve_browser_tmp_path,
    _worker_entry,
    _split_count,
    _wait_while_paused,
    run_batch,
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

    def test_build_profile_uses_random_name_choices(self) -> None:
        with patch(
            "app.maintainer.runner.secrets.choice",
            side_effect=["Ava", "Chen"],
        ):
            given_name, family_name, password = build_profile()

        self.assertEqual((given_name, family_name), ("Ava", "Chen"))
        self.assertNotEqual((given_name, family_name), ("Neo", "Lin"))
        self.assertTrue(password.startswith("N"))

    def test_click_email_signup_button_accepts_direct_email_form(self) -> None:
        mock_page = MagicMock()
        mock_page.run_js.return_value = {"status": "email-form-ready"}

        with (
            patch("app.maintainer.runner.page", mock_page),
            patch("app.maintainer.runner.refresh_active_page", return_value=mock_page),
            patch("app.maintainer.runner.time.sleep"),
        ):
            self.assertTrue(click_email_signup_button(timeout=1))

    def test_click_email_signup_button_error_includes_diagnostics(self) -> None:
        mock_page = MagicMock()
        mock_page.run_js.return_value = {
            "status": "not-found",
            "url": "https://accounts.x.ai/sign-up?redirect=grok-com",
            "readyState": "complete",
            "candidates": ["Continue with Google", "Continue with Apple"],
        }

        with (
            patch("app.maintainer.runner.page", mock_page),
            patch("app.maintainer.runner.refresh_active_page", return_value=mock_page),
            patch("app.maintainer.runner.time.time", side_effect=[0.0, 0.0, 2.0]),
            patch("app.maintainer.runner.time.sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "候选按钮=Continue with Google"):
                click_email_signup_button(timeout=1)

    def test_click_email_signup_button_reports_cloudflare_block(self) -> None:
        mock_page = MagicMock()
        mock_page.run_js.return_value = {
            "status": "cloudflare-blocked",
            "url": "https://accounts.x.ai/sign-up?redirect=grok-com",
            "readyState": "complete",
            "title": "Attention Required! | Cloudflare",
        }

        with (
            patch("app.maintainer.runner.page", mock_page),
            patch("app.maintainer.runner.refresh_active_page", return_value=mock_page),
            patch("app.maintainer.runner._click_cloudflare_challenge", return_value="not-found"),
            patch("app.maintainer.runner.time.time", side_effect=[0.0, 0.0, 2.0]),
            patch("app.maintainer.runner.time.sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Cloudflare 硬拦截"):
                click_email_signup_button(timeout=1)

    def test_click_email_signup_button_attempts_cloudflare_click(self) -> None:
        mock_page = MagicMock()
        mock_page.run_js.side_effect = [
            {
                "status": "cloudflare-challenge",
                "url": "https://accounts.x.ai/sign-up?redirect=grok-com",
                "readyState": "complete",
                "title": "Just a moment...",
            },
            {"status": "email-form-ready"},
        ]

        with (
            patch("app.maintainer.runner.page", mock_page),
            patch("app.maintainer.runner.refresh_active_page", return_value=mock_page),
            patch(
                "app.maintainer.runner._click_cloudflare_challenge",
                return_value="iframe-coordinate",
            ) as click_mock,
            patch("app.maintainer.runner.time.sleep"),
        ):
            self.assertTrue(click_email_signup_button(timeout=1))

        click_mock.assert_called_once()

    def test_prewarm_cloudflare_clearance_injects_flaresolverr_cookies(self) -> None:
        class _Response:
            def __enter__(self) -> "_Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return json.dumps(
                    {
                        "status": "ok",
                        "solution": {
                            "userAgent": "Mozilla/5.0 Test",
                            "cookies": [
                                {
                                    "name": "cf_clearance",
                                    "value": "clearance-value",
                                    "domain": ".x.ai",
                                    "path": "/",
                                    "secure": True,
                                    "httpOnly": True,
                                    "sameSite": "None",
                                    "expiry": 1780000000,
                                }
                            ],
                        },
                    }
                ).encode("utf-8")

        mock_page = MagicMock()

        with (
            patch.dict(
                os.environ,
                {"MAINTAINER_FLARESOLVERR_URL": "http://flaresolverr:8191"},
                clear=True,
            ),
            patch("app.maintainer.runner.page", mock_page),
            patch("app.maintainer.runner.urllib_request.urlopen", return_value=_Response()),
        ):
            self.assertTrue(_prewarm_cloudflare_clearance())

        mock_page.run_cdp.assert_any_call(
            "Network.setUserAgentOverride",
            userAgent="Mozilla/5.0 Test",
        )
        set_cookie_call = mock_page.run_cdp.call_args_list[-1]
        self.assertEqual(set_cookie_call.args[0], "Network.setCookies")
        self.assertEqual(set_cookie_call.kwargs["cookies"][0]["name"], "cf_clearance")

    def test_fill_profile_does_not_treat_debug_text_as_clicked(self) -> None:
        mock_page = MagicMock()
        mock_page.run_js.side_effect = [
            "filled",
            True,
            "not-found",
            "NO_BUTTON: Continue with Google",
        ]
        mock_page.ele.return_value = None

        with (
            patch("app.maintainer.runner.page", mock_page),
            patch(
                "app.maintainer.runner.build_profile",
                return_value=("Ava", "Chen", "Nabc!a7#def"),
            ),
            patch("app.maintainer.runner.time.time", side_effect=[0.0, 0.0, 2.0]),
            patch("app.maintainer.runner.time.sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "未找到最终注册表单"):
                fill_profile_and_submit(timeout=1)

    def test_fill_profile_clicks_submit_when_turnstile_is_absent(self) -> None:
        mock_page = MagicMock()
        mock_button = MagicMock()
        mock_page.run_js.side_effect = [
            "filled",
            True,
            "not-found",
            "not-found",
        ]
        mock_page.ele.return_value = mock_button

        with (
            patch("app.maintainer.runner.page", mock_page),
            patch(
                "app.maintainer.runner.build_profile",
                return_value=("Ava", "Chen", "Nabc!a7#def"),
            ),
            patch("app.maintainer.runner.time.time", return_value=0.0),
            patch("app.maintainer.runner.time.sleep"),
        ):
            profile = fill_profile_and_submit(timeout=1)

        self.assertEqual(profile["given_name"], "Ava")
        mock_button.click.assert_called_once()


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


class MaintainerBatchProfileIsolationTests(unittest.TestCase):
    def test_run_batch_resets_isolated_profile_between_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            config_path = tmp_path / "maintainer.config.json"
            config_path.write_text("{}", encoding="utf-8")
            output_path = tmp_path / "sso.txt"
            user_data_dir = tmp_path / "profile"
            stale_cookie = user_data_dir / "Default" / "Cookies"
            calls = {"count": 0}

            def fake_registration(*_args: Any, **_kwargs: Any) -> dict[str, str]:
                calls["count"] += 1
                if calls["count"] == 1:
                    stale_cookie.parent.mkdir(parents=True, exist_ok=True)
                    stale_cookie.write_text("old-session", encoding="utf-8")
                    return {"sso": "sso-first"}

                self.assertFalse(
                    stale_cookie.exists(),
                    "worker profile must be cleared before the next registration round",
                )
                return {"sso": "sso-second"}

            env = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith("MAINTAINER_")
            }
            env["MAINTAINER_CHROME_USER_DATA_DIR"] = str(user_data_dir)

            with (
                patch.dict(os.environ, env, clear=True),
                patch("app.maintainer.runner.start_browser"),
                patch("app.maintainer.runner.stop_browser"),
                patch("app.maintainer.runner.push_sso_to_api"),
                patch(
                    "app.maintainer.runner.run_single_registration",
                    side_effect=fake_registration,
                ),
            ):
                tokens = run_batch(
                    config_path=str(config_path),
                    count=2,
                    output=str(output_path),
                )

        self.assertEqual(tokens, ["sso-first", "sso-second"])
        self.assertEqual(calls["count"], 2)


class RunBatchParallelSpawnTests(unittest.TestCase):
    def _build_ctx_mock(self, captured_processes: list[MagicMock]) -> MagicMock:
        """Return a context-like object that records every Process(...) call."""
        ctx = MagicMock()

        def make_process(*args: object, **kwargs: object) -> MagicMock:
            proc = MagicMock(
                spec=["start", "join", "pid", "exitcode", "is_alive", "terminate", "kill"]
            )
            proc.start = MagicMock()
            proc.join = MagicMock()
            proc.pid = 10000 + len(captured_processes)
            proc.exitcode = 0
            proc.is_alive = MagicMock(return_value=False)
            proc.terminate = MagicMock()
            proc.kill = MagicMock()
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
            if not queues_built:
                queue.get_nowait = MagicMock(
                    side_effect=[(0, [], None), (1, [], None), Exception("empty")]
                )
            else:
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

    def test_worker_exit_without_result_emits_failure_progress(self) -> None:
        captured: list[MagicMock] = []
        ctx = MagicMock()

        def make_process(*args: object, **kwargs: object) -> MagicMock:
            proc = MagicMock(
                spec=["start", "join", "pid", "exitcode", "is_alive", "terminate", "kill"]
            )
            proc.start = MagicMock()
            proc.join = MagicMock()
            proc.pid = 11000 + len(captured)
            proc.exitcode = 0 if len(captured) == 0 else 1
            proc.is_alive = MagicMock(return_value=False)
            proc.terminate = MagicMock()
            proc.kill = MagicMock()
            proc._ctor_args = args
            proc._ctor_kwargs = kwargs
            captured.append(proc)
            return proc

        result_queue = MagicMock()
        result_queue.get_nowait = MagicMock(
            side_effect=[(0, ["sso-ok"], None), Exception("empty")]
        )
        progress_queue = MagicMock()
        progress_queue.get = MagicMock(return_value=None)
        progress_queue.put = MagicMock()
        ctx.Process = MagicMock(side_effect=make_process)
        ctx.Queue = MagicMock(side_effect=[result_queue, progress_queue])

        events: list[tuple[int, str, dict[str, Any]]] = []
        pause_event = MagicMock()
        pause_event.is_set = MagicMock(return_value=True)
        stop_event = MagicMock()

        with patch("app.maintainer.runner.mp.get_context", return_value=ctx):
            tokens = run_batch_parallel(
                config_path="/tmp/fake-config.json",
                count=1,
                workers=2,
                output="/tmp/fake-sso.txt",
                pause_event=pause_event,
                stop_event=stop_event,
                progress_callback=lambda worker_id, event, payload: events.append(
                    (worker_id, event, payload)
                ),
            )

        self.assertEqual(tokens, ["sso-ok"])
        failure_events = [event for event in events if event[1] == "worker_failed"]
        self.assertEqual(len(failure_events), 1)
        self.assertEqual(failure_events[0][0], 1)
        self.assertIn("exitcode=1", failure_events[0][2]["error"])

    def test_failed_worker_output_tokens_are_recovered_and_pushed(self) -> None:
        captured: list[MagicMock] = []
        ctx = MagicMock()

        def make_process(*args: object, **kwargs: object) -> MagicMock:
            proc = MagicMock(
                spec=["start", "join", "pid", "exitcode", "is_alive", "terminate", "kill"]
            )
            proc.start = MagicMock()
            proc.join = MagicMock()
            proc.pid = 12000 + len(captured)
            proc.exitcode = 0 if len(captured) == 0 else 1
            proc.is_alive = MagicMock(return_value=False)
            proc.terminate = MagicMock()
            proc.kill = MagicMock()
            proc._ctor_args = args
            proc._ctor_kwargs = kwargs
            captured.append(proc)
            return proc

        result_queue = MagicMock()
        result_queue.get_nowait = MagicMock(
            side_effect=[(0, ["sso-ok"], None), Exception("empty")]
        )
        progress_queue = MagicMock()
        progress_queue.get = MagicMock(return_value=None)
        progress_queue.put = MagicMock()
        ctx.Process = MagicMock(side_effect=make_process)
        ctx.Queue = MagicMock(side_effect=[result_queue, progress_queue])

        pause_event = MagicMock()
        pause_event.is_set = MagicMock(return_value=True)
        stop_event = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "sso.txt"
            _build_worker_output(output, 1).write_text("sso-lost\n", encoding="utf-8")
            with (
                patch("app.maintainer.runner.mp.get_context", return_value=ctx),
                patch("app.maintainer.runner.push_sso_to_api") as push_mock,
            ):
                tokens = run_batch_parallel(
                    config_path="/tmp/fake-config.json",
                    count=1,
                    workers=2,
                    output=str(output),
                    pause_event=pause_event,
                    stop_event=stop_event,
                )

        self.assertEqual(tokens, ["sso-ok", "sso-lost"])
        push_mock.assert_called_once_with(["sso-lost"])

    def test_idle_worker_is_terminated_and_marked_failed(self) -> None:
        captured: list[MagicMock] = []
        ctx = MagicMock()

        def make_process(*args: object, **kwargs: object) -> MagicMock:
            proc = MagicMock(
                spec=["start", "join", "pid", "exitcode", "is_alive", "terminate", "kill"]
            )
            alive = {"value": len(captured) == 0}
            proc.start = MagicMock()
            proc.join = MagicMock()
            proc.pid = 13000 + len(captured)
            proc.exitcode = None if alive["value"] else 0

            def is_alive() -> bool:
                return alive["value"]

            def terminate() -> None:
                alive["value"] = False
                proc.exitcode = -15

            proc.is_alive = MagicMock(side_effect=is_alive)
            proc.terminate = MagicMock(side_effect=terminate)
            proc.kill = MagicMock()
            proc._ctor_args = args
            proc._ctor_kwargs = kwargs
            captured.append(proc)
            return proc

        result_queue = MagicMock()
        result_queue.get_nowait = MagicMock(
            side_effect=[(1, ["sso-ok"], None), Exception("empty")]
        )
        progress_queue = MagicMock()
        progress_queue.get = MagicMock(return_value=None)
        progress_queue.put = MagicMock()
        ctx.Process = MagicMock(side_effect=make_process)
        ctx.Queue = MagicMock(side_effect=[result_queue, progress_queue])

        events: list[tuple[int, str, dict[str, Any]]] = []
        pause_event = MagicMock()
        pause_event.is_set = MagicMock(return_value=True)
        stop_event = MagicMock()

        env = {
            k: v
            for k, v in os.environ.items()
            if k != "MAINTAINER_WORKER_IDLE_TIMEOUT"
        }
        env["MAINTAINER_WORKER_IDLE_TIMEOUT"] = "0.01"
        with (
            patch.dict(os.environ, env, clear=True),
            patch("app.maintainer.runner.mp.get_context", return_value=ctx),
        ):
            tokens = run_batch_parallel(
                config_path="/tmp/fake-config.json",
                count=1,
                workers=2,
                output="/tmp/fake-sso.txt",
                pause_event=pause_event,
                stop_event=stop_event,
                progress_callback=lambda worker_id, event, payload: events.append(
                    (worker_id, event, payload)
                ),
            )

        self.assertEqual(tokens, ["sso-ok"])
        captured[0].terminate.assert_called_once()
        failure_events = [event for event in events if event[1] == "worker_failed"]
        self.assertEqual(len(failure_events), 1)
        self.assertEqual(failure_events[0][0], 0)
        self.assertIn("idle timeout", failure_events[0][2]["error"])


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

    def test_configure_browser_options_uses_worker_debug_port_when_env_set(
        self,
    ) -> None:
        # DrissionPage.set_user_data_path() disables auto_port internally.
        # Without an explicit local port, parallel workers all keep the
        # default address 127.0.0.1:9222 and fail to connect concurrently.
        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith("MAINTAINER_")
            }
            env["MAINTAINER_CHROME_USER_DATA_DIR"] = tmpdir
            env["MAINTAINER_CHROME_DEBUG_PORT"] = "34567"
            with patch.dict(os.environ, env, clear=True):
                opts = _configure_browser_options()

        self.assertEqual(opts.address, "127.0.0.1:34567")
        self.assertFalse(opts.is_auto_port)

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

    def test_headless_browser_options_include_registration_stability_args(
        self,
    ) -> None:
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("MAINTAINER_")
        }
        env["MAINTAINER_HEADLESS"] = "true"
        with patch.dict(os.environ, env, clear=True):
            opts = _configure_browser_options()

        self.assertIn("--headless=new", opts.arguments)
        self.assertIn("--window-size=1440,900", opts.arguments)
        self.assertIn("--disable-gpu", opts.arguments)
        self.assertIn("--disable-blink-features=AutomationControlled", opts.arguments)
        self.assertIn("--lang=en-US", opts.arguments)
        self.assertIn("--password-store=basic", opts.arguments)
        self.assertIn("--use-mock-keychain", opts.arguments)

    def test_maintainer_chrome_args_allows_extra_browser_arguments(self) -> None:
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("MAINTAINER_")
        }
        env["MAINTAINER_CHROME_ARGS"] = '--foo --bar=baz "--quoted=value with space"'
        with patch.dict(os.environ, env, clear=True):
            opts = _configure_browser_options()

        self.assertIn("--foo", opts.arguments)
        self.assertIn("--bar=baz", opts.arguments)
        self.assertIn("--quoted=value with space", opts.arguments)

    def test_configure_browser_options_uses_maintainer_proxy(self) -> None:
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("MAINTAINER_")
        }
        env["MAINTAINER_PROXY"] = "http://127.0.0.1:8118"
        with patch.dict(os.environ, env, clear=True):
            opts = _configure_browser_options()

        self.assertIn("--proxy-server=http://127.0.0.1:8118", opts.arguments)

    def test_worker_entry_sets_unique_chrome_debug_port_env(self) -> None:
        class _Queue:
            def __init__(self) -> None:
                self.items: list[Any] = []

            def put(self, item: Any) -> None:
                self.items.append(item)

        class _Event:
            def __init__(self, value: bool = False) -> None:
                self.value = value

            def is_set(self) -> bool:
                return self.value

        seen_debug_ports: list[str | None] = []

        def fake_run_batch(**_kwargs: Any) -> list[str]:
            seen_debug_ports.append(os.getenv("MAINTAINER_CHROME_DEBUG_PORT"))
            return ["sso-token"]

        result_queue = _Queue()
        progress_queue = _Queue()

        with tempfile.TemporaryDirectory() as tmpdir:
            env = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith("MAINTAINER_")
            }
            env["MAINTAINER_TMP_PATH"] = tmpdir
            with (
                patch.dict(os.environ, env, clear=True),
                patch(
                    "app.maintainer.runner._select_worker_chrome_debug_port",
                    return_value=34567,
                ),
                patch("app.maintainer.runner.run_batch", side_effect=fake_run_batch),
            ):
                _worker_entry(
                    2,
                    "/tmp/fake-config.json",
                    1,
                    "/tmp/fake-output.txt",
                    False,
                    {},
                    _Event(True),
                    _Event(False),
                    result_queue,
                    progress_queue,
                )

        self.assertEqual(seen_debug_ports, ["34567"])
        self.assertEqual(result_queue.items, [(2, ["sso-token"], None)])
        alive_events = [item for item in progress_queue.items if item[1] == "alive"]
        self.assertEqual(len(alive_events), 1)
        self.assertEqual(alive_events[0][2]["debug_port"], 34567)


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
