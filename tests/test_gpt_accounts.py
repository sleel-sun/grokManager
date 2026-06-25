from __future__ import annotations

from pathlib import Path

from app.products.web.admin.gpt_accounts import (
    GPTAccountItem,
    _delete_record_tokens,
    _ext_for_item,
    gpt_account_credential_record_token,
    gpt_account_record_token,
)


def test_gpt_account_ext_marks_oauth_token_account_unchecked() -> None:
    item = GPTAccountItem(
        access_token="Bearer access-token",
        id_token="id-token",
        refresh_token="refresh-token",
        account_id="acct_123",
        organization_id="org_123",
        plan_type="Plus",
        email="user@example.test",
        alias="GPT User",
    )

    ext = _ext_for_item(item)

    assert item.access_token == "access-token"
    assert gpt_account_record_token(item.access_token).startswith("gpt_")
    assert ext["gpt"] is True
    assert ext["gpt_access_token"] == "access-token"
    assert ext["gpt_id_token"] == "id-token"
    assert ext["gpt_refresh_token"] == "refresh-token"
    assert ext["gpt_account_id"] == "acct_123"
    assert ext["gpt_organization_id"] == "org_123"
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
        gpt_account_record_token(access_token),
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
    assert 'id="gpt-account-delete-list"' in html
    assert 'id="gpt-account-delete-btn"' in html
    assert 'id="gpt-account-auto-run-btn"' in html
    assert "parseGPTAccountBulkCredentials(" in html
    assert "api('POST', '/gpt/accounts'" in html
    assert "api('DELETE', '/gpt/accounts'" in html
    assert "runGPTAccountTest('/gpt/accounts/test'" in html
    assert "api('POST', '/maintainer/gpt/run'" in html
