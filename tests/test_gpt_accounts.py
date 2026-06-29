from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import orjson

from app.control.account.models import AccountRecord
from app.maintainer.gpt_oauth import GPTAccountOAuthService
from app.products.web.admin.gpt_accounts import (
    GPTAccountItem,
    GPTAccountLoginRequest,
    GPTAccountOAuthFinishRequest,
    GPTAccountOAuthStartRequest,
    _access_token_from_login_result,
    _delete_record_tokens,
    _ext_for_item,
    _export_record,
    _remote_detail_ext,
    _serialize,
    _legacy_image_credential_record_token,
    _legacy_image_record_token,
    _summary,
    finish_gpt_account_oauth,
    gpt_account_credential_record_token,
    gpt_account_record_token,
    login_gpt_account,
    start_gpt_account_oauth,
)


def test_gpt_account_ext_marks_oauth_token_account_unchecked() -> None:
    item = GPTAccountItem(
        access_token="Bearer access-token",
        plan_type="Plus",
        email="user@example.test",
        alias="GPT User",
    )

    ext = _ext_for_item(item)

    assert item.access_token == "access-token"
    assert gpt_account_record_token(item.access_token).startswith("gpt_")
    assert ext["gpt"] is True
    assert ext["gpt_access_token"] == "access-token"
    assert ext["gpt_plan_type"] == "Plus"
    assert ext["gpt_status"] == "unchecked"


def test_gpt_account_credentials_can_register_without_access_token() -> None:
    item = GPTAccountItem(
        email=" User@Example.test ",
        password="chat-pass",
        mail_token="mail-token",
        email_provider="DuckMail",
    )

    ext = _ext_for_item(item)
    record_token = gpt_account_credential_record_token(item.email or "")

    assert record_token.startswith("gptcred_")
    assert "User@Example" not in record_token
    assert ext["gpt_access_token"] is None
    assert ext["gpt_email"] == "User@Example.test"
    assert ext["gpt_password"] == "chat-pass"
    assert ext["gpt_mail_token"] == "mail-token"
    assert ext["gpt_email_provider"] == "DuckMail"
    assert ext["gpt_status"] == "login_required"


def test_gpt_account_ext_preserves_auto_registration_status() -> None:
    item = GPTAccountItem(
        email="auto@example.test",
        password="chat-pass",
        mail_token="mail-token",
        registration_status="phone_verification_required",
        registration_error="needs phone",
    )

    ext = _ext_for_item(item)

    assert ext["gpt_status"] == "phone_verification_required"
    assert ext["gpt_registration_error"] == "needs phone"


def test_gpt_account_summary_and_secret_export() -> None:
    record = AccountRecord(
        token="gpt_123",
        tags=["gpt"],
        ext={
            "gpt": True,
            "gpt_access_token": "access-secret",
            "gpt_email": "user@example.test",
            "gpt_password": "password-secret",
            "gpt_mail_token": "mail-secret",
            "gpt_plan_type": "plus",
            "gpt_status": "available",
        },
    )

    summary = _summary([record])
    safe_export = _export_record(record, include_secrets=False)
    secret_export = _export_record(record, include_secrets=True)

    assert summary["total"] == 1
    assert summary["available"] == 1
    assert summary["with_access_token"] == 1
    assert summary["with_credentials"] == 1
    assert summary["plans"]["plus"] == 1
    assert "access_token" not in safe_export
    assert secret_export["access_token"] == "access-secret"
    assert secret_export["password"] == "password-secret"
    assert secret_export["mail_token"] == "mail-secret"


def test_gpt_delete_record_tokens_accept_ids_email_and_bearer_token() -> None:
    access_token = "access-token"
    email = "User@Example.test"

    tokens = _delete_record_tokens([
        "gpt_existing",
        "gptcred_existing",
        email,
        f"Bearer {access_token}",
        f"Bearer {access_token}",
    ])

    assert tokens == [
        "gpt_existing",
        "gptcred_existing",
        gpt_account_credential_record_token(email),
        _legacy_image_credential_record_token(email),
        gpt_account_record_token(access_token),
        _legacy_image_record_token(access_token),
    ]


