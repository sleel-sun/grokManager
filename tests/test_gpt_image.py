from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.control.account.commands import AccountPatch, AccountUpsert
from app.control.account.enums import AccountStatus
from app.control.account.models import AccountRecord
from app.control.model.registry import resolve
from app.platform.errors import UpstreamError
from app.products.openai import gpt_image
from app.products.web.admin.gpt_accounts import gpt_account_credential_record_token
from app.products.web.admin.gpt_image_accounts import (
    GPTImageAccountItem,
    account_credential_record_token,
    _ext_for_item,
    account_record_token,
)


def test_gpt_image_models_are_registered_as_image_models() -> None:
    one = resolve("gpt-image-1")
    two = resolve("gpt-image-2")

    assert one.is_image()
    assert two.is_image()
    assert one.upstream_profile == "chatgpt_image"
    assert two.upstream_model_name() == "gpt-image-2"


def test_gpt_image_account_record_token_is_stable_and_non_secret() -> None:
    first = account_record_token("token-a")
    second = account_record_token("token-a")

    assert first == second
    assert first.startswith("gptimg_")
    assert "token-a" not in first


def test_gpt_image_account_ext_marks_unchecked_image_account() -> None:
    item = GPTImageAccountItem(
        access_token="Bearer abc123",
        email="user@example.test",
        alias="image user",
        is_free=True,
    )

    ext = _ext_for_item(item)

    assert item.access_token == "abc123"
    assert ext["gpt_image"] is True
    assert ext["gpt_image_access_token"] == "abc123"
    assert ext["gpt_image_is_free"] is True
    assert ext["gpt_image_status"] == "unchecked"


def test_gpt_image_account_credentials_can_register_without_access_token() -> None:
    item = GPTImageAccountItem(
        email=" Image@Example.test ",
        password="chat-pass",
        mail_token="mail-token",
        email_provider="DuckMail",
    )

    ext = _ext_for_item(item)
    record_token = account_credential_record_token(item.email or "")

    assert record_token.startswith("gptimgcred_")
    assert "Image@Example" not in record_token
    assert ext["gpt_image_access_token"] is None
    assert ext["gpt_image_email"] == "Image@Example.test"
    assert ext["gpt_image_password"] == "chat-pass"
    assert ext["gpt_image_mail_token"] == "mail-token"
    assert ext["gpt_image_email_provider"] == "DuckMail"
    assert ext["gpt_image_status"] == "login_required"


def test_gpt_image_parse_sse_extracts_conversation_and_file_ids() -> None:
    raw = "\n".join(
        [
            'data: {"conversation_id":"conv_1"}',
            'data: {"message":{"content":{"content_type":"text","parts":["working"]}}}',
            "data: file-service://file_123",
            "data: sediment://sed_456",
            "data: [DONE]",
        ]
    )

    conversation_id, file_ids, text = gpt_image._parse_sse(raw)

    assert conversation_id == "conv_1"
    assert file_ids == ["file_123", "sed:sed_456"]
    assert text == "working"


def test_gpt_image_upstream_model_uses_gpt5_for_paid_image2() -> None:
    assert gpt_image._upstream_model("gpt-image-1", is_free=False) == "auto"
    assert gpt_image._upstream_model("gpt-image-2", is_free=True) == "auto"
    assert gpt_image._upstream_model("gpt-image-2", is_free=False) == "gpt-5-3"


def test_gpt_image_account_import_uses_disabled_status_patch_shape() -> None:
    access_token = "access-token"
    upsert = AccountUpsert(
        token=account_record_token(access_token),
        pool="basic",
        tags=["gpt-image"],
        ext=_ext_for_item(GPTImageAccountItem(access_token=access_token)),
    )
    patch = AccountPatch(
        token=upsert.token,
        status=AccountStatus.DISABLED,
        state_reason="GPT image-only account; excluded from Grok SSO pool",
    )

    assert upsert.ext["gpt_image_access_token"] == access_token
    assert patch.status == AccountStatus.DISABLED


