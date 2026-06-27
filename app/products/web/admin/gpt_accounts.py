"""Admin API for ordinary GPT/Codex account records."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

import orjson
from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, RootModel, field_validator, model_validator
from starlette.concurrency import run_in_threadpool

from app.control.account.commands import AccountPatch, AccountUpsert, ListAccountsQuery
from app.control.account.enums import AccountStatus
from app.platform.errors import AppError, ErrorKind, ValidationError
from app.platform.runtime.batch import run_batch

from . import get_repo

if TYPE_CHECKING:
    from app.control.account.models import AccountRecord
    from app.control.account.repository import AccountRepository


router = APIRouter(prefix="/gpt", tags=["Admin - GPT Accounts"])
_TAG = "gpt"
_MAX_TEST_CONCURRENCY = 10


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def gpt_account_record_token(access_token: str) -> str:
    digest = hashlib.sha256(access_token.encode("utf-8")).hexdigest()
    return f"gpt_{digest[:40]}"


def gpt_account_credential_record_token(email: str) -> str:
    digest = hashlib.sha256(_clean_text(email).lower().encode("utf-8")).hexdigest()
    return f"gptcred_{digest[:40]}"


def _mask(value: str) -> str:
    value = str(value or "")
    return f"{value[:8]}...{value[-8:]}" if len(value) > 20 else value


class GPTAccountItem(BaseModel):
    access_token: str | None = Field(default=None)
    id_token: str | None = None
    refresh_token: str | None = None
    account_id: str | None = None
    organization_id: str | None = None
    plan_type: str | None = None
    email: str | None = None
    alias: str | None = None
    password: str | None = None
    mail_token: str | None = None
    email_provider: str | None = None
    registration_status: str | None = None
    registration_error: str | None = None

    @field_validator("access_token", mode="before")
    @classmethod
    def _normalize_access_token(cls, value: Any) -> str | None:
        token = _clean_text(value)
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        return token or None

    @model_validator(mode="after")
    def _require_token_or_login_credentials(self) -> "GPTAccountItem":
        if self.access_token:
            return self
        if (
            _clean_text(self.email)
            and _clean_text(self.password)
            and _clean_text(self.mail_token)
        ):
            return self
        raise ValueError(
            "access_token is required unless email, password, and mail_token are provided"
        )


class GPTAccountsRequest(BaseModel):
    accounts: list[str | GPTAccountItem]


class GPTDeleteRequest(RootModel[list[str]]):
    """Delete by access token or stored gpt_* / gptcred_* record token."""


class GPTAccountsTestRequest(BaseModel):
    accounts: list[str] = Field(default_factory=list)


class GPTAccountLoginRequest(BaseModel):
    account: str | None = None
    email: str | None = None
    password: str | None = None
    mail_token: str | None = None
    email_provider: str | None = None
    alias: str | None = None
    plan_type: str | None = None
    save: bool = True
    timeout: int = Field(default=90, ge=1, le=600)


class GPTAccountOAuthStartRequest(BaseModel):
    account: str | None = None
    email_hint: str | None = None


class GPTAccountOAuthFinishRequest(BaseModel):
    session_id: str = ""
    callback: str = ""
    account: str | None = None
    email: str | None = None
    alias: str | None = None
    plan_type: str | None = None
    save: bool = True


def _item_from_raw(raw: str | GPTAccountItem) -> GPTAccountItem:
    if isinstance(raw, GPTAccountItem):
        return raw
    return GPTAccountItem(access_token=raw)


def _record_token_for_item(item: GPTAccountItem) -> str:
    if item.access_token:
        return gpt_account_record_token(item.access_token)
    return gpt_account_credential_record_token(_clean_text(item.email))


def _dedupe_key_for_item(item: GPTAccountItem) -> str:
    if item.access_token:
        return f"token:{item.access_token}"
    return f"email:{_clean_text(item.email).lower()}"


def _ext_for_item(item: GPTAccountItem) -> dict[str, Any]:
    access_token = _clean_text(item.access_token)
    return {
        "gpt": True,
        "gpt_access_token": access_token or None,
        "gpt_id_token": _clean_text(item.id_token) or None,
        "gpt_refresh_token": _clean_text(item.refresh_token) or None,
        "gpt_account_id": _clean_text(item.account_id) or None,
        "gpt_organization_id": _clean_text(item.organization_id) or None,
        "gpt_plan_type": _clean_text(item.plan_type) or None,
        "gpt_email": _clean_text(item.email) or None,
        "gpt_alias": _clean_text(item.alias) or None,
        "gpt_password": _clean_text(item.password) or None,
        "gpt_mail_token": _clean_text(item.mail_token) or None,
        "gpt_email_provider": _clean_text(item.email_provider) or None,
        "gpt_status": _clean_text(item.registration_status) or ("unchecked" if access_token else "login_required"),
        "gpt_registration_error": _clean_text(item.registration_error) or None,
        "gpt_last_checked_at": None,
    }


def _is_gpt_record(record: "AccountRecord") -> bool:
    return (
        _TAG in (record.tags or [])
        or bool((record.ext or {}).get("gpt"))
        or bool((record.ext or {}).get("gpt_access_token"))
    )


def _serialize(record: "AccountRecord") -> dict[str, Any]:
    ext = record.ext or {}
    access_token = _clean_text(ext.get("gpt_access_token"))
    refresh_token = _clean_text(ext.get("gpt_refresh_token"))
    return {
        "id": record.token,
        "status": record.status,
        "email": ext.get("gpt_email"),
        "alias": ext.get("gpt_alias"),
        "plan_type": ext.get("gpt_plan_type"),
        "organization_id": ext.get("gpt_organization_id"),
        "account_id": ext.get("gpt_account_id"),
        "email_provider": ext.get("gpt_email_provider"),
        "capability_status": ext.get("gpt_status") or "unknown",
        "capability_error": ext.get("gpt_registration_error"),
        "last_checked_at": ext.get("gpt_last_checked_at"),
        "access_token_masked": _mask(access_token),
        "has_access_token": bool(access_token),
        "has_refresh_token": bool(refresh_token),
        "has_credentials": bool(ext.get("gpt_password") and ext.get("gpt_mail_token")),
        "registration_error": ext.get("gpt_registration_error"),
        "updated_at": record.updated_at,
        "last_fail_reason": record.last_fail_reason,
    }


def _summary(records: list["AccountRecord"]) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    plan_counts: dict[str, int] = {}
    with_access_token = 0
    with_refresh_token = 0
    with_credentials = 0
    for record in records:
        ext = record.ext or {}
        status = _clean_text(ext.get("gpt_status")) or "unknown"
        plan = (_clean_text(ext.get("gpt_plan_type")) or "unknown").lower()
        status_counts[status] = status_counts.get(status, 0) + 1
        plan_counts[plan] = plan_counts.get(plan, 0) + 1
        if _clean_text(ext.get("gpt_access_token")):
            with_access_token += 1
        if _clean_text(ext.get("gpt_refresh_token")):
            with_refresh_token += 1
        if ext.get("gpt_password") and ext.get("gpt_mail_token"):
            with_credentials += 1

    return {
        "total": len(records),
        "available": status_counts.get("available", 0),
        "unchecked": status_counts.get("unchecked", 0),
        "login_required": status_counts.get("login_required", 0),
        "invalid": status_counts.get("invalid", 0),
        "with_access_token": with_access_token,
        "with_refresh_token": with_refresh_token,
        "with_credentials": with_credentials,
        "status": status_counts,
        "plans": plan_counts,
    }


def _export_record(record: "AccountRecord", *, include_secrets: bool) -> dict[str, Any]:
    ext = record.ext or {}
    payload = _serialize(record)
    payload["registration_status"] = ext.get("gpt_status") or "unknown"
    if include_secrets:
        payload.update(
            {
                "access_token": _clean_text(ext.get("gpt_access_token")) or None,
                "refresh_token": _clean_text(ext.get("gpt_refresh_token")) or None,
                "id_token": _clean_text(ext.get("gpt_id_token")) or None,
                "password": _clean_text(ext.get("gpt_password")) or None,
                "mail_token": _clean_text(ext.get("gpt_mail_token")) or None,
            }
        )
    return payload


def _json(data: Any, status_code: int = 200) -> Response:
    return Response(
        content=orjson.dumps(data),
        media_type="application/json",
        status_code=status_code,
    )


async def _list_all_gpt_records(repo: "AccountRepository") -> list["AccountRecord"]:
    records: list["AccountRecord"] = []
    page_num = 1
    while True:
        page = await repo.list_accounts(
            ListAccountsQuery(
                page=page_num,
                page_size=2000,
                include_deleted=False,
                sort_by="updated_at",
                sort_desc=True,
            )
        )
        records.extend(record for record in page.items if _is_gpt_record(record))
        if page_num * 2000 >= page.total:
            break
        page_num += 1
    return records


def _access_token_value(value: str) -> str:
    token = _clean_text(value)
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def _test_record_tokens(values: list[str]) -> list[str]:
    tokens: list[str] = []
    for raw in values:
        value = _clean_text(raw)
        if not value:
            continue
        if value.startswith(("gpt_", "gptcred_")):
            tokens.append(value)
        elif "@" in value and not value.lower().startswith("bearer "):
            tokens.append(gpt_account_credential_record_token(value))
        else:
            tokens.append(gpt_account_record_token(_access_token_value(value)))
    return list(dict.fromkeys(tokens))


def _delete_record_tokens(values: list[str]) -> list[str]:
    return _test_record_tokens(values)


async def _test_records(
    repo: "AccountRepository",
    values: list[str],
) -> list["AccountRecord"]:
    if not values:
        return await _list_all_gpt_records(repo)
    records = await repo.get_accounts(_test_record_tokens(values))
    return [record for record in records if _is_gpt_record(record)]


def _test_concurrency(value: int | None) -> int:
    try:
        resolved = int(value or 3)
    except (TypeError, ValueError):
        resolved = 3
    return min(max(1, resolved), _MAX_TEST_CONCURRENCY)


async def _lookup_gpt_record(
    repo: "AccountRepository",
    account_ref: str,
    *,
    param: str = "account",
) -> "AccountRecord":
    records = await repo.get_accounts(_test_record_tokens([account_ref]))
    record = next(
        (item for item in records if _is_gpt_record(item) and not item.is_deleted()),
        None,
    )
    if not record:
        raise ValidationError("GPT account not found", param=param, code="account_not_found")
    return record


@router.get("/accounts")
async def list_gpt_accounts(repo: "AccountRepository" = Depends(get_repo)):
    records = await _list_all_gpt_records(repo)
    return _json(
        {
            "summary": _summary(records),
            "accounts": [_serialize(record) for record in records],
        }
    )


@router.get("/accounts/summary")
async def summarize_gpt_accounts(repo: "AccountRepository" = Depends(get_repo)):
    records = await _list_all_gpt_records(repo)
    return _json({"summary": _summary(records)})


@router.get("/accounts/export")
async def export_gpt_accounts(
    include_secrets: bool = Query(False),
    repo: "AccountRepository" = Depends(get_repo),
):
    records = await _list_all_gpt_records(repo)
    return _json(
        {
            "summary": _summary(records),
            "accounts": [
                _export_record(record, include_secrets=include_secrets)
                for record in records
            ],
        }
    )


@router.post("/accounts")
async def add_gpt_accounts(
    req: GPTAccountsRequest,
    repo: "AccountRepository" = Depends(get_repo),
):
    cleaned: list[GPTAccountItem] = []
    seen: set[str] = set()
    for raw in req.accounts:
        item = _item_from_raw(raw)
        key = _dedupe_key_for_item(item)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(item)

    if not cleaned:
        raise ValidationError("No valid GPT accounts provided", param="accounts")

    upserts = [
        AccountUpsert(
            token=_record_token_for_item(item),
            pool="basic",
            tags=[_TAG],
            ext=_ext_for_item(item),
        )
        for item in cleaned
    ]
    result = await repo.upsert_accounts(upserts)
    await repo.patch_accounts(
        [
            AccountPatch(
                token=upsert.token,
                status=AccountStatus.DISABLED,
                state_reason="GPT account record; excluded from Grok SSO pool",
            )
            for upsert in upserts
        ]
    )
    return _json({"status": "success", "count": result.upserted or len(upserts)})


@router.delete("/accounts")
async def delete_gpt_accounts(
    req: GPTDeleteRequest = Body(...),
    repo: "AccountRepository" = Depends(get_repo),
):
    tokens = _delete_record_tokens(req.root)
    if not tokens:
        raise ValidationError("No GPT accounts specified", param="accounts")
    result = await repo.delete_accounts(tokens)
    return _json({"status": "success", "deleted": result.deleted})


@router.post("/accounts/oauth/start")
async def start_gpt_account_oauth(
    req: GPTAccountOAuthStartRequest,
    repo: "AccountRepository" = Depends(get_repo),
):
    email_hint = _clean_text(req.email_hint)
    account_ref = _clean_text(req.account)
    if account_ref:
        record = await _lookup_gpt_record(repo, account_ref)
        email_hint = email_hint or _clean_text((record.ext or {}).get("gpt_email"))

    try:
        from app.maintainer.gpt_oauth import GPTAccountOAuthError, gpt_oauth_login_service

        payload = await run_in_threadpool(gpt_oauth_login_service.start, email_hint)
    except GPTAccountOAuthError as exc:
        raise ValidationError(str(exc), param="oauth", code="oauth_start_failed") from exc

    return _json({"status": "success", **payload, "email_hint": email_hint})


@router.post("/accounts/oauth/finish")
async def finish_gpt_account_oauth(
    req: GPTAccountOAuthFinishRequest,
    repo: "AccountRepository" = Depends(get_repo),
):
    account_ref = _clean_text(req.account)
    record: AccountRecord | None = None
    email = _clean_text(req.email)
    alias = _clean_text(req.alias)
    plan_type = _clean_text(req.plan_type)
    if account_ref:
        record = await _lookup_gpt_record(repo, account_ref)
        ext = record.ext or {}
        email = email or _clean_text(ext.get("gpt_email"))
        alias = alias or _clean_text(ext.get("gpt_alias"))
        plan_type = plan_type or _clean_text(ext.get("gpt_plan_type"))

    try:
        from app.maintainer.gpt_oauth import GPTAccountOAuthError, gpt_oauth_login_service

        tokens = await run_in_threadpool(
            gpt_oauth_login_service.finish,
            req.session_id,
            req.callback,
        )
    except GPTAccountOAuthError as exc:
        raise ValidationError(str(exc), param="callback", code="oauth_finish_failed") from exc

    access_token = _clean_text(tokens.get("access_token"))
    refresh_token = _clean_text(tokens.get("refresh_token"))
    id_token = _clean_text(tokens.get("id_token"))
    if not access_token:
        raise AppError(
            "GPT OAuth login did not return an access token",
            kind=ErrorKind.UPSTREAM,
            code="gpt_oauth_no_token",
            status=502,
        )

    saved_id = record.token if record else ""
    if req.save:
        if record:
            ext_merge: dict[str, Any] = {
                "gpt": True,
                "gpt_access_token": access_token,
                "gpt_email": email or None,
                "gpt_alias": alias or None,
                "gpt_plan_type": plan_type or None,
                "gpt_status": "available",
                "gpt_registration_error": None,
            }
            if refresh_token:
                ext_merge["gpt_refresh_token"] = refresh_token
            if id_token:
                ext_merge["gpt_id_token"] = id_token
            await repo.patch_accounts(
                [
                    AccountPatch(
                        token=record.token,
                        status=AccountStatus.DISABLED,
                        add_tags=[_TAG],
                        state_reason="GPT account record; excluded from Grok SSO pool",
                        ext_merge=ext_merge,
                    )
                ]
            )
        else:
            item = GPTAccountItem(
                access_token=access_token,
                refresh_token=refresh_token or None,
                id_token=id_token or None,
                email=email or None,
                alias=alias or None,
                plan_type=plan_type or None,
                registration_status="available",
            )
            saved_id = _record_token_for_item(item)
            await repo.upsert_accounts(
                [
                    AccountUpsert(
                        token=saved_id,
                        pool="basic",
                        tags=[_TAG],
                        ext=_ext_for_item(item),
                    )
                ]
            )
            await repo.patch_accounts(
                [
                    AccountPatch(
                        token=saved_id,
                        status=AccountStatus.DISABLED,
                        state_reason="GPT account record; excluded from Grok SSO pool",
                    )
                ]
            )

    return _json(
        {
            "status": "success",
            "access_token": access_token,
            "access_token_masked": _mask(access_token),
            "refresh_token": refresh_token,
            "refresh_token_masked": _mask(refresh_token),
            "id_token": id_token,
            "account": {
                "id": saved_id,
                "email": email or None,
                "alias": alias or None,
                "plan_type": plan_type or None,
                "saved": bool(req.save),
            },
        }
    )


@router.post("/accounts/login")
async def login_gpt_account(
    req: GPTAccountLoginRequest,
    repo: "AccountRepository" = Depends(get_repo),
):
    record: AccountRecord | None = None
    email = _clean_text(req.email)
    password = _clean_text(req.password)
    mail_token = _clean_text(req.mail_token)
    email_provider = _clean_text(req.email_provider)
    alias = _clean_text(req.alias)
    plan_type = _clean_text(req.plan_type)

    account_ref = _clean_text(req.account)
    if account_ref:
        records = await repo.get_accounts(_test_record_tokens([account_ref]))
        record = next((item for item in records if _is_gpt_record(item) and not item.is_deleted()), None)
        if not record:
            raise ValidationError("GPT account not found", param="account", code="account_not_found")
        ext = record.ext or {}
        email = email or _clean_text(ext.get("gpt_email"))
        password = password or _clean_text(ext.get("gpt_password"))
        mail_token = mail_token or _clean_text(ext.get("gpt_mail_token"))
        email_provider = email_provider or _clean_text(ext.get("gpt_email_provider"))
        alias = alias or _clean_text(ext.get("gpt_alias"))
        plan_type = plan_type or _clean_text(ext.get("gpt_plan_type"))

    if not email:
        raise ValidationError("Email is required", param="email")
    if not password:
        raise ValidationError("Password is required", param="password")
    if not mail_token:
        raise ValidationError("Mail token is required", param="mail_token")

    try:
        from app.maintainer import gpt as gpt_module

        access_token = await run_in_threadpool(
            gpt_module.login_gpt_credentials,
            email=email,
            password=password,
            mail_token=mail_token,
            timeout=req.timeout,
        )
    except AppError:
        raise
    except Exception as exc:
        raise AppError(
            f"GPT account login failed: {exc}",
            kind=ErrorKind.UPSTREAM,
            code="gpt_login_failed",
            status=502,
        ) from exc

    access_token = _clean_text(access_token)
    if not access_token:
        raise AppError(
            "GPT account login did not return an access token",
            kind=ErrorKind.UPSTREAM,
            code="gpt_login_no_token",
            status=502,
        )

    saved_id = record.token if record else ""
    if req.save:
        if record:
            await repo.patch_accounts(
                [
                    AccountPatch(
                        token=record.token,
                        status=AccountStatus.DISABLED,
                        add_tags=[_TAG],
                        state_reason="GPT account record; excluded from Grok SSO pool",
                        ext_merge={
                            "gpt": True,
                            "gpt_access_token": access_token,
                            "gpt_email": email,
                            "gpt_alias": alias or None,
                            "gpt_password": password,
                            "gpt_mail_token": mail_token,
                            "gpt_email_provider": email_provider or None,
                            "gpt_plan_type": plan_type or None,
                            "gpt_status": "available",
                            "gpt_registration_error": None,
                        },
                    )
                ]
            )
        else:
            item = GPTAccountItem(
                access_token=access_token,
                email=email,
                password=password,
                mail_token=mail_token,
                email_provider=email_provider or None,
                alias=alias or None,
                plan_type=plan_type or None,
                registration_status="available",
            )
            saved_id = _record_token_for_item(item)
            await repo.upsert_accounts(
                [
                    AccountUpsert(
                        token=saved_id,
                        pool="basic",
                        tags=[_TAG],
                        ext=_ext_for_item(item),
                    )
                ]
            )
            await repo.patch_accounts(
                [
                    AccountPatch(
                        token=saved_id,
                        status=AccountStatus.DISABLED,
                        state_reason="GPT account record; excluded from Grok SSO pool",
                    )
                ]
            )

    return _json(
        {
            "status": "success",
            "access_token": access_token,
            "access_token_masked": _mask(access_token),
            "account": {
                "id": saved_id,
                "email": email,
                "alias": alias or None,
                "plan_type": plan_type or None,
                "email_provider": email_provider or None,
                "saved": bool(req.save),
            },
        }
    )


@router.post("/accounts/test")
async def test_gpt_accounts(
    req: GPTAccountsTestRequest | None = Body(default=None),
    concurrency: int | None = Query(None, ge=1),
    repo: "AccountRepository" = Depends(get_repo),
):
    records = await _test_records(repo, req.accounts if req else [])
    if not records:
        raise ValidationError("No GPT accounts found", param="accounts")

    from app.products.openai.gpt_image import test_gpt_account_record

    async def _one(record: "AccountRecord") -> dict[str, Any]:
        return await test_gpt_account_record(record, repo=repo)

    results = await run_batch(records, _one, concurrency=_test_concurrency(concurrency))
    ok = sum(1 for item in results if item.get("ok"))
    return _json(
        {
            "status": "success",
            "summary": {
                "total": len(results),
                "ok": ok,
                "fail": len(results) - ok,
            },
            "accounts": results,
        }
    )


@router.post("/accounts/refresh")
async def refresh_gpt_accounts(
    req: GPTAccountsTestRequest | None = Body(default=None),
    concurrency: int | None = Query(None, ge=1),
    repo: "AccountRepository" = Depends(get_repo),
):
    return await test_gpt_accounts(req=req, concurrency=concurrency, repo=repo)


__all__ = [
    "router",
    "GPTAccountItem",
    "GPTAccountsTestRequest",
    "GPTAccountLoginRequest",
    "GPTAccountOAuthStartRequest",
    "GPTAccountOAuthFinishRequest",
    "_ext_for_item",
    "_summary",
    "_export_record",
    "_delete_record_tokens",
    "gpt_account_record_token",
    "gpt_account_credential_record_token",
]