def test_maintainer_page_exposes_ordinary_gpt_bulk_registration_panel() -> None:
    html = (
        Path(__file__).resolve().parent.parent
        / "app"
        / "statics"
        / "admin"
        / "maintainer.html"
    ).read_text(encoding="utf-8")

    assert 'id="gpt-account-form"' in html
    assert 'id="gpt-account-bulk-credentials"' in html
    assert 'id="gpt-account-oauth-tokens"' in html
    assert 'id="gpt-account-test-btn"' in html
    assert 'id="gpt-account-auto-run-btn"' in html
    assert "parseGPTAccountBulkCredentials(" in html
    assert "api('POST', '/gpt/accounts'" in html
    assert "runGPTAccountTest('/gpt/accounts/test'" in html
    assert "api('POST', '/maintainer/gpt/run'" in html


def test_account_page_exposes_ordinary_gpt_management_panel() -> None:
    html = (
        Path(__file__).resolve().parent.parent
        / "app"
        / "statics"
        / "admin"
        / "account.html"
    ).read_text(encoding="utf-8")

    assert 'id="gpt-account-tbody"' in html
    assert 'id="gpt-account-overview"' in html
    assert 'id="gpt-overview-total"' in html
    assert 'id="gpt-overview-available"' in html
    assert 'id="gpt-overview-pending"' in html
    assert 'id="gpt-overview-invalid"' in html
    assert 'id="gpt-overview-token"' in html
    assert 'id="gpt-overview-credentials"' in html
    assert "function renderGptAccountOverview()" in html
    assert "function gptAccountOverviewStats()" in html
    assert 'id="modal-gpt-account-detail"' in html
    assert "openGptAccountDetail(" in html
    assert "refreshGptAccountDetail(" in html
    assert "_api('POST', '/gpt/accounts/detail'" in html
    assert "远端详情" in html
    assert "GPTChat 账号详情" in html
    assert 'id="modal-gpt-login"' in html
    assert 'id="gpt-login-token-result"' in html
    assert 'id="gpt-oauth-authorize-url"' in html
    assert 'id="gpt-oauth-callback"' in html
    assert 'id="gpt-session-json-open"' in html
    assert "openChatGPTSessionJson()" in html
    assert "ChatGPT /api/auth/session JSON / Bearer token" in html
    assert "scheduleGptOAuthAutoPoll(" not in html
    assert "https://chatgpt.com/api/auth/session" in html
    assert "_api('GET', '/gpt/accounts'" in html
    assert "_api('POST', '/gpt/accounts/oauth/start'" in html
    assert "_api('POST', '/gpt/accounts/oauth/finish'" in html
    assert "_api('POST', '/gpt/accounts/login'" in html
    assert "_api('DELETE', '/gpt/accounts'" in html
    assert "deleteGptAccount(" in html