def test_gpt_image_accounts_accepts_ordinary_gpt_access_token() -> None:
    record = AccountRecord(
        token="gpt_123",
        tags=["gpt"],
        ext={
            "gpt": True,
            "gpt_access_token": "ordinary-access-token",
            "gpt_plan_type": "free",
            "gpt_status": "available",
        },
    )

    account = asyncio.run(gpt_image._account_from_record(record))

    assert account is not None
    assert account.record_token == "gpt_123"
    assert account.access_token == "ordinary-access-token"
    assert account.is_free is True


def test_gpt_image_accounts_accepts_unchecked_access_token() -> None:
    record = AccountRecord(
        token="gptimg_123",
        tags=["gpt-image"],
        ext={
            "gpt_image": True,
            "gpt_image_access_token": "unchecked-access-token",
            "gpt_image_status": "unchecked",
        },
    )

    account = asyncio.run(gpt_image._account_from_record(record))

    assert account is not None
    assert account.access_token == "unchecked-access-token"
    assert account.status_key == "gpt_image_status"
    assert account.error_key == "gpt_image_error"


def test_gpt_image_accounts_skip_invalid_access_token() -> None:
    record = AccountRecord(
        token="gptimg_123",
        tags=["gpt-image"],
        ext={
            "gpt_image": True,
            "gpt_image_access_token": "invalid-access-token",
            "gpt_image_status": "invalid",
        },
    )

    account = asyncio.run(gpt_image._account_from_record(record))

    assert account is None


def test_gpt_image_accounts_login_ordinary_gpt_credentials(monkeypatch) -> None:
    class FakeRepo:
        def __init__(self) -> None:
            self.patches = []

        async def patch_accounts(self, patches):
            self.patches.extend(patches)

    repo = FakeRepo()
    record = AccountRecord(
        token=gpt_account_credential_record_token("user@example.test"),
        tags=["gpt"],
        ext={
            "gpt": True,
            "gpt_email": "user@example.test",
            "gpt_password": "chat-pass",
            "gpt_mail_token": "mail-token",
            "gpt_plan_type": "free",
            "gpt_status": "login_required",
        },
    )

    monkeypatch.setattr(gpt_image, "get_account_repository", lambda: repo)

    async def fake_login(**kwargs):
        return "fresh-session-token"

    monkeypatch.setattr(gpt_image, "_login_gpt_credentials_async", fake_login)

    account = asyncio.run(gpt_image._account_from_record(record))

    assert account is not None
    assert account.access_token == "fresh-session-token"
    assert repo.patches
    patch = repo.patches[0]
    assert patch.ext_merge["gpt_access_token"] == "fresh-session-token"
    assert patch.ext_merge["gpt_status"] == "available"


def test_gpt_image_mark_success_sets_available(monkeypatch) -> None:
    class FakeRepo:
        def __init__(self) -> None:
            self.patches = []

        async def patch_accounts(self, patches):
            self.patches.extend(patches)

    repo = FakeRepo()
    monkeypatch.setattr(gpt_image, "get_account_repository", lambda: repo)
    account = gpt_image.GPTImageAccount(
        record_token="gptimg_123",
        access_token="token",
        status_key="gpt_image_status",
        error_key="gpt_image_error",
    )

    asyncio.run(gpt_image._mark_account_success(account))

    patch = repo.patches[0]
    assert patch.last_use_at is not None
    assert patch.ext_merge["gpt_image_status"] == "available"
    assert patch.ext_merge["gpt_image_error"] is None
    assert "gpt_image_last_checked_at" in patch.ext_merge


def test_gpt_image_mark_failure_marks_invalid_token(monkeypatch) -> None:
    class FakeRepo:
        def __init__(self) -> None:
            self.patches = []

        async def patch_accounts(self, patches):
            self.patches.extend(patches)

    repo = FakeRepo()
    monkeypatch.setattr(gpt_image, "get_account_repository", lambda: repo)
    account = gpt_image.GPTImageAccount(
        record_token="gptimg_123",
        access_token="token",
        status_key="gpt_image_status",
        error_key="gpt_image_error",
    )
    exc = UpstreamError("ChatGPT chat-requirements failed", status=401, body="unauthorized")

    asyncio.run(gpt_image._mark_account_failure(account, exc))

    patch = repo.patches[0]
    assert patch.last_fail_at is not None
    assert patch.ext_merge["gpt_image_status"] == "invalid"
    assert "unauthorized" in patch.ext_merge["gpt_image_error"]


