"""Admin API for ordinary GPT/Codex account records."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

import orjson
from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, RootModel, field_validator, model_validator

from app.control.account.commands import AccountPatch, AccountUpsert, ListAccountsQuery
from app.control.account.enums import AccountStatus
from app.platform.errors import ValidationError
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


@router.get("/accounts")
async def list_gpt_accounts(repo: "AccountRepository" = Depends(get_repo)):
    records = await _list_all_gpt_records(repo)
    return _json({"accounts": [_serialize(record) for record in records]})


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


__all__ = [
    "router",
    "GPTAccountItem",
    "GPTAccountsTestRequest",
    "_ext_for_item",
    "_delete_record_tokens",
    "gpt_account_record_token",
    "gpt_account_credential_record_token",
]
