"""Compatibility API for ChatGPT image account routes backed by GPTChat records."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import orjson
from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, RootModel, field_validator, model_validator

from app.platform.errors import ValidationError

from . import get_repo
from .gpt_accounts import (
    GPTAccountItem,
    GPTAccountsRequest,
    GPTAccountsTestRequest,
    GPTDeleteRequest,
    _delete_record_tokens as _gpt_delete_record_tokens,
    _export_record as _gpt_export_record,
    _list_all_gpt_records,
    _serialize as _gpt_serialize,
    _summary as _gpt_summary,
    add_gpt_accounts as _add_gpt_accounts,
    delete_gpt_accounts as _delete_gpt_accounts,
    gpt_account_credential_record_token,
    gpt_account_record_token,
    test_gpt_accounts as _test_gpt_accounts,
)

if TYPE_CHECKING:
    from app.control.account.models import AccountRecord
    from app.control.account.repository import AccountRepository


router = APIRouter(prefix="/gpt-image", tags=["Admin - GPT Image Accounts"])


def account_record_token(access_token: str) -> str:
    """Compatibility alias: image accounts are now stored in the GPTChat pool."""
    return gpt_account_record_token(access_token)


def account_credential_record_token(email: str) -> str:
    """Compatibility alias: image credentials are now stored in the GPTChat pool."""
    return gpt_account_credential_record_token(email)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


class GPTImageAccountItem(BaseModel):
    access_token: str | None = Field(default=None)
    email: str | None = None
    alias: str | None = None
    password: str | None = None
    mail_token: str | None = None
    email_provider: str | None = None
    is_free: bool = False

    @field_validator("access_token", mode="before")
    @classmethod
    def _normalize_access_token(cls, value: Any) -> str | None:
        token = _clean_text(value)
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        return token or None

    @model_validator(mode="after")
    def _require_token_or_login_credentials(self) -> "GPTImageAccountItem":
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


class GPTImageAccountsRequest(BaseModel):
    accounts: list[str | GPTImageAccountItem]
    is_free: bool = False


class GPTImageDeleteRequest(RootModel[list[str]]):
    """Delete by access token or stored GPT/GPT-image record token."""


class GPTImageTestRequest(BaseModel):
    accounts: list[str] = Field(default_factory=list)


def _item_from_raw(raw: str | GPTImageAccountItem, *, default_is_free: bool) -> GPTImageAccountItem:
    if isinstance(raw, GPTImageAccountItem):
        return raw
    return GPTImageAccountItem(access_token=raw, is_free=default_is_free)


def _dedupe_key_for_item(item: GPTImageAccountItem) -> str:
    if item.access_token:
        return f"token:{item.access_token}"
    return f"email:{_clean_text(item.email).lower()}"


def _ext_for_item(item: GPTImageAccountItem) -> dict[str, Any]:
    gpt_item = _to_gpt_item(item)
    ext = {
        "gpt": True,
        "gpt_access_token": _clean_text(gpt_item.access_token) or None,
        "gpt_plan_type": _clean_text(gpt_item.plan_type) or None,
        "gpt_email": _clean_text(gpt_item.email) or None,
        "gpt_alias": _clean_text(gpt_item.alias) or None,
        "gpt_password": _clean_text(gpt_item.password) or None,
        "gpt_mail_token": _clean_text(gpt_item.mail_token) or None,
        "gpt_email_provider": _clean_text(gpt_item.email_provider) or None,
        "gpt_status": "unchecked" if _clean_text(gpt_item.access_token) else "login_required",
        "gpt_registration_error": None,
        "gpt_last_checked_at": None,
    }
    # Preserve the legacy free/paid intent for the image route while keeping one
    # canonical GPTChat account record shape.
    ext["gpt_image_is_free"] = bool(item.is_free)
    return ext


def _to_gpt_item(item: GPTImageAccountItem) -> GPTAccountItem:
    return GPTAccountItem(
        access_token=item.access_token,
        email=item.email,
        alias=item.alias,
        password=item.password,
        mail_token=item.mail_token,
        email_provider=item.email_provider,
        plan_type="free" if item.is_free else "plus",
    )


def _serialize(record: "AccountRecord") -> dict[str, Any]:
    payload = _gpt_serialize(record)
    ext = record.ext or {}
    plan = _clean_text(payload.get("plan_type")).lower()
    payload["is_free"] = bool(ext.get("gpt_image_is_free")) or plan in {"", "free", "basic"}
    return payload


def _summary(records: list["AccountRecord"]) -> dict[str, Any]:
    payload = _gpt_summary(records)
    type_counts = {"free": 0, "paid": 0}
    for record in records:
        ext = record.ext or {}
        plan = _clean_text(ext.get("gpt_plan_type")).lower()
        if ext.get("gpt_image_is_free") or plan in {"", "free", "basic"}:
            type_counts["free"] += 1
        else:
            type_counts["paid"] += 1
    payload["types"] = type_counts
    return payload


def _export_record(record: "AccountRecord", *, include_secrets: bool) -> dict[str, Any]:
    payload = _gpt_export_record(record, include_secrets=include_secrets)
    payload["is_free"] = _serialize(record).get("is_free")
    return payload


def _json(data: Any, status_code: int = 200) -> Response:
    return Response(
        content=orjson.dumps(data),
        media_type="application/json",
        status_code=status_code,
    )


async def _list_all_gpt_image_records(repo: "AccountRepository") -> list["AccountRecord"]:
    return await _list_all_gpt_records(repo)


@router.get("/accounts")
async def list_gpt_image_accounts(repo: "AccountRepository" = Depends(get_repo)):
    records = await _list_all_gpt_image_records(repo)
    return _json(
        {
            "summary": _summary(records),
            "accounts": [_serialize(record) for record in records],
        }
    )


@router.get("/accounts/summary")
async def summarize_gpt_image_accounts(repo: "AccountRepository" = Depends(get_repo)):
    records = await _list_all_gpt_image_records(repo)
    return _json({"summary": _summary(records)})


@router.get("/accounts/export")
async def export_gpt_image_accounts(
    include_secrets: bool = Query(False),
    repo: "AccountRepository" = Depends(get_repo),
):
    records = await _list_all_gpt_image_records(repo)
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
async def add_gpt_image_accounts(
    req: GPTImageAccountsRequest,
    repo: "AccountRepository" = Depends(get_repo),
):
    cleaned: list[GPTImageAccountItem] = []
    seen: set[str] = set()
    for raw in req.accounts:
        item = _item_from_raw(raw, default_is_free=req.is_free)
        key = _dedupe_key_for_item(item)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(item)

    if not cleaned:
        raise ValidationError("No valid GPT image accounts provided", param="accounts")

    return await _add_gpt_accounts(
        GPTAccountsRequest(accounts=[_to_gpt_item(item) for item in cleaned]),
        repo=repo,
    )


@router.delete("/accounts")
async def delete_gpt_image_accounts(
    req: GPTImageDeleteRequest = Body(...),
    repo: "AccountRepository" = Depends(get_repo),
):
    tokens = _gpt_delete_record_tokens(req.root)
    if not tokens:
        raise ValidationError("No GPT image accounts specified", param="accounts")
    return await _delete_gpt_accounts(GPTDeleteRequest(root=list(dict.fromkeys(tokens))), repo=repo)


@router.post("/accounts/test")
async def test_gpt_image_accounts(
    req: GPTImageTestRequest | None = Body(default=None),
    concurrency: int | None = Query(None, ge=1),
    repo: "AccountRepository" = Depends(get_repo),
):
    return await _test_gpt_accounts(
        GPTAccountsTestRequest(accounts=req.accounts if req else []),
        concurrency=concurrency,
        repo=repo,
    )


@router.post("/accounts/refresh")
async def refresh_gpt_image_accounts(
    req: GPTImageTestRequest | None = Body(default=None),
    concurrency: int | None = Query(None, ge=1),
    repo: "AccountRepository" = Depends(get_repo),
):
    return await test_gpt_image_accounts(req=req, concurrency=concurrency, repo=repo)


__all__ = [
    "router",
    "GPTImageTestRequest",
    "_summary",
    "_export_record",
    "account_record_token",
    "account_credential_record_token",
]
