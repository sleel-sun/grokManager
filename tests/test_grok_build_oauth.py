import hashlib
import logging
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.maintainer.grok_build_oauth import (
    _proxy_log_label,
    authorize_device_with_sso,
    authorize_sso_account,
    authorize_sso_accounts,
    poll_device_token,
    refresh_due_pool_credentials,
    refresh_pool_credential,
    source_id_for_sso,
)


class GrokBuildOAuthTests(unittest.TestCase):
    def test_verify_and_approve_accept_body_success_markers(self) -> None:
        session = MagicMock()
        session.get.side_effect = [
            SimpleNamespace(url="https://accounts.x.ai/"),
            SimpleNamespace(url="https://auth.x.ai/device"),
        ]
        session.post.side_effect = [
            SimpleNamespace(url="https://auth.x.ai/device", text="Authorize Grok Build"),
            SimpleNamespace(url="https://auth.x.ai/device", text="Device authorized"),
        ]

        authorize_device_with_sso(session, "sso-secret", "https://verify", "ABCD")

    def test_poll_soft_retries_network_error(self) -> None:
        success = SimpleNamespace(
            status_code=200,
            json=lambda: {"access_token": "access"},
        )
        session = MagicMock()
        session.post.side_effect = [RuntimeError("proxy unavailable"), success]
        with (
            patch("app.maintainer.grok_build_oauth.time.monotonic", side_effect=[0, 0, 1]),
            patch("app.maintainer.grok_build_oauth.time.sleep") as sleep_mock,
        ):
            payload = poll_device_token(session, "device", expires_in=30, interval=1)

        self.assertEqual(payload["access_token"], "access")
        sleep_mock.assert_called_once_with(1)

    def test_poll_soft_retries_5xx_and_non_json(self) -> None:
        server_error = SimpleNamespace(status_code=503, json=lambda: {"error": "upstream"})
        non_json = SimpleNamespace(
            status_code=200,
            json=MagicMock(side_effect=ValueError("html")),
        )
        success = SimpleNamespace(status_code=200, json=lambda: {"access_token": "access"})
        session = MagicMock()
        session.post.side_effect = [server_error, non_json, success]
        with (
            patch(
                "app.maintainer.grok_build_oauth.time.monotonic",
                side_effect=[0, 0, 1, 2],
            ),
            patch("app.maintainer.grok_build_oauth.time.sleep") as sleep_mock,
        ):
            payload = poll_device_token(session, "device", expires_in=30, interval=2)

        self.assertEqual(payload["access_token"], "access")
        self.assertEqual(sleep_mock.call_count, 2)

    def test_authorize_account_applies_explicit_proxy(self) -> None:
        session = MagicMock()
        session.__enter__.return_value = session
        session.__exit__.return_value = None
        device = {
            "device_code": "device",
            "user_code": "code",
            "verification_uri": "https://verify",
            "expires_in": 30,
            "interval": 1,
        }
        with (
            patch("app.maintainer.grok_build_oauth.requests.Session", return_value=session),
            patch("app.maintainer.grok_build_oauth.request_device_code", return_value=device),
            patch("app.maintainer.grok_build_oauth.authorize_device_with_sso"),
            patch(
                "app.maintainer.grok_build_oauth.poll_device_token",
                return_value={"access_token": "access", "refresh_token": "refresh"},
            ),
            patch("app.maintainer.grok_build_oauth.save_pool_credential"),
        ):
            authorize_sso_account(
                "sso-secret",
                "source",
                proxy="http://user:password@proxy.example:8080",
            )

        self.assertEqual(
            session.proxies,
            {
                "http": "http://user:password@proxy.example:8080",
                "https": "http://user:password@proxy.example:8080",
            },
        )

    def test_batch_uses_sha256_ids_and_authorizes_sequentially(self) -> None:
        calls: list[tuple[str, str]] = []

        def fake_authorize(token: str, source_id: str, **_kwargs: object) -> dict[str, object]:
            calls.append((token, source_id))
            return {"source_id": source_id, "has_refresh_token": True}

        logger = MagicMock(spec=logging.Logger)
        with (
            patch(
                "app.maintainer.grok_build_oauth.authorize_sso_account",
                side_effect=fake_authorize,
            ),
            patch("app.maintainer.grok_build_oauth.time.sleep") as sleep_mock,
        ):
            results = authorize_sso_accounts(
                ["token-a", "token-b", "token-a"],
                delay_sec=2,
                proxy="http://user:password@proxy.example:8080",
                logger=logger,
            )

        self.assertEqual([item[0] for item in calls], ["token-a", "token-b"])
        self.assertEqual(
            [item[1] for item in calls],
            [
                "sso:" + hashlib.sha256(b"token-a").hexdigest()[:24],
                "sso:" + hashlib.sha256(b"token-b").hexdigest()[:24],
            ],
        )
        self.assertEqual(len(results), 2)
        sleep_mock.assert_called_once_with(2.0)
        log_text = str(logger.method_calls)
        self.assertNotIn("token-a", log_text)
        self.assertNotIn("password", log_text)

    def test_required_batch_raises_without_exposing_token(self) -> None:
        with patch(
            "app.maintainer.grok_build_oauth.authorize_sso_account",
            side_effect=RuntimeError("contains sso-secret"),
        ):
            with self.assertRaisesRegex(RuntimeError, "1 account") as raised:
                authorize_sso_accounts(["sso-secret"], required=True)

        self.assertNotIn("sso-secret", str(raised.exception))

    def test_source_id_is_sha256(self) -> None:
        self.assertEqual(
            source_id_for_sso(" token "),
            "sso:" + hashlib.sha256(b"token").hexdigest()[:24],
        )

    def test_proxy_log_label_redacts_credentials(self) -> None:
        label = _proxy_log_label("http://alice:secret@proxy.example:8080")
        self.assertEqual(label, "http://user:***@proxy.example:8080")
        self.assertNotIn("alice", label)
        self.assertNotIn("secret", label)

    def test_refresh_pool_credential_rotates_tokens_with_cas(self) -> None:
        session = MagicMock()
        session.__enter__.return_value = session
        session.__exit__.return_value = None
        session.post.return_value = SimpleNamespace(
            status_code=200,
            json=lambda: {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 7200,
            },
        )
        entry = {
            "key": "old-access",
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_at": 1000,
            "email": "build@example.com",
        }
        with (
            patch(
                "app.maintainer.grok_build_oauth.pool_entries",
                return_value={"sso:a": entry},
            ),
            patch(
                "app.maintainer.grok_build_oauth.requests.Session",
                return_value=session,
            ),
            patch(
                "app.maintainer.grok_build_oauth._oauth_config",
                return_value=("client", "https://auth/token", "scope"),
            ),
            patch(
                "app.maintainer.grok_build_oauth.pool_entry_refresh_lock",
                return_value=nullcontext(),
            ),
            patch(
                "app.maintainer.grok_build_oauth.time.time",
                side_effect=[2000.0, 2001.0],
            ),
            patch(
                "app.maintainer.grok_build_oauth.save_pool_entry_if_refresh_token",
                return_value=True,
            ) as save_mock,
        ):
            result = refresh_pool_credential("sso:a")

        saved = save_mock.call_args.args[1]
        self.assertEqual(saved["access_token"], "new-access")
        self.assertEqual(saved["refresh_token"], "new-refresh")
        self.assertEqual(saved["expires_at"], 9200.0)
        self.assertEqual(saved["updated_at"], 2001.0)
        self.assertEqual(saved["email"], "build@example.com")
        self.assertEqual(save_mock.call_args.args[2], "old-refresh")
        self.assertFalse(result["conflict"])

    def test_auto_refresh_only_selects_due_refreshable_entries(self) -> None:
        entries = {
            "sso:due": {
                "access_token": "a",
                "refresh_token": "r",
                "expires_at": 1100,
            },
            "sso:later": {
                "access_token": "b",
                "refresh_token": "r2",
                "expires_at": 5000,
            },
            "sso:no-refresh": {"access_token": "c", "expires_at": 1050},
        }
        with (
            patch("app.maintainer.grok_build_oauth.pool_entries", return_value=entries),
            patch("app.maintainer.grok_build_oauth.time.time", return_value=1000),
            patch(
                "app.maintainer.grok_build_oauth.refresh_pool_credential",
                return_value={"conflict": False},
            ) as refresh_mock,
        ):
            result = refresh_due_pool_credentials(refresh_before_expiry_s=300)

        refresh_mock.assert_called_once_with("sso:due")
        self.assertEqual(result["checked"], 3)
        self.assertEqual(result["eligible"], 2)
        self.assertEqual(result["due"], 1)
        self.assertEqual(result["refreshed"], 1)


if __name__ == "__main__":
    unittest.main()
