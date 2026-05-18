import unittest

from app.products.web.admin.maintainer import (
    MaintainerRunRequest,
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


if __name__ == "__main__":
    unittest.main()
