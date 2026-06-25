from __future__ import annotations

import pytest

from app.maintainer import gpt


def test_single_gpt_registration_pushes_available_account(monkeypatch) -> None:
    class FakeClient:
        def run_register(self, email: str, password: str, mail_token: str, **_kwargs):
            assert email == "user@example.com"
            assert password
            assert mail_token == "mail-token"
            return False, "123456"

        def session_access_token(self) -> str:
            return "chatgpt-session-token"

    pushed: list[gpt.GPTRegistrationResult] = []
    monkeypatch.setattr(
        gpt,
        "_create_temp_email_from_config",
        lambda _conf: ("user@example.com", "mail-token"),
    )

    result = gpt.run_single_gpt_registration(
        {"api": {}},
        client_factory=FakeClient,
        push_account=lambda _conf, item: pushed.append(item),
    )

    assert result.status == "available"
    assert result.access_token == "chatgpt-session-token"
    assert pushed == [result]
    payload = gpt._gpt_account_payload(result)
    assert payload["access_token"] == "chatgpt-session-token"
    assert payload["email"] == "user@example.com"
    assert payload["mail_token"] == "mail-token"


def test_single_gpt_registration_logs_in_after_register_when_session_token_missing(monkeypatch) -> None:
    calls: list[str] = []

    class RegisterClient:
        def run_register(self, email: str, password: str, mail_token: str, **_kwargs):
            calls.append("register")
            return False, "111111"

        def session_access_token(self) -> str:
            return ""

    class LoginClient:
        def login_with_otp(
            self,
            email: str,
            password: str,
            mail_token: str,
            *,
            ignore_otp: str | None = None,
            **_kwargs,
        ) -> str:
            calls.append("login")
            assert ignore_otp == "111111"
            return "fresh-login-token"

    clients = iter([RegisterClient(), LoginClient()])
    pushed: list[gpt.GPTRegistrationResult] = []
    monkeypatch.setattr(
        gpt,
        "_create_temp_email_from_config",
        lambda _conf: ("login@example.com", "mail-token"),
    )

    result = gpt.run_single_gpt_registration(
        {"api": {}},
        client_factory=lambda: next(clients),
        push_account=lambda _conf, item: pushed.append(item),
    )

    assert result.status == "available"
    assert result.access_token == "fresh-login-token"
    assert result.error == ""
    assert calls == ["register", "login"]
    assert pushed == [result]


def test_login_with_otp_retries_wrong_email_code_and_ignores_seen_codes(monkeypatch) -> None:
    wait_ignore_codes: list[set[str]] = []

    class FakeLoginClient(gpt.ChatGPTRegistrationClient):
        def __init__(self) -> None:
            self.sent = 0
            self.validated: list[str] = []
            self.callback_done = False

        def visit_homepage(self) -> None:
            return None

        def get_csrf(self) -> str:
            return "csrf"

        def signin(self, email: str, csrf: str) -> str:
            return "auth-url"

        def authorize(self, url: str) -> str:
            return "https://auth.openai.com/log-in/password"

        def session_access_token(self) -> str:
            return "fresh-session-token" if self.callback_done else ""

        def submit_login_password(self, email: str, password: str) -> None:
            return None

        def send_otp(self, *, referer: str | None = None) -> None:
            self.sent += 1

        def validate_otp(self, code: str) -> str:
            self.validated.append(code)
            if len(self.validated) == 1:
                raise gpt.GPTRegistrationError(
                    '{"error":{"code":"wrong_email_otp_code","message":"Wrong code"}}'
                )
            return "/callback"

        def perform_callback(self, url: str = "") -> None:
            self.callback_done = True

    codes = iter(["222222", "333333"])
    monkeypatch.setattr(
        gpt,
        "_mail_snapshot",
        lambda mail_token, email: ({"old-mail"}, {"111111"}),
    )

    def fake_wait_for_code(
        mail_token: str,
        email: str,
        *,
        timeout: int = 90,
        ignore_codes: set[str] | None = None,
        ignore_ids: set[str] | None = None,
    ) -> str:
        assert ignore_ids == {"old-mail"}
        wait_ignore_codes.append(set(ignore_codes or set()))
        return next(codes)

    monkeypatch.setattr(gpt, "_wait_for_code", fake_wait_for_code)

    client = FakeLoginClient()
    token = client.login_with_otp("user@example.com", "pw", "mail-token", ignore_otp="000000")

    assert token == "fresh-session-token"
    assert client.sent == 2
    assert client.validated == ["222222", "333333"]
    assert wait_ignore_codes[0] == {"000000", "111111"}
    assert wait_ignore_codes[1] == {"000000", "111111", "222222"}


