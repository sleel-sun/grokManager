from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.control.account.commands import AccountPatch, AccountUpsert
from app.control.account.enums import AccountStatus
from app.control.account.models import AccountRecord
from app.control.model.registry import resolve
from app.platform.errors import RateLimitError, UpstreamError
from app.products.openai import gpt_image
from app.products.web.admin.gpt_accounts import gpt_account_credential_record_token
from app.products.web.admin.gpt_image_accounts import (
    GPTImageAccountItem,
    account_credential_record_token,
    _ext_for_item,
    _export_record,
    _summary,
    account_record_token,
)


def test_gpt_image_models_are_registered_as_image_models() -> None:
    one = resolve("gpt-image-1")
    two = resolve("gpt-image-2")

    assert one.is_image()
    assert two.is_image()
    assert one.upstream_profile == "chatgpt_image"
    assert one.upstream_model_name() == "gpt-image-2"
    assert two.upstream_model_name() == "gpt-image-2"


def test_gpt_image_account_record_token_is_stable_and_non_secret() -> None:
    first = account_record_token("token-a")
    second = account_record_token("token-a")

    assert first == second
    assert first.startswith("gpt_")
    assert "token-a" not in first


def test_gpt_image_account_ext_uses_unified_gpt_account_shape() -> None:
    item = GPTImageAccountItem(
        access_token="Bearer abc123",
        email="user@example.test",
        alias="image user",
        is_free=True,
    )

    ext = _ext_for_item(item)

    assert item.access_token == "abc123"
    assert ext["gpt"] is True
    assert ext["gpt_access_token"] == "abc123"
    assert ext["gpt_plan_type"] == "free"
    assert ext["gpt_image_is_free"] is True
    assert ext["gpt_status"] == "unchecked"


def test_gpt_image_account_credentials_map_to_unified_gpt_record() -> None:
    item = GPTImageAccountItem(
        email=" Image@Example.test ",
        password="chat-pass",
        mail_token="mail-token",
        email_provider="DuckMail",
    )

    ext = _ext_for_item(item)
    record_token = account_credential_record_token(item.email or "")

    assert record_token.startswith("gptcred_")
    assert "Image@Example" not in record_token
    assert ext["gpt_access_token"] is None
    assert ext["gpt_email"] == "Image@Example.test"
    assert ext["gpt_password"] == "chat-pass"
    assert ext["gpt_mail_token"] == "mail-token"
    assert ext["gpt_email_provider"] == "DuckMail"
    assert ext["gpt_status"] == "login_required"


def test_gpt_image_account_summary_and_secret_export() -> None:
    record = AccountRecord(
        token="gpt_123",
        tags=["gpt"],
        ext={
            "gpt": True,
            "gpt_access_token": "image-access-secret",
            "gpt_email": "image@example.test",
            "gpt_password": "password-secret",
            "gpt_mail_token": "mail-secret",
            "gpt_plan_type": "free",
            "gpt_image_is_free": True,
            "gpt_status": "available",
        },
    )

    summary = _summary([record])
    safe_export = _export_record(record, include_secrets=False)
    secret_export = _export_record(record, include_secrets=True)

    assert summary["total"] == 1
    assert summary["available"] == 1
    assert summary["types"]["free"] == 1
    assert summary["with_access_token"] == 1
    assert summary["with_credentials"] == 1
    assert "access_token" not in safe_export
    assert secret_export["access_token"] == "image-access-secret"
    assert secret_export["password"] == "password-secret"
    assert secret_export["mail_token"] == "mail-secret"


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


def test_gpt_image_prompt_forces_generation_not_search() -> None:
    prompt = gpt_image._image_generation_prompt("马斯克直播图")

    assert "Create exactly one original image" in prompt
    assert "Do not search the web" in prompt
    assert "image_group" in prompt
    assert "马斯克直播图" in prompt


def test_gpt_image_no_image_error_sanitizes_image_group_text() -> None:
    message = gpt_image._no_image_error('马斯克直播图image_group{"query":["Elon Musk"]}')

    assert message == "ChatGPT returned image search results instead of a generated image"