def test_gpt_account_serialize_includes_safe_detail_fields_without_secrets() -> None:
    record = AccountRecord(
        token="gpt_123",
        tags=["gpt"],
        created_at=1710000000000,
        updated_at=1710000100000,
        last_use_at=1710000200000,
        last_fail_at=1710000300000,
        last_fail_reason="upstream failed",
        usage_use_count=7,
        usage_fail_count=2,
        ext={
            "gpt": True,
            "gpt_access_token": "access-secret-value",
            "gpt_email": "user@example.test",
            "gpt_password": "password-secret",
            "gpt_mail_token": "mail-secret",
            "gpt_plan_type": "plus",
            "gpt_status": "available",
            "gpt_last_checked_at": 1710000400000,
            "gpt_login_attempt_at": 1710000500000,
            "gpt_cooldown_until": 1710000600000,
            "gpt_last_remote_refresh_at": 1710000700000,
            "gpt_remote_user_id": "user-123",
            "gpt_default_model_slug": "gpt-5",
            "gpt_limits_progress": [{"feature_name": "image_gen", "remaining": 3}],
            "gpt_image_quota": 3,
            "gpt_image_quota_unknown": False,
            "gpt_image_restore_at": "2026-06-29T00:00:00Z",
            "gpt_remote_error": None,
        },
    )

    payload = _serialize(record)

    assert payload["created_at"] == 1710000000000
    assert payload["last_used_at"] == 1710000200000
    assert payload["last_fail_at"] == 1710000300000
    assert payload["last_checked_at"] == 1710000400000
    assert payload["last_login_attempt_at"] == 1710000500000
    assert payload["cooldown_until"] == 1710000600000
    assert payload["last_remote_refresh_at"] == 1710000700000
    assert payload["remote_user_id"] == "user-123"
    assert payload["default_model_slug"] == "gpt-5"
    assert payload["limits_progress"] == [{"feature_name": "image_gen", "remaining": 3}]
    assert payload["image_quota"] == 3
    assert payload["image_quota_unknown"] is False
    assert payload["image_restore_at"] == "2026-06-29T00:00:00Z"
    assert payload["use_count"] == 7
    assert payload["fail_count"] == 2
    assert payload["has_access_token"] is True
    assert payload["has_credentials"] is True
    assert payload["has_password"] is True
    assert payload["has_mail_token"] is True
    assert payload["access_token_masked"] != "access-secret-value"
    assert "access_token" not in payload
    assert "password" not in payload
    assert "mail_token" not in payload


def test_gpt_remote_detail_ext_maps_profile_plan_and_quota() -> None:
    detail = {
        "email": "remote@example.test",
        "user_id": "user-remote",
        "plan_type": "Plus",
        "default_model_slug": "gpt-5",
        "limits_progress": [
            {
                "feature_name": "image_gen",
                "remaining": 12,
                "reset_after": "2026-06-29T00:00:00Z",
            }
        ],
        "image_quota": 12,
        "image_restore_at": "2026-06-29T00:00:00Z",
        "image_quota_unknown": False,
        "account": {"plan_type": "Plus"},
    }

    ext = _remote_detail_ext(detail, {"gpt_image": True})

    assert ext["gpt"] is True
    assert ext["gpt_status"] == "available"
    assert ext["gpt_email"] == "remote@example.test"
    assert ext["gpt_plan_type"] == "Plus"
    assert ext["gpt_remote_user_id"] == "user-remote"
    assert ext["gpt_default_model_slug"] == "gpt-5"
    assert ext["gpt_limits_progress"] == detail["limits_progress"]
    assert ext["gpt_image_quota"] == 12
    assert ext["gpt_image_quota_unknown"] is False
    assert ext["gpt_image_restore_at"] == "2026-06-29T00:00:00Z"
    assert ext["gpt_image_is_free"] is False
    assert ext["gpt_remote_error"] is None


def test_maintainer_page_no_longer_exposes_ordinary_gpt_delete_controls() -> None:
    html = (
        Path(__file__).resolve().parent.parent
        / "app"
        / "statics"
        / "admin"
        / "maintainer.html"
    ).read_text(encoding="utf-8")

    assert 'id="gpt-account-delete-list"' not in html
    assert 'id="gpt-account-delete-btn"' not in html
    assert "readGPTAccountDeletePayload(" not in html