def test_single_gpt_registration_saves_credentials_when_token_unavailable(monkeypatch) -> None:
    class FakeClient:
        def run_register(self, email: str, password: str, mail_token: str, **_kwargs):
            return True, "123456"

        def session_access_token(self) -> str:
            raise AssertionError("phone-gated accounts should not request session token")

    pushed: list[gpt.GPTRegistrationResult] = []
    monkeypatch.setattr(
        gpt,
        "_create_temp_email_from_config",
        lambda _conf: ("phone@example.com", "mail-token"),
    )

    result = gpt.run_single_gpt_registration(
        {"api": {}},
        client_factory=FakeClient,
        push_account=lambda _conf, item: pushed.append(item),
    )

    assert result.status == "login_required"
    assert result.access_token == ""
    assert result.phone_verification_required is True
    assert "手机验证" in result.error
    assert pushed == [result]
    payload = gpt._gpt_account_payload(result)
    assert "access_token" not in payload
    assert payload["registration_status"] == "login_required"
    assert payload["registration_error"]


def test_single_gpt_registration_does_not_save_registration_disallowed_login_fallback(monkeypatch) -> None:
    calls: list[str] = []

    class RegisterClient:
        def run_register(self, email: str, password: str, mail_token: str, **_kwargs):
            calls.append("register")
            return False, "111111"

        def session_access_token(self) -> str:
            return ""

    class LoginClient:
        def login_with_otp(
            self,
            email: str,
            password: str,
            mail_token: str,
            *,
            ignore_otp: str | None = None,
            **_kwargs,
        ) -> str:
            calls.append("login")
            raise gpt.GPTRegistrationError(
                '创建账号资料失败: HTTP 400 {"error":{"code":"registration_disallowed",'
                '"message":"Sorry, we cannot create your account with the given information."}}'
            )

    clients = iter([RegisterClient(), LoginClient()])
    pushed: list[gpt.GPTRegistrationResult] = []
    monkeypatch.setattr(
        gpt,
        "_create_temp_email_from_config",
        lambda _conf: ("blocked@example.com", "mail-token"),
    )

    with pytest.raises(gpt.GPTAccountNotCreatedError) as err:
        gpt.run_single_gpt_registration(
            {"api": {}, "gpt": {"registration_attempts_per_account": 1}},
            client_factory=lambda: next(clients),
            push_account=lambda _conf, item: pushed.append(item),
        )

    assert calls == ["register", "login"]
    assert pushed == []
    assert "registration_disallowed" in str(err.value)


def test_gpt_batch_reports_progress(tmp_path) -> None:
    config_path = tmp_path / "maintainer.config.json"
    config_path.write_text('{"email": {}, "api": {}}', encoding="utf-8")
    events: list[tuple[str, dict]] = []

    def fake_registration(_conf):
        return gpt.GPTRegistrationResult(
            email="batch@example.com",
            password="pw",
            mail_token="mail",
            status="login_required",
        )

    results = gpt.run_gpt_batch(
        config_path=config_path,
        count=2,
        progress_callback=lambda event, payload: events.append((event, payload)),
        registration_func=fake_registration,
    )

    assert len(results) == 2
    assert events[0][0] == "started"
    assert [event for event, _payload in events].count("round_done") == 2
    assert events[-1] == ("finished", {"token_count": 2})
