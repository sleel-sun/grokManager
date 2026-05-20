import unittest

from app.products.web.admin.maintainer import (
    MaintainerRunRequest,
    _MaintainerController,
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

    def test_redact_state_hides_secret_values(self) -> None:
        redacted = redact_state(
            {
                "running": False,
                "email_admin_password": "worker-secret",
                "api_token": "admin-secret",
                "message": "done",
            }
        )

        self.assertEqual(redacted["email_admin_password"], "***")
        self.assertEqual(redacted["api_token"], "***")
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
                    "extract_numbers": True,
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

    def test_saved_config_response_clamps_workers(self) -> None:
        too_high = build_saved_config_response({"run": {"count": 1, "workers": 99}})
        self.assertEqual(too_high["workers"], 8)

        too_low = build_saved_config_response({"run": {"count": 1, "workers": 0}})
        self.assertEqual(too_low["workers"], 1)

        missing = build_saved_config_response({"run": {"count": 1}})
        self.assertEqual(missing["workers"], 1)

    def test_run_request_rejects_workers_out_of_range(self) -> None:
        with self.assertRaises(Exception):
            MaintainerRunRequest(
                count=1,
                workers=99,
                email_worker_domain="mail.example.com",
                email_domains=["example.com"],
                email_admin_password="pw",
            )

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