def test_gpt_account_login_returns_token_and_updates_existing_record(monkeypatch) -> None:
    class Repo:
        def __init__(self) -> None:
            self.patches = []

        async def get_accounts(self, tokens):
            assert tokens == [
                gpt_account_credential_record_token("user@example.test"),
                _legacy_image_credential_record_token("user@example.test"),
            ]
            return [
                AccountRecord(
                    token=tokens[0],
                    tags=["gpt"],
                    ext={
                        "gpt": True,
                        "gpt_email": "user@example.test",
                        "gpt_password": "secret",
                        "gpt_mail_token": "mail-token",
                    },
                )
            ]

        async def patch_accounts(self, patches):
            self.patches.extend(patches)

    calls = []

    def fake_login_gpt_credentials(**kwargs):
        calls.append(kwargs)
        return "returned-access-token"

    monkeypatch.setattr("app.maintainer.gpt.login_gpt_credentials", fake_login_gpt_credentials)

    repo = Repo()
    response = asyncio.run(
        login_gpt_account(
            GPTAccountLoginRequest(account="user@example.test"),
            repo=repo,
        )
    )
    body = orjson.loads(response.body)

    assert body["access_token"] == "returned-access-token"
    assert body["account"]["id"] == gpt_account_credential_record_token("user@example.test")
    assert calls[0]["email"] == "user@example.test"
    assert calls[0]["password"] == "secret"
    assert calls[0]["mail_token"] == "mail-token"
    assert repo.patches[0].ext_merge["gpt_access_token"] == "returned-access-token"
    assert repo.patches[0].ext_merge["gpt_status"] == "available"


def test_gpt_account_login_accepts_legacy_image_credentials(monkeypatch) -> None:
    class Repo:
        def __init__(self) -> None:
            self.patches = []

        async def get_accounts(self, tokens):
            return [
                AccountRecord(
                    token=tokens[-1],
                    tags=["gpt-image"],
                    ext={
                        "gpt_image": True,
                        "gpt_image_email": "legacy@example.test",
                        "gpt_image_password": "secret",
                        "gpt_image_mail_token": "mail-token",
                    },
                )
            ]

        async def patch_accounts(self, patches):
            self.patches.extend(patches)

    calls = []

    def fake_login_gpt_credentials(**kwargs):
        calls.append(kwargs)
        return "returned-access-token"

    monkeypatch.setattr("app.maintainer.gpt.login_gpt_credentials", fake_login_gpt_credentials)

    repo = Repo()
    response = asyncio.run(
        login_gpt_account(
            GPTAccountLoginRequest(account="legacy@example.test"),
            repo=repo,
        )
    )
    body = orjson.loads(response.body)

    assert body["access_token"] == "returned-access-token"
    assert calls[0]["email"] == "legacy@example.test"
    assert repo.patches[0].ext_merge["gpt"] is True
    assert repo.patches[0].ext_merge["gpt_access_token"] == "returned-access-token"
    assert repo.patches[0].ext_merge["gpt_status"] == "available"


def test_gpt_account_login_extracts_token_from_session_json(monkeypatch) -> None:
    def fake_login_gpt_credentials(**_kwargs):
        return '{"accessToken":"json-access-token","user":{"email":"user@example.test"}}'

    monkeypatch.setattr("app.maintainer.gpt.login_gpt_credentials", fake_login_gpt_credentials)

    response = asyncio.run(
        login_gpt_account(
            GPTAccountLoginRequest(
                email="user@example.test",
                password="secret",
                mail_token="mail-token",
                save=False,
            ),
            repo=SimpleNamespace(),
        )
    )
    body = orjson.loads(response.body)

    assert body["access_token"] == "json-access-token"
    assert body["account"]["email"] == "user@example.test"
    assert body["account"]["saved"] is False


