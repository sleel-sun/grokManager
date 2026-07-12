import hashlib
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.maintainer.grok_build_oauth import (
    _proxy_log_label,
    authorize_device_with_sso,
    authorize_sso_account,
    authorize_sso_accounts,
    poll_device_token,
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


if __name__ == "__main__":
    unittest.main()
