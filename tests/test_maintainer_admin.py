import unittest
from pathlib import Path
from unittest.mock import patch

from app.products.web.admin.maintainer import (
    MaintainerRunRequest,
    _MaintainerController,
    _env_for_request,
    browser_mode_for_request,
    build_completion_status,
    build_gpt_runtime_config,
    build_saved_config_response,
    build_runtime_config,
    redact_state,
)


class MaintainerAdminTests(unittest.TestCase):
    def test_build_runtime_config_targets_admin_token_add_endpoint(self) -> None:
        req = MaintainerRunRequest(
            count=2,
            workers=3,
            email_worker_domain="mail.example.com",
            email_domains=["example.com", "mail.example.com"],
            email_admin_password="worker-secret",
            pool="basic",
            turnstile_manual_wait_sec=120,
            turnstile_solver_provider="capsolver",
            turnstile_solver_api_key="solver-secret",
            turnstile_solver_timeout_sec=180,
            turnstile_solver_poll_sec=4,
        )

        cfg = build_runtime_config(
            req,
            base_url="http://127.0.0.1:8000/",
            admin_token="admin-secret",
        )

        self.assertEqual(cfg["run"]["count"], 2)
        self.assertEqual(cfg["run"]["workers"], 3)
        self.assertEqual(cfg["email"]["worker_domain"], "mail.example.com")
        self.assertEqual(cfg["email"]["email_domains"], ["example.com", "mail.example.com"])
        self.assertEqual(cfg["email"]["admin_password"], "worker-secret")
        self.assertEqual(cfg["api"]["endpoint"], "http://127.0.0.1:8000/admin/api/tokens/add")
        self.assertEqual(cfg["api"]["token"], "admin-secret")
        self.assertEqual(cfg["api"]["pool"], "basic")
        self.assertTrue(cfg["api"]["append"])
        self.assertEqual(cfg["web"]["turnstile_manual_wait_sec"], 120)
        self.assertEqual(cfg["web"]["turnstile_solver_provider"], "capsolver")
        self.assertEqual(cfg["web"]["turnstile_solver_api_key"], "solver-secret")
        self.assertEqual(cfg["web"]["turnstile_solver_timeout_sec"], 180)
        self.assertEqual(cfg["web"]["turnstile_solver_poll_sec"], 4)

    def test_build_gpt_runtime_config_targets_gpt_accounts_endpoint(self) -> None:
        req = MaintainerRunRequest(
            count=2,
            workers=3,
            email_worker_domain="mail.example.com",
            email_domains=["example.com"],
            email_admin_password="worker-secret",
            gpt_fixed_password="FixedGPT!123",
        )

        cfg = build_gpt_runtime_config(
            req,
            base_url="http://127.0.0.1:8000/",
            admin_token="admin-secret",
        )

        self.assertEqual(cfg["api"]["endpoint"], "http://127.0.0.1:8000/admin/api/gpt/accounts")
        self.assertEqual(cfg["api"]["token"], "admin-secret")
        self.assertEqual(cfg["run"], {"count": 2, "workers": 3})
        self.assertTrue(cfg["gpt"]["auto_oauth_after_register"])
        self.assertTrue(cfg["gpt"]["save_credentials_on_failure"])
        self.assertEqual(cfg["gpt"]["fixed_password"], "FixedGPT!123")

    def test_redact_state_hides_secret_values(self) -> None:
        redacted = redact_state(
            {
                "running": False,
                "email_admin_password": "worker-secret",
                "api_token": "admin-secret",
                "turnstile_solver_api_key": "solver-secret",
                "message": "done",
            }
        )

        self.assertEqual(redacted["email_admin_password"], "***")
        self.assertEqual(redacted["api_token"], "***")
        self.assertEqual(redacted["turnstile_solver_api_key"], "***")
        self.assertEqual(redacted["message"], "done")

    def test_saved_config_response_does_not_expose_password(self) -> None:
        response = build_saved_config_response(
            {
                "email": {
                    "worker_domain": "mail.example.com",
                    "email_domains": ["example.com"],
                    "admin_password": "worker-secret",
                    "verify_ssl": True,
                },
                "api": {"pool": "super"},
                "run": {"count": 3, "workers": 4},
                "web": {
                    "headless": True,
                    "use_xvfb": False,
                    "no_sandbox": True,
                    "disable_dev_shm": False,
                    "window_size": "1280,800",
                    "turnstile_manual_wait_sec": 90,
                    "turnstile_solver_provider": "2captcha",
                    "turnstile_solver_api_key": "solver-secret",
                    "turnstile_solver_timeout_sec": 180,
                    "turnstile_solver_poll_sec": 6,
                    "extract_numbers": True,
                },
                "gpt": {
                    "fixed_password": "gpt-password-secret",
                },
            }
        )

        self.assertEqual(response["email_worker_domain"], "mail.example.com")
        self.assertEqual(response["email_domains"], ["example.com"])
        self.assertTrue(response["has_email_admin_password"])
        self.assertNotIn("worker-secret", str(response))
        self.assertEqual(response["pool"], "super")
        self.assertEqual(response["count"], 3)
        self.assertEqual(response["workers"], 4)
        self.assertTrue(response["headless"])
        self.assertEqual(response["window_size"], "1280,800")
        self.assertEqual(response["turnstile_manual_wait_sec"], 90)
        self.assertEqual(response["turnstile_solver_provider"], "2captcha")
        self.assertTrue(response["has_turnstile_solver_api_key"])
        self.assertEqual(response["turnstile_solver_timeout_sec"], 180)
        self.assertEqual(response["turnstile_solver_poll_sec"], 6)
        self.assertTrue(response["has_gpt_fixed_password"])
        self.assertNotIn("solver-secret", str(response))
        self.assertNotIn("gpt-password-secret", str(response))

    def test_env_for_request_passes_manual_turnstile_wait(self) -> None:
        req = MaintainerRunRequest(
            count=1,
            workers=1,
            email_worker_domain="mail.example.com",
            email_domains=["example.com"],
            email_admin_password="pw",
            use_xvfb=True,
            turnstile_manual_wait_sec=180,
            turnstile_solver_provider="capsolver",
            turnstile_solver_api_key="solver-secret",
            turnstile_solver_timeout_sec=120,
            turnstile_solver_poll_sec=3,
        )

        with patch.dict(
            "os.environ",
            {
                "MAINTAINER_PROXY": "http://privoxy:8118",
                "MAINTAINER_FLARESOLVERR_URL": "http://flaresolverr:8191",
                "MAINTAINER_FLARESOLVERR_TIMEOUT_SEC": "60",
            },
            clear=True,
        ):
            env = _env_for_request(req, Path("/tmp/cfg.json"))

        self.assertEqual(env["MAINTAINER_TMP_PATH"], "/tmp/grokmanager-web-maintainer")
        self.assertEqual(
            env["MAINTAINER_CHROME_USER_DATA_DIR"],
            "/tmp/grokmanager-web-maintainer/chrome-profile",
        )
        self.assertEqual(env["MAINTAINER_BROWSER_PATH"], "/usr/bin/chromium-browser")
        self.assertEqual(env["MAINTAINER_PROXY"], "http://privoxy:8118")
        self.assertEqual(env["MAINTAINER_FLARESOLVERR_URL"], "http://flaresolverr:8191")
        self.assertEqual(env["MAINTAINER_FLARESOLVERR_TIMEOUT_SEC"], "60")
        self.assertEqual(env["MAINTAINER_TURNSTILE_MANUAL_WAIT_SEC"], "180")
        self.assertEqual(env["MAINTAINER_TURNSTILE_SOLVER_PROVIDER"], "capsolver")
        self.assertEqual(env["MAINTAINER_TURNSTILE_SOLVER_API_KEY"], "solver-secret")
        self.assertEqual(env["MAINTAINER_TURNSTILE_SOLVER_TIMEOUT_SEC"], "120")
        self.assertEqual(env["MAINTAINER_TURNSTILE_SOLVER_POLL_SEC"], "3")
        self.assertEqual(env["MAINTAINER_HEADLESS"], "false")

    def test_env_for_request_does_not_force_headless_on_macos_without_display(self) -> None:
        req = MaintainerRunRequest(
            count=1,
            workers=1,
            email_worker_domain="mail.example.com",
            email_domains=["example.com"],
            email_admin_password="pw",
        )

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("app.products.web.admin.maintainer.sys.platform", "darwin"),
        ):
            env = _env_for_request(req, Path("/tmp/cfg.json"))
            mode = browser_mode_for_request(req)

        self.assertEqual(env["MAINTAINER_HEADLESS"], "false")
        self.assertEqual(mode["browser_mode"], "visible")
        self.assertTrue(mode["browser_visible"])

    def test_browser_mode_explains_hidden_browser_modes(self) -> None:
        base = {
            "count": 1,
            "workers": 1,
            "email_worker_domain": "mail.example.com",
            "email_domains": ["example.com"],
            "email_admin_password": "pw",
        }

        self.assertEqual(
            browser_mode_for_request(MaintainerRunRequest(**base, headless=True))["browser_mode"],
            "headless",
        )
        self.assertEqual(
            browser_mode_for_request(MaintainerRunRequest(**base, use_xvfb=True))["browser_mode"],
            "xvfb",
        )

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("app.products.web.admin.maintainer.sys.platform", "linux"),
        ):
            mode = browser_mode_for_request(MaintainerRunRequest(**base))

        self.assertEqual(mode["browser_mode"], "auto_headless")
        self.assertFalse(mode["browser_visible"])

    def test_runtime_config_reuses_saved_turnstile_solver_key_when_request_omits_it(self) -> None:
        req = MaintainerRunRequest(
            count=1,
            email_worker_domain="mail.example.com",
            email_domains=["example.com"],
            email_admin_password="pw",
            turnstile_solver_provider="capsolver",
            turnstile_solver_api_key="",
        )

        cfg = build_runtime_config(
            req,
            base_url="http://127.0.0.1:8000/",
            admin_token="admin-secret",
            existing_config={
                "web": {"turnstile_solver_api_key": "saved-solver-secret"}
            },
        )

        self.assertEqual(cfg["web"]["turnstile_solver_api_key"], "saved-solver-secret")

    def test_saved_config_response_preserves_high_workers_unclamped(self) -> None:
        # Removing the historical upper cap (8) was the only way to fix the
        # user complaint "并发 worker 数没有生效" — submitting workers=10
        # was silently clamped to 8 here and the UI never showed the
        # difference. Now the helper must surface the saved value as-is.
        large = build_saved_config_response({"run": {"count": 1, "workers": 99}})
        self.assertEqual(large["workers"], 99)

        # ge=1 floor stays — zero / negative is meaningless.
        too_low = build_saved_config_response({"run": {"count": 1, "workers": 0}})
        self.assertEqual(too_low["workers"], 1)

        missing = build_saved_config_response({"run": {"count": 1}})
        self.assertEqual(missing["workers"], 1)

    def test_saved_config_response_preserves_high_count_unclamped(self) -> None:
        # ``count`` (registration rounds per worker) used to be capped at 100
        # which silently truncated large batch jobs. Operators must be able
        # to schedule larger batches — the cap is now gone.
        large = build_saved_config_response({"run": {"count": 500, "workers": 1}})
        self.assertEqual(large["count"], 500)

    def test_empty_saved_config_defaults_to_linux_safe_browser_options_in_container(self) -> None:
        with patch("app.products.web.admin.maintainer._running_in_container", return_value=True):
            response = build_saved_config_response({})

        self.assertFalse(response["headless"])
        self.assertTrue(response["use_xvfb"])
        self.assertTrue(response["no_sandbox"])
        self.assertTrue(response["disable_dev_shm"])

    def test_run_request_accepts_workers_above_old_cap(self) -> None:
        # Pydantic no longer rejects workers>8. Operators may schedule any
        # positive integer; spawning capacity is the operator's concern.
        req = MaintainerRunRequest(
            count=1,
            workers=99,
            email_worker_domain="mail.example.com",
            email_domains=["example.com"],
            email_admin_password="pw",
        )
        self.assertEqual(req.workers, 99)

        # And count>100 is also accepted now.
        req2 = MaintainerRunRequest(
            count=500,
            workers=1,
            email_worker_domain="mail.example.com",
            email_domains=["example.com"],
            email_admin_password="pw",
        )
        self.assertEqual(req2.count, 500)

    def test_run_request_still_rejects_non_positive_workers(self) -> None:
        # ge=1 floor is the only validation we keep. Zero would mean
        # "spawn no workers" which has no useful semantics.
        with self.assertRaises(Exception):
            MaintainerRunRequest(
                count=1,
                workers=0,
                email_worker_domain="mail.example.com",
                email_domains=["example.com"],
                email_admin_password="pw",
            )

    def test_runtime_config_reuses_saved_password_when_request_omits_it(self) -> None:
        req = MaintainerRunRequest(
            count=1,
            email_worker_domain="mail.example.com",
            email_domains=["example.com"],
            email_admin_password="",
            pool="basic",
        )

        cfg = build_runtime_config(
            req,
            base_url="http://127.0.0.1:8000/",
            admin_token="admin-secret",
            existing_config={"email": {"admin_password": "saved-worker-secret"}},
        )

        self.assertEqual(cfg["email"]["admin_password"], "saved-worker-secret")

    def test_completion_status_treats_empty_tokens_as_failed(self) -> None:
        status, message = build_completion_status(
            [],
            stopped=False,
            progress={"0": {"last_error": "round#1: RuntimeError: button missing"}},
        )

        self.assertEqual(status, "failed")
        self.assertIn("注册任务未采集到 token", message)
        self.assertIn("button missing", message)

    def test_completion_status_reports_success_only_with_tokens(self) -> None:
        status, message = build_completion_status(["sso-value"], stopped=False)

        self.assertEqual(status, "completed")
        self.assertEqual(message, "注册任务完成，采集 1 个 token")


class MaintainerControllerTests(unittest.TestCase):
    def test_controller_pause_resume_stop_lifecycle(self) -> None:
        ctrl = _MaintainerController()

        self.assertFalse(ctrl.is_paused())
        self.assertFalse(ctrl.is_stopped())

        ctrl.pause()
        self.assertTrue(ctrl.is_paused())
        self.assertFalse(ctrl.is_stopped())

        ctrl.resume()
        self.assertFalse(ctrl.is_paused())

        # Stop unblocks any pending pause so workers do not deadlock.
        ctrl.pause()
        ctrl.stop()
        self.assertFalse(ctrl.is_paused())
        self.assertTrue(ctrl.is_stopped())

    def test_controller_reset_clears_pause_and_stop(self) -> None:
        ctrl = _MaintainerController()
        ctrl.pause()
        ctrl.stop()
        ctrl.reset()

        self.assertFalse(ctrl.is_paused())
        self.assertFalse(ctrl.is_stopped())


if __name__ == "__main__":
    unittest.main()