def test_gpt_oauth_service_start_builds_authorize_url() -> None:
    service = GPTAccountOAuthService()

    data = service.start("User@example.test")
    parsed = urlparse(data["authorize_url"])
    params = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "auth.openai.com"
    assert parsed.path == "/api/accounts/authorize"
    assert data["session_id"]
    assert data["redirect_uri_prefix"] == "https://platform.openai.com/auth/callback"
    assert params["client_id"] == ["app_2SKx67EdpoN0G6j64rFvigXD"]
    assert params["audience"] == ["https://api.openai.com/v1"]
    assert params["login_hint"] == ["User@example.test"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["scope"] == ["openid profile email offline_access"]


def test_gpt_oauth_service_proxy_env_prefers_maintainer_proxy(monkeypatch) -> None:
    monkeypatch.setenv("MAINTAINER_PROXY", "http://maintainer-proxy:8118")
    monkeypatch.setenv("GROK_PROXY_EGRESS_PROXY_URL", "http://egress-proxy:8118")

    assert GPTAccountOAuthService.proxy_url_from_env() == "http://maintainer-proxy:8118"


def test_gpt_oauth_service_proxy_env_falls_back_to_egress_proxy(monkeypatch) -> None:
    monkeypatch.delenv("MAINTAINER_PROXY", raising=False)
    monkeypatch.setenv("GROK_PROXY_EGRESS_PROXY_URL", "http://egress-proxy:8118")

    assert GPTAccountOAuthService.proxy_url_from_env() == "http://egress-proxy:8118"


def test_gpt_account_login_start_uses_reference_oauth_url() -> None:
    response = asyncio.run(
        start_gpt_account_oauth(
            GPTAccountOAuthStartRequest(email_hint=" image@example.test "),
            repo=SimpleNamespace(),
        )
    )
    body = orjson.loads(response.body)
    parsed = urlparse(body["authorize_url"])
    params = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "auth.openai.com"
    assert parsed.path == "/api/accounts/authorize"
    assert body["session_id"]
    assert body["expires_in"] == 600
    assert body["redirect_uri_prefix"] == "https://platform.openai.com/auth/callback"
    assert body["email_hint"] == "image@example.test"
    assert params["client_id"] == ["app_2SKx67EdpoN0G6j64rFvigXD"]
    assert params["audience"] == ["https://api.openai.com/v1"]
    assert params["redirect_uri"] == ["https://platform.openai.com/auth/callback"]
    assert params["login_hint"] == ["image@example.test"]
    assert params["state"][0].startswith(f"{body['session_id']}.")
    assert "logout_authorize_url" not in body
    assert "direct_authorize_url" not in body
    assert "login_mode" not in body


def test_gpt_account_oauth_finish_exchanges_raw_callback_code(monkeypatch) -> None:
    class Repo:
        def __init__(self) -> None:
            self.upserts = []
            self.patches = []

        async def upsert_accounts(self, upserts):
            self.upserts.extend(upserts)
            return SimpleNamespace(upserted=len(upserts))

        async def patch_accounts(self, patches):
            self.patches.extend(patches)

    calls = []

    def fake_finish(session_id, callback):
        calls.append((session_id, callback))
        return {"access_token": "code-access-token"}

    monkeypatch.setattr("app.maintainer.gpt_oauth.gpt_oauth_login_service.finish", fake_finish)

    repo = Repo()
    response = asyncio.run(
        finish_gpt_account_oauth(
            GPTAccountOAuthFinishRequest(
                session_id="session-1",
                callback="raw-callback-code",
                email="image@example.test",
            ),
            repo=repo,
        )
    )
    body = orjson.loads(response.body)

    assert calls == [("session-1", "raw-callback-code")]
    assert body["access_token"] == "code-access-token"
    assert body["account"]["email"] == "image@example.test"
    assert repo.upserts[0].token == gpt_account_record_token("code-access-token")
    assert repo.upserts[0].ext["gpt_access_token"] == "code-access-token"
    assert repo.upserts[0].ext["gpt_email"] == "image@example.test"


def test_gpt_account_oauth_finish_accepts_codexmanager_session_snapshot() -> None:
    snapshot = {
        "href": "https://chatgpt.com/api/auth/session",
        "text": '{"accessToken":"snapshot-token","user":{"email":"image@example.test"}}',
    }

    access_token, email = _access_token_from_login_result(orjson.dumps(snapshot).decode())

    assert access_token == "snapshot-token"
    assert email == "image@example.test"


def test_gpt_account_oauth_finish_saves_session_snapshot_without_oauth_exchange(monkeypatch) -> None:
    class Repo:
        def __init__(self) -> None:
            self.upserts = []
            self.patches = []

        async def upsert_accounts(self, upserts):
            self.upserts.extend(upserts)
            return SimpleNamespace(upserted=len(upserts))

        async def patch_accounts(self, patches):
            self.patches.extend(patches)

    def fail_finish(_session_id, _callback):
        raise AssertionError("session JSON should not be exchanged as an OAuth code")

    monkeypatch.setattr("app.maintainer.gpt_oauth.gpt_oauth_login_service.finish", fail_finish)

    snapshot = {
        "href": "https://chatgpt.com/api/auth/session",
        "text": '{"accessToken":"snapshot-token","user":{"email":"image@example.test"}}',
    }
    repo = Repo()
    response = asyncio.run(
        finish_gpt_account_oauth(
            GPTAccountOAuthFinishRequest(
                session_id="",
                callback=orjson.dumps(snapshot).decode(),
            ),
            repo=repo,
        )
    )
    body = orjson.loads(response.body)

    assert body["access_token"] == "snapshot-token"
    assert body["account"]["email"] == "image@example.test"
    assert repo.upserts[0].token == gpt_account_record_token("snapshot-token")
    assert repo.upserts[0].ext["gpt_access_token"] == "snapshot-token"
    assert repo.upserts[0].ext["gpt_email"] == "image@example.test"


def test_gpt_account_oauth_finish_extracts_token_from_pasted_fragment() -> None:
    access_token, email = _access_token_from_login_result(
        'copy from page: "accessToken":"fragment-token", "expires":"soon"'
    )

    assert access_token == "fragment-token"
    assert email == ""


def test_gpt_account_oauth_finish_rejects_non_chatgpt_session_snapshot() -> None:
    snapshot = {
        "href": "https://example.com/api/auth/session",
        "text": '{"accessToken":"snapshot-token","user":{"email":"image@example.test"}}',
    }

    access_token, email = _access_token_from_login_result(orjson.dumps(snapshot).decode())

    assert access_token == ""
    assert email == ""


def test_gpt_account_oauth_finish_saves_tokens(monkeypatch) -> None:
    class Repo:
        def __init__(self) -> None:
            self.upserts = []
            self.patches = []

        async def upsert_accounts(self, upserts):
            self.upserts.extend(upserts)
            return SimpleNamespace(upserted=len(upserts))

        async def patch_accounts(self, patches):
            self.patches.extend(patches)

    calls = []

    def fake_finish(session_id, callback):
        calls.append((session_id, callback))
        return {
            "access_token": "oauth-access-token",
            "refresh_token": "oauth-refresh-token",
            "id_token": "oauth-id-token",
            "client_id": "oauth-client-id",
        }

    monkeypatch.setattr("app.maintainer.gpt_oauth.gpt_oauth_login_service.finish", fake_finish)

    repo = Repo()
    response = asyncio.run(
        finish_gpt_account_oauth(
            GPTAccountOAuthFinishRequest(
                session_id="session-1",
                callback="https://platform.openai.com/auth/callback?code=abc&state=session-1.x",
                email="user@example.test",
            ),
            repo=repo,
        )
    )
    body = orjson.loads(response.body)

    assert calls == [
        (
            "session-1",
            "https://platform.openai.com/auth/callback?code=abc&state=session-1.x",
        )
    ]
    assert body["access_token"] == "oauth-access-token"
    assert body["account"]["id"] == gpt_account_record_token("oauth-access-token")
    assert repo.upserts[0].token == gpt_account_record_token("oauth-access-token")
    assert repo.upserts[0].ext["gpt_access_token"] == "oauth-access-token"
    assert "gpt_refresh_token" not in repo.upserts[0].ext
    assert "gpt_id_token" not in repo.upserts[0].ext
    assert "gpt_client_id" not in repo.upserts[0].ext
    assert "gpt_source_type" not in repo.upserts[0].ext
    assert repo.upserts[0].ext["gpt_email"] == "user@example.test"
    assert repo.upserts[0].ext["gpt_status"] == "available"
    assert repo.patches[0].token == gpt_account_record_token("oauth-access-token")