def test_gpt_image_no_image_error_sanitizes_processing_queue_text() -> None:
    message = gpt_image._no_image_error("正在处理图片\n\n目前有很多人在创建图片，因此可能需要一点时间。")

    assert message == "ChatGPT image generation is still queued upstream; retry later"


def test_gpt_image_upstream_model_uses_gpt5_for_paid_image2() -> None:
    assert gpt_image._normalize_image_model("gpt-image-1") == "gpt-image-2"
    assert gpt_image._normalize_image_model("gpt-image-2") == "gpt-image-2"
    assert gpt_image._upstream_model("gpt-image-2", is_free=True) == "gpt-5-3"
    assert gpt_image._upstream_model("gpt-image-2", is_free=False) == "gpt-5-3"


def test_gpt_image_generate_routes_compat_model_to_image2(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_run_generation(prompt: str, model: str, n: int):
        captured.update({"prompt": prompt, "model": model, "n": n})
        return [
            gpt_image._GeneratedImage(
                b64_json=base64.b64encode(b"image").decode("ascii"),
            )
        ]

    monkeypatch.setattr(gpt_image, "_run_generation", fake_run_generation)

    result = asyncio.run(
        gpt_image.generate(
            model="gpt-image-1",
            prompt="draw a cube",
            response_format="b64_json",
        )
    )

    assert captured == {"prompt": "draw a cube", "model": "gpt-image-2", "n": 1}
    assert result["data"][0]["b64_json"] == base64.b64encode(b"image").decode("ascii")


def test_gpt_image_account_import_uses_disabled_status_patch_shape() -> None:
    access_token = "access-token"
    upsert = AccountUpsert(
        token=account_record_token(access_token),
        pool="basic",
        tags=["gpt"],
        ext=_ext_for_item(GPTImageAccountItem(access_token=access_token)),
    )
    patch = AccountPatch(
        token=upsert.token,
        status=AccountStatus.DISABLED,
        state_reason="GPT account record; excluded from Grok SSO pool",
    )

    assert upsert.ext["gpt_access_token"] == access_token
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


def test_gpt_image_accounts_prefer_unified_fields_on_migrated_record() -> None:
    record = AccountRecord(
        token="gptimg_123",
        tags=["gpt-image", "gpt"],
        ext={
            "gpt_image": True,
            "gpt_image_access_token": "legacy-access-token",
            "gpt_image_status": "invalid",
            "gpt": True,
            "gpt_access_token": "unified-access-token",
            "gpt_status": "available",
            "gpt_plan_type": "plus",
        },
    )

    account = asyncio.run(gpt_image._account_from_record(record))

    assert account is not None
    assert account.access_token == "unified-access-token"
    assert account.status_key == "gpt_status"
    assert account.error_key == "gpt_registration_error"
    assert account.is_free is False


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


def test_gpt_image_accounts_skip_timeout_status() -> None:
    record = AccountRecord(
        token="gptimg_123",
        tags=["gpt-image"],
        ext={
            "gpt_image": True,
            "gpt_image_access_token": "timeout-access-token",
            "gpt_image_status": "timeout",
        },
    )

    account = asyncio.run(gpt_image._account_from_record(record))

    assert account is None


def test_gpt_image_accounts_skip_recent_generation_timeout_even_if_status_available() -> None:
    record = AccountRecord(
        token="gpt_123",
        tags=["gpt"],
        last_fail_at=gpt_image._now_ms(),
        last_fail_reason="ChatGPT image generation timed out after 180s: timeout",
        ext={
            "gpt": True,
            "gpt_access_token": "ordinary-access-token",
            "gpt_status": "available",
        },
    )

    account = asyncio.run(gpt_image._account_from_record(record))

    assert account is None


def test_gpt_image_accounts_skip_recent_image_search_failure() -> None:
    record = AccountRecord(
        token="gptimg_123",
        tags=["gpt-image"],
        last_fail_at=gpt_image._now_ms(),
        last_fail_reason="ChatGPT returned image search results instead of a generated image",
        ext={
            "gpt_image": True,
            "gpt_image_access_token": "search-routed-token",
            "gpt_image_status": "failed",
        },
    )

    account = asyncio.run(gpt_image._account_from_record(record))

    assert account is None


def test_gpt_image_accounts_dedupe_tokens_and_prioritize_unified_gpt_records(monkeypatch) -> None:
    class FakeRepo:
        async def list_accounts(self, query):
            return SimpleNamespace(
                total=3,
                items=[
                    AccountRecord(
                        token="gpt_1",
                        tags=["gpt"],
                        ext={
                            "gpt": True,
                            "gpt_access_token": "shared-token",
                            "gpt_status": "available",
                        },
                    ),
                    AccountRecord(
                        token="gptimg_1",
                        tags=["gpt-image"],
                        ext={
                            "gpt_image": True,
                            "gpt_image_access_token": "image-token",
                            "gpt_image_status": "available",
                        },
                    ),
                    AccountRecord(
                        token="gptimg_2",
                        tags=["gpt-image"],
                        ext={
                            "gpt_image": True,
                            "gpt_image_access_token": "shared-token",
                            "gpt_image_status": "available",
                        },
                    ),
                ],
            )

    monkeypatch.setattr(gpt_image, "get_account_repository", lambda: FakeRepo())

    accounts = asyncio.run(gpt_image._gpt_image_accounts())

    assert [item.record_token for item in accounts] == ["gpt_1", "gptimg_1"]


def test_gpt_image_accounts_block_duplicate_token_after_image_timeout(monkeypatch) -> None:
    shared_token = "shared-timeout-token"

    class FakeRepo:
        async def list_accounts(self, query):
            return SimpleNamespace(
                total=2,
                items=[
                    AccountRecord(
                        token="gptimg_1",
                        tags=["gpt-image"],
                        last_fail_at=gpt_image._now_ms(),
                        last_fail_reason="ChatGPT image generation timed out after 180s: timeout",
                        ext={
                            "gpt_image": True,
                            "gpt_image_access_token": shared_token,
                            "gpt_image_status": "timeout",
                        },
                    ),
                    AccountRecord(
                        token="gpt_1",
                        tags=["gpt"],
                        ext={
                            "gpt": True,
                            "gpt_access_token": shared_token,
                            "gpt_status": "available",
                        },
                    ),
                ],
            )

    monkeypatch.setattr(gpt_image, "get_account_repository", lambda: FakeRepo())

    accounts = asyncio.run(gpt_image._gpt_image_accounts())

    assert accounts == []


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


def test_gpt_image_run_generation_defaults_to_single_account_attempt(monkeypatch) -> None:
    attempts: list[str] = []
    accounts = [
        gpt_image.GPTImageAccount(record_token="gptimg_1", access_token="token-1"),
        gpt_image.GPTImageAccount(record_token="gptimg_2", access_token="token-2"),
    ]

    async def fake_accounts():
        return accounts

    async def fail_generate(account, prompt, model, *, timeout_s=None):
        attempts.append(account.record_token)
        raise UpstreamError("ChatGPT image generation timed out after 180s", status=504)

    async def mark_failure(account, exc):
        return None

    monkeypatch.setattr(gpt_image, "_gpt_image_accounts", fake_accounts)
    monkeypatch.setattr(gpt_image, "_generate_one", fail_generate)
    monkeypatch.setattr(gpt_image, "_mark_account_failure", mark_failure)

    with pytest.raises(UpstreamError):
        asyncio.run(gpt_image._run_generation("draw", "gpt-image-2", 1))

    assert attempts == ["gptimg_1"]


def test_gpt_image_run_generation_uses_single_request_timeout_budget(monkeypatch) -> None:
    accounts = [
        gpt_image.GPTImageAccount(record_token="gptimg_1", access_token="token-1"),
        gpt_image.GPTImageAccount(record_token="gptimg_2", access_token="token-2"),
    ]
    attempts: list[tuple[str, float | None]] = []

    async def fake_accounts():
        return accounts

    async def slow_fail_generate(account, prompt, model, *, timeout_s=None):
        attempts.append((account.record_token, timeout_s))
        await asyncio.sleep(0.25)
        raise UpstreamError("You've hit the Free plan limit for image generations requests.", status=429)

    async def mark_failure(account, exc):
        return None

    monkeypatch.setattr(gpt_image, "_gpt_image_accounts", fake_accounts)
    monkeypatch.setattr(gpt_image, "_generation_timeout_s", lambda: 1.1)
    monkeypatch.setattr(gpt_image, "_max_account_attempts_per_image", lambda count: 2)
    monkeypatch.setattr(gpt_image, "_generate_one", slow_fail_generate)
    monkeypatch.setattr(gpt_image, "_mark_account_failure", mark_failure)

    with pytest.raises(UpstreamError) as excinfo:
        asyncio.run(gpt_image._run_generation("draw", "gpt-image-2", 1))

    assert excinfo.value.status == 504
    assert [item[0] for item in attempts] == ["gptimg_1"]
    assert attempts[0][1] is not None
    assert attempts[0][1] <= 1.1


def test_gpt_image_run_generation_prefers_quota_failure_detail(monkeypatch) -> None:
    accounts = [
        gpt_image.GPTImageAccount(record_token="gpt_1", access_token="token-1"),
        gpt_image.GPTImageAccount(record_token="gpt_2", access_token="token-2"),
    ]
    attempts: list[str] = []

    async def fake_accounts():
        return accounts

    async def fake_generate(account, prompt, model, *, timeout_s=None):
        attempts.append(account.record_token)
        if account.record_token == "gpt_1":
            raise UpstreamError("You've hit the Free plan limit for image generations requests.", status=429)
        raise UpstreamError('ChatGPT image generation failed: upstream returned 401: {"detail":"Unauthorized"}', status=401)

    async def mark_failure(account, exc):
        return None

    monkeypatch.setattr(gpt_image, "_gpt_image_accounts", fake_accounts)
    monkeypatch.setattr(gpt_image, "_max_account_attempts_per_image", lambda count: 2)
    monkeypatch.setattr(gpt_image, "_generate_one", fake_generate)
    monkeypatch.setattr(gpt_image, "_mark_account_failure", mark_failure)

    with pytest.raises(RateLimitError) as excinfo:
        asyncio.run(gpt_image._run_generation("draw", "gpt-image-2", 1))

    assert attempts == ["gpt_1", "gpt_2"]
    assert "Free plan limit" in str(excinfo.value)


def test_account_admin_page_uses_unified_gptchat_account_panel() -> None:
    html = (
        Path(__file__).resolve().parent.parent
        / "app"
        / "statics"
        / "admin"
        / "account.html"
    ).read_text(encoding="utf-8")

    assert "GPTChat 账号池" in html
    assert 'id="gpt-account-tbody"' in html
    assert 'id="modal-gpt-account-add"' in html
    assert "_api('GET', '/gpt/accounts')" in html
    assert "_api('POST', '/gpt/accounts'" in html
    assert "_api('POST', '/gpt/accounts/test'" in html
    assert "_api('DELETE', '/gpt/accounts'" in html
    assert 'id="modal-gpt-image"' not in html
    assert 'id="gpt-image-tbody"' not in html
    assert "/gpt-image/accounts" not in html


def test_maintainer_page_uses_unified_gptchat_registration_panel() -> None:
    html = (
        Path(__file__).resolve().parent.parent
        / "app"
        / "statics"
        / "admin"
        / "maintainer.html"
    ).read_text(encoding="utf-8")

    assert "GPTChat 账号批量注册" in html
    assert 'id="gpt-account-form"' in html
    assert 'id="gpt-account-oauth-tokens"' in html
    assert 'id="gpt-account-bulk-credentials"' in html
    assert 'id="gpt-account-test-btn"' in html
    assert "parseGPTAccountBulkCredentials(" in html
    assert "api('POST', '/gpt/accounts'" in html
    assert "runGPTAccountTest('/gpt/accounts/test'" in html
    assert 'id="gpt-image-form"' not in html
    assert "readGPTImagePayload()" not in html
    assert "/gpt-image/accounts" not in html
