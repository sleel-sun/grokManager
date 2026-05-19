import unittest

from app.products.web.admin.maintainer import (
    MaintainerRunRequest,
    build_progress_fields,
    build_saved_config_response,
    build_runtime_config,
    redact_state,
)


class MaintainerAdminTests(unittest.TestCase):
    def test_build_runtime_config_targets_admin_token_add_endpoint(self) -> None:
        req = MaintainerRunRequest(
            count=2,
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
        self.assertEqual(cfg["email"]["worker_domain"], "mail.example.com")
        self.assertEqual(cfg["email"]["email_domains"], ["example.com", "mail.example.com"])
        self.assertEqual(cfg["email"]["admin_password"], "worker-secret")
        self.assertEqual(cfg["api"]["endpoint"], "http://127.0.0.1:8000/admin/api/tokens/add")
        self.assertEqual(cfg["api"]["token"], "admin-secret")
        self.assertEqual(cfg["api"]["pool"], "basic")
        self.assertTrue(cfg["api"]["append"])

    def test_run_request_allows_unlimited_and_large_registration_counts(self) -> None:
        unlimited = MaintainerRunRequest(
            count=0,
            email_worker_domain="mail.example.com",
            email_domains=["example.com"],
            email_admin_password="worker-secret",
            pool="basic",
        )
        large_batch = MaintainerRunRequest(
            count=250,
            email_worker_domain="mail.example.com",
            email_domains=["example.com"],
            email_admin_password="worker-secret",
            pool="basic",
        )

        self.assertEqual(unlimited.count, 0)
        self.assertEqual(large_batch.count, 250)

    def test_saved_config_response_preserves_unlimited_and_large_counts(self) -> None:
        unlimited = build_saved_config_response({"run": {"count": 0}})
        large_batch = build_saved_config_response({"run": {"count": 250}})

        self.assertEqual(unlimited["count"], 0)
        self.assertEqual(large_batch["count"], 250)

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
                "run": {"count": 3},
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
        self.assertTrue(response["headless"])
        self.assertEqual(response["window_size"], "1280,800")

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

    def test_progress_fields_include_remaining_count(self) -> None:
        progress = build_progress_fields(
            total_count=5,
            completed_count=2,
            token_count=1,
            current_round=3,
        )

        self.assertEqual(progress["total_count"], 5)
        self.assertEqual(progress["completed_count"], 2)
        self.assertEqual(progress["remaining_count"], 3)
        self.assertEqual(progress["current_round"], 3)
        self.assertEqual(progress["token_count"], 1)
        self.assertEqual(progress["progress_percent"], 40)


if __name__ == "__main__":
    unittest.main()