def test_gpt_image_test_account_success_marks_available(monkeypatch) -> None:
    class FakeRepo:
        def __init__(self) -> None:
            self.patches = []

        async def patch_accounts(self, patches):
            self.patches.extend(patches)

    async def validate(access_token: str) -> None:
        assert access_token == "valid-token"

    repo = FakeRepo()
    monkeypatch.setattr(gpt_image, "_validate_access_token", validate)
    record = AccountRecord(
        token="gptimg_123",
        tags=["gpt-image"],
        ext={
            "gpt_image": True,
            "gpt_image_access_token": "valid-token",
            "gpt_image_status": "unchecked",
        },
    )

    result = asyncio.run(gpt_image.test_gpt_account_record(record, repo=repo))

    assert result["ok"] is True
    assert result["capability_status"] == "available"
    patch = repo.patches[0]
    assert patch.ext_merge["gpt_image_status"] == "available"
    assert patch.ext_merge["gpt_image_error"] is None
    assert "gpt_image_last_checked_at" in patch.ext_merge


def test_gpt_image_test_account_failure_marks_invalid(monkeypatch) -> None:
    class FakeRepo:
        def __init__(self) -> None:
            self.patches = []

        async def patch_accounts(self, patches):
            self.patches.extend(patches)

    async def validate(access_token: str) -> None:
        raise UpstreamError("ChatGPT chat-requirements failed", status=401, body="unauthorized")

    repo = FakeRepo()
    monkeypatch.setattr(gpt_image, "_validate_access_token", validate)
    record = AccountRecord(
        token="gpt_123",
        tags=["gpt"],
        ext={
            "gpt": True,
            "gpt_access_token": "invalid-token",
            "gpt_status": "unchecked",
        },
    )

    result = asyncio.run(gpt_image.test_gpt_account_record(record, repo=repo))

    assert result["ok"] is False
    assert result["kind"] == "gpt"
    assert result["capability_status"] == "invalid"
    patch = repo.patches[0]
    assert patch.ext_merge["gpt_status"] == "invalid"
    assert "unauthorized" in patch.ext_merge["gpt_registration_error"]


def test_gpt_image_generate_one_has_hard_timeout(monkeypatch) -> None:
    async def slow_generate(*args, **kwargs):
        await asyncio.sleep(1)
        raise AssertionError("timeout should cancel first")

    monkeypatch.setattr(gpt_image, "_generate_one_inner", slow_generate)
    monkeypatch.setattr(gpt_image, "_generation_timeout_s", lambda: 0.01)
    account = gpt_image.GPTImageAccount(
        record_token="gptimg_123",
        access_token="token",
    )

    with pytest.raises(UpstreamError) as excinfo:
        asyncio.run(gpt_image._generate_one(account, "draw", "gpt-image-2"))

    assert excinfo.value.status == 504
    assert "timed out" in str(excinfo.value)


def test_account_admin_page_exposes_gpt_image_account_panel() -> None:
    html = (
        Path(__file__).resolve().parent.parent
        / "app"
        / "statics"
        / "admin"
        / "account.html"
    ).read_text(encoding="utf-8")

    assert 'id="modal-gpt-image"' in html
    assert 'id="gpt-image-tbody"' in html
    assert "_api('GET', '/gpt-image/accounts')" in html
    assert "_api('POST', '/gpt-image/accounts'" in html
    assert "_api('POST', '/gpt-image/accounts/test'" in html
    assert "_api('DELETE', '/gpt-image/accounts'" in html


def test_maintainer_page_exposes_gpt_image_registration_panel() -> None:
    html = (
        Path(__file__).resolve().parent.parent
        / "app"
        / "statics"
        / "admin"
        / "maintainer.html"
    ).read_text(encoding="utf-8")

    assert 'id="gpt-image-form"' in html
    assert 'id="gpt-image-email"' in html
    assert 'id="gpt-image-access-tokens"' in html
    assert 'id="gpt-image-bulk-credentials"' in html
    assert 'id="gpt-image-test-btn"' in html
    assert "readGPTImagePayload()" in html
    assert "parseGPTImageBulkCredentials(" in html
    assert "api('POST', '/gpt-image/accounts'" in html
    assert "runGPTAccountTest('/gpt-image/accounts/test'" in html
