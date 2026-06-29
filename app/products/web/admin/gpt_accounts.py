"""Admin API for ordinary ChatGPT account records."""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import aiohttp
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
_LEGACY_IMAGE_TAG = "gpt-image"
_MAX_TEST_CONCURRENCY = 10
_CHATGPT_BASE_URL = "https://chatgpt.com"
_CHATGPT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0"
)
_CHATGPT_CLIENT_VERSION = "prod-be885abbfcfe7b1f511e88b3003d9ee44757fbad"
_CHATGPT_CLIENT_BUILD_NUMBER = "5955942"


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def gpt_account_record_token(access_token: str) -> str:
    digest = hashlib.sha256(access_token.encode("utf-8")).hexdigest()
    return f"gpt_{digest[:40]}"


def gpt_account_credential_record_token(email: str) -> str:
    digest = hashlib.sha256(_clean_text(email).lower().encode("utf-8")).hexdigest()
    return f"gptcred_{digest[:40]}"


def _legacy_image_record_token(access_token: str) -> str:
    digest = hashlib.sha256(access_token.encode("utf-8")).hexdigest()
    return f"gptimg_{digest[:40]}"


def _legacy_image_credential_record_token(email: str) -> str:
    digest = hashlib.sha256(_clean_text(email).lower().encode("utf-8")).hexdigest()
    return f"gptimgcred_{digest[:40]}"


def _mask(value: str) -> str:
    value = str(value or "")
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    if len(value) <= 20:
        return f"{value[:3]}...{value[-3:]}"
    return f"{value[:8]}...{value[-8:]}"


def _access_token_from_login_result(value: Any) -> tuple[str, str]:
    """Accept ChatGPT session JSON, a CodexManager-style snapshot, or a raw token."""
    if isinstance(value, dict):
        data = value
        token = _clean_text(data.get("accessToken") or data.get("access_token"))
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        email = _clean_text((user or {}).get("email") or data.get("email"))
        return token, email
    attr_token = _clean_text(getattr(value, "access_token", ""))
    if attr_token:
        return attr_token, _clean_text(getattr(value, "email", ""))
    raw = _clean_text(value)
    if not raw:
        return "", ""
    token = raw[7:].strip() if raw.lower().startswith("bearer ") else raw
    if not token.startswith(("{", "[")):
        match = re.search(r'"access(?:Token|_token)"\s*:\s*"([^"]+)"', raw)
        if match:
            return _clean_text(match.group(1)), ""
    if token and not token.startswith(("http://", "https://", "{", "[")) and "\n" not in token:
        return token, ""
    try:
        data = orjson.loads(raw)
    except orjson.JSONDecodeError:
        match = re.search(r'"access(?:Token|_token)"\s*:\s*"([^"]+)"', raw)
        return (_clean_text(match.group(1)), "") if match else ("", "")
    if not isinstance(data, dict):
        return "", ""
    href = _clean_text(data.get("href"))
    text = _clean_text(data.get("text"))
    if href and text:
        parsed = urlparse(href)
        if parsed.scheme == "https" and parsed.netloc.lower() == "chatgpt.com" and parsed.path == "/api/auth/session":
            return _access_token_from_login_result(text)
        return "", ""
    token = _clean_text(data.get("accessToken") or data.get("access_token"))
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    email = _clean_text((user or {}).get("email") or data.get("email"))
    return token, email


def _looks_like_direct_access_token_payload(value: Any) -> bool:
    raw = _clean_text(value)
    if not raw:
        return False
    lower = raw.lower()
    return (
        lower.startswith("bearer ")
        or raw.startswith(("{", "["))
        or "accesstoken" in lower
        or "access_token" in lower
        or "chatgpt.com/api/auth/session" in lower
    )


def _now_ms() -> int:
    return int(time.time() * 1000)


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        import base64

        data = orjson.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _chatgpt_account_id(access_token: str) -> str:
    auth = _decode_jwt_payload(access_token).get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        return _clean_text(auth.get("chatgpt_account_id"))
    return ""


def _chatgpt_headers(access_token: str, path: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "user-agent": _CHATGPT_USER_AGENT,
        "authorization": f"Bearer {access_token}",
        "origin": _CHATGPT_BASE_URL,
        "referer": f"{_CHATGPT_BASE_URL}/",
        "accept": "application/json",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "oai-language": "zh-CN",
        "oai-client-version": _CHATGPT_CLIENT_VERSION,
        "oai-client-build-number": _CHATGPT_CLIENT_BUILD_NUMBER,
        "x-openai-target-path": path,
        "x-openai-target-route": path,
    }
    account_id = _chatgpt_account_id(access_token)
    if account_id:
        headers["chatgpt-account-id"] = account_id
    if extra:
        headers.update(extra)
    return headers


async def _chatgpt_json(
    session: aiohttp.ClientSession,
    method: str,
    path: str,
    access_token: str,
    *,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{_CHATGPT_BASE_URL}{path}"
    target_path = path.split("?", 1)[0]
    headers = _chatgpt_headers(
        access_token,
        target_path,
        {"content-type": "application/json"} if json_body is not None else None,
    )
    async with session.request(
        method,
        url,
        headers=headers,
        json=json_body,
        timeout=aiohttp.ClientTimeout(total=20),
    ) as response:
        text = await response.text()
        if response.status == 401:
            raise ValidationError("GPT access token is invalid or expired", param="account", code="invalid_access_token")
        if response.status >= 400:
            raise AppError(
                f"ChatGPT account detail request failed: {path} HTTP {response.status}",
                kind=ErrorKind.UPSTREAM,
                status=502,
                details={"body": text[:500]},
            )
        try:
            data = orjson.loads(text)
        except orjson.JSONDecodeError as exc:
            raise AppError(
                f"ChatGPT account detail request returned invalid JSON: {path}",
                kind=ErrorKind.UPSTREAM,
                status=502,
            ) from exc
        return data if isinstance(data, dict) else {}


def _extract_image_quota(limits_progress: list[Any]) -> tuple[int, str | None, bool]:
    for item in limits_progress:
        if isinstance(item, dict) and item.get("feature_name") == "image_gen":
            try:
                remaining = int(item.get("remaining") or 0)
            except (TypeError, ValueError):
                remaining = 0
            return remaining, _clean_text(item.get("reset_after")) or None, False
    return 0, None, True


async def _fetch_gpt_remote_detail(access_token: str) -> dict[str, Any]:
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as session:
        me_future = _chatgpt_json(session, "GET", "/backend-api/me", access_token)
        init_future = _chatgpt_json(
            session,
            "POST",
            "/backend-api/conversation/init",
            access_token,
            json_body={
                "gizmo_id": None,
                "requested_default_model": None,
                "conversation_id": None,
                "timezone_offset_min": -480,
            },
        )
        account_future = _chatgpt_json(
            session,
            "GET",
            "/backend-api/accounts/check/v4-2023-04-27?timezone_offset_min=-480",
            access_token,
        )
        me_payload, init_payload, account_payload = await asyncio.gather(
            me_future,
            init_future,
            account_future,
        )

    default_account = ((account_payload.get("accounts") or {}).get("default") or {}).get("account") or {}
    if not isinstance(default_account, dict):
        default_account = {}
    limits_progress = init_payload.get("limits_progress")
    limits_progress = limits_progress if isinstance(limits_progress, list) else []
    image_quota, image_restore_at, image_quota_unknown = _extract_image_quota(limits_progress)
    return {
        "email": _clean_text(me_payload.get("email")) or None,
        "user_id": _clean_text(me_payload.get("id")) or None,
        "plan_type": _clean_text(default_account.get("plan_type")) or "free",
        "default_model_slug": _clean_text(init_payload.get("default_model_slug")) or None,
        "limits_progress": limits_progress,
        "image_quota": image_quota,
        "image_restore_at": image_restore_at,
        "image_quota_unknown": image_quota_unknown,
        "account": default_account,
    }


def _remote_detail_ext(detail: dict[str, Any], existing_ext: dict[str, Any]) -> dict[str, Any]:
    now = _now_ms()
    plan_type = _clean_text(detail.get("plan_type"))
    email = _clean_text(detail.get("email"))
    ext_merge: dict[str, Any] = {
        "gpt": True,
        "gpt_status": "available",
        "gpt_registration_error": None,
        "gpt_last_checked_at": now,
        "gpt_last_remote_refresh_at": now,
        "gpt_remote_error": None,
        "gpt_remote_user_id": _clean_text(detail.get("user_id")) or None,
        "gpt_default_model_slug": _clean_text(detail.get("default_model_slug")) or None,
        "gpt_limits_progress": detail.get("limits_progress") if isinstance(detail.get("limits_progress"), list) else [],
        "gpt_image_quota": int(detail.get("image_quota") or 0),
        "gpt_image_quota_unknown": bool(detail.get("image_quota_unknown")),
        "gpt_image_restore_at": _clean_text(detail.get("image_restore_at")) or None,
        "gpt_remote_account": detail.get("account") if isinstance(detail.get("account"), dict) else {},
    }
    if email:
        ext_merge["gpt_email"] = email
    if plan_type:
        ext_merge["gpt_plan_type"] = plan_type
    if existing_ext.get("gpt_image") and plan_type:
        ext_merge["gpt_image_is_free"] = plan_type.lower() in {"", "free", "basic"}
    return ext_merge


class GPTAccountItem(BaseModel):
    access_token: str | None = Field(default=None)
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


class GPTAccountDetailRequest(BaseModel):
    account: str = ""


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
    ext = record.ext or {}
    tags = set(record.tags or [])
    return (
        _TAG in tags
        or _LEGACY_IMAGE_TAG in tags
        or bool(ext.get("gpt"))
        or bool(ext.get("gpt_access_token"))
        or bool(ext.get("gpt_image"))
        or bool(ext.get("gpt_image_access_token"))
    )


def _record_access_token(ext: dict[str, Any]) -> str:
    return _clean_text(ext.get("gpt_access_token") or ext.get("gpt_image_access_token"))


def _record_email(ext: dict[str, Any]) -> str:
    return _clean_text(ext.get("gpt_email") or ext.get("gpt_image_email"))


def _record_alias(ext: dict[str, Any]) -> str:
    return _clean_text(ext.get("gpt_alias") or ext.get("gpt_image_alias"))


def _record_password(ext: dict[str, Any]) -> str:
    return _clean_text(ext.get("gpt_password") or ext.get("gpt_image_password"))


def _record_mail_token(ext: dict[str, Any]) -> str:
    return _clean_text(ext.get("gpt_mail_token") or ext.get("gpt_image_mail_token"))


def _record_email_provider(ext: dict[str, Any]) -> str:
    return _clean_text(ext.get("gpt_email_provider") or ext.get("gpt_image_email_provider"))


def _record_plan_type(ext: dict[str, Any]) -> str:
    plan = _clean_text(ext.get("gpt_plan_type"))
    if plan:
        return plan
    if ext.get("gpt_image") or ext.get("gpt_image_access_token"):
        return "free" if ext.get("gpt_image_is_free") else "plus"
    return ""


def _record_status(ext: dict[str, Any]) -> str:
    return _clean_text(ext.get("gpt_status") or ext.get("gpt_image_status")) or "unknown"


def _record_error(ext: dict[str, Any]) -> str:
    return _clean_text(
        ext.get("gpt_registration_error")
        or ext.get("gpt_image_error")
        or ext.get("gpt_image_login_error")
    )


def _record_last_checked_at(ext: dict[str, Any]) -> Any:
    return ext.get("gpt_last_checked_at") or ext.get("gpt_image_last_checked_at")


def _record_login_attempt_at(ext: dict[str, Any]) -> Any:
    return ext.get("gpt_login_attempt_at") or ext.get("gpt_image_login_attempt_at")


def _record_cooldown_until(ext: dict[str, Any]) -> Any:
    return ext.get("gpt_cooldown_until") or ext.get("gpt_image_cooldown_until")


def _record_last_remote_refresh_at(ext: dict[str, Any]) -> Any:
    return ext.get("gpt_last_remote_refresh_at") or ext.get("gpt_image_last_remote_refresh_at")


def _record_identity(record: "AccountRecord") -> str:
    ext = record.ext or {}
    access_token = _record_access_token(ext)
    if access_token:
        return f"token:{hashlib.sha256(access_token.encode('utf-8')).hexdigest()}"
    email = _record_email(ext).lower()
    if email:
        return f"email:{email}"
    return f"id:{record.token}"


def _record_priority(record: "AccountRecord") -> int:
    ext = record.ext or {}
    tags = set(record.tags or [])
    if ext.get("gpt") or _TAG in tags or record.token.startswith(("gpt_", "gptcred_")):
        return 0
    return 1


def _dedupe_gpt_records(records: list["AccountRecord"]) -> list["AccountRecord"]:
    chosen: dict[str, "AccountRecord"] = {}
    order: list[str] = []
    for record in records:
        key = _record_identity(record)
        existing = chosen.get(key)
        if existing is None:
            chosen[key] = record
            order.append(key)
            continue
        if _record_priority(record) < _record_priority(existing):
            chosen[key] = record
    return [chosen[key] for key in order]


def _serialize(record: "AccountRecord") -> dict[str, Any]:
    ext = record.ext or {}
    access_token = _record_access_token(ext)
    email = _record_email(ext) or None
    alias = _record_alias(ext) or None
    plan_type = _record_plan_type(ext) or None
    error = _record_error(ext) or None
    password = _record_password(ext)
    mail_token = _record_mail_token(ext)
    legacy_image_account = bool(ext.get("gpt_image") or _LEGACY_IMAGE_TAG in (record.tags or []))
    return {
        "id": record.token,
        "status": record.status,
        "email": email,
        "alias": alias,
        "plan_type": plan_type,
        "email_provider": _record_email_provider(ext) or None,
        "capability_status": _record_status(ext),
        "capability_error": error,
        "last_checked_at": _record_last_checked_at(ext),
        "last_login_attempt_at": _record_login_attempt_at(ext),
        "last_remote_refresh_at": _record_last_remote_refresh_at(ext),
        "cooldown_until": _record_cooldown_until(ext),
        "access_token_masked": _mask(access_token),
        "has_access_token": bool(access_token),
        "has_credentials": bool(password and mail_token),
        "has_password": bool(password),
        "has_mail_token": bool(mail_token),
        "registration_error": error,
        "legacy_image_account": legacy_image_account,
        "source_type": "legacy_image" if legacy_image_account else "gpt",
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "last_used_at": record.last_use_at,
        "last_fail_at": record.last_fail_at,
        "last_fail_reason": record.last_fail_reason,
        "use_count": record.usage_use_count,
        "fail_count": record.usage_fail_count,
        "remote_user_id": ext.get("gpt_remote_user_id") or ext.get("gpt_image_remote_user_id"),
        "default_model_slug": ext.get("gpt_default_model_slug") or ext.get("gpt_image_default_model_slug"),
        "limits_progress": ext.get("gpt_limits_progress") or ext.get("gpt_image_limits_progress") or [],
        "image_quota": ext.get("gpt_image_quota"),
        "image_quota_unknown": ext.get("gpt_image_quota_unknown"),
        "image_restore_at": ext.get("gpt_image_restore_at"),
        "remote_error": ext.get("gpt_remote_error") or ext.get("gpt_image_remote_error"),
    }


def _summary(records: list["AccountRecord"]) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    plan_counts: dict[str, int] = {}
    with_access_token = 0
    with_credentials = 0
    total_available_quota = 0
    available_quota_unknown = 0
    for record in records:
        ext = record.ext or {}
        status = _record_status(ext)
        plan = (_record_plan_type(ext) or "unknown").lower()
        status_counts[status] = status_counts.get(status, 0) + 1
        plan_counts[plan] = plan_counts.get(plan, 0) + 1
        if status == "available":
            if ext.get("gpt_image_quota_unknown") is True:
                available_quota_unknown += 1
            else:
                try:
                    total_available_quota += int(ext.get("gpt_image_quota") or 0)
                except (TypeError, ValueError):
                    available_quota_unknown += 1
        if _record_access_token(ext):
            with_access_token += 1
        if _record_password(ext) and _record_mail_token(ext):
            with_credentials += 1

    return {
        "total": len(records),
        "available": status_counts.get("available", 0),
        "unchecked": status_counts.get("unchecked", 0),
        "login_required": status_counts.get("login_required", 0),
        "invalid": status_counts.get("invalid", 0),
        "total_available_quota": total_available_quota,
        "available_quota_unknown": available_quota_unknown,
        "with_access_token": with_access_token,
        "with_credentials": with_credentials,
        "status": status_counts,
        "plans": plan_counts,
    }


def _export_record(record: "AccountRecord", *, include_secrets: bool) -> dict[str, Any]:
    ext = record.ext or {}
    payload = _serialize(record)
    payload["registration_status"] = _record_status(ext)
    if include_secrets:
        payload.update(
            {
                "access_token": _record_access_token(ext) or None,
                "password": _record_password(ext) or None,
                "mail_token": _record_mail_token(ext) or None,
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
    return _dedupe_gpt_records(records)


def _same_email(left: str, right: str) -> bool:
    return _clean_text(left).lower() == _clean_text(right).lower()


async def _find_gpt_record_by_email(
    repo: "AccountRepository",
    email: str,
) -> "AccountRecord | None":
    email = _clean_text(email)
    if not email:
        return None

    preferred_tokens = [
        gpt_account_credential_record_token(email),
        _legacy_image_credential_record_token(email),
    ]
    preferred_records = await repo.get_accounts(preferred_tokens)
    preferred_candidates = [
        record
        for record in preferred_records
        if _is_gpt_record(record)
        and not record.is_deleted()
        and (
            record.token in preferred_tokens
            or _same_email(_record_email(record.ext or {}), email)
        )
    ]
    for token in preferred_tokens:
        match = next((record for record in preferred_candidates if record.token == token), None)
        if match:
            return match

    records = await _list_all_gpt_records(repo)
    return next(
        (
            record
            for record in records
            if not record.is_deleted()
            and _same_email(_record_email(record.ext or {}), email)
        ),
        None,
    )


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
        if value.startswith(("gpt_", "gptcred_", "gptimg_", "gptimgcred_")):
            tokens.append(value)
        elif "@" in value and not value.lower().startswith("bearer "):
            tokens.append(gpt_account_credential_record_token(value))
            tokens.append(_legacy_image_credential_record_token(value))
        else:
            access_token = _access_token_value(value)
            tokens.append(gpt_account_record_token(access_token))
            tokens.append(_legacy_image_record_token(access_token))
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
    return _dedupe_gpt_records([record for record in records if _is_gpt_record(record)])


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
    candidates = _dedupe_gpt_records(
        [item for item in records if _is_gpt_record(item) and not item.is_deleted()]
    )
    record = next(iter(candidates), None)
    if not record:
        raise ValidationError("GPT account not found", param=param, code="account_not_found")
    return record


async def _refresh_gpt_record_remote_detail(
    repo: "AccountRepository",
    record: "AccountRecord",
) -> dict[str, Any]:
    ext = record.ext or {}
    access_token = _record_access_token(ext)
    if not access_token:
        message = "No access token is available; login this GPT account before refreshing remote details"
        return {
            "refreshed": False,
            "error": message,
            "account": _serialize(record),
        }

    try:
        detail = await _fetch_gpt_remote_detail(access_token)
        await repo.patch_accounts(
            [
                AccountPatch(
                    token=record.token,
                    ext_merge=_remote_detail_ext(detail, ext),
                )
            ]
        )
        refreshed = next(iter(await repo.get_accounts([record.token])), record)
        return {"refreshed": True, "account": _serialize(refreshed)}
    except ValidationError as exc:
        message = str(exc)[:500]
        now = _now_ms()
        await repo.patch_accounts(
            [
                AccountPatch(
                    token=record.token,
                    last_fail_at=now,
                    last_fail_reason=message,
                    ext_merge={
                        "gpt_status": "invalid",
                        "gpt_registration_error": message,
                        "gpt_remote_error": message,
                        "gpt_last_remote_refresh_at": now,
                    },
                )
            ]
        )
        refreshed = next(iter(await repo.get_accounts([record.token])), record)
        return {"refreshed": False, "error": message, "account": _serialize(refreshed)}
    except Exception as exc:
        message = (str(exc) or exc.__class__.__name__)[:500]
        now = _now_ms()
        await repo.patch_accounts(
            [
                AccountPatch(
                    token=record.token,
                    last_fail_at=now,
                    last_fail_reason=message,
                    ext_merge={
                        "gpt_remote_error": message,
                        "gpt_last_remote_refresh_at": now,
                    },
                )
            ]
        )
        refreshed = next(iter(await repo.get_accounts([record.token])), record)
        return {"refreshed": False, "error": message, "account": _serialize(refreshed)}


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
    records = await repo.get_accounts([upsert.token for upsert in upserts])
    refresh_results: list[dict[str, Any]] = []
    for record in records:
        if _record_access_token(record.ext or {}):
            refresh_results.append(await _refresh_gpt_record_remote_detail(repo, record))

    remote_refreshed = sum(1 for item in refresh_results if item.get("refreshed"))
    remote_failed = sum(1 for item in refresh_results if not item.get("refreshed"))
    return _json(
        {
            "status": "success",
            "count": result.upserted or len(upserts),
            "remote_refreshed": remote_refreshed,
            "remote_failed": remote_failed,
            "remote_skipped": max(0, len(upserts) - len(refresh_results)),
            "accounts": [item.get("account") for item in refresh_results if item.get("account")],
        }
    )


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


@router.post("/accounts/detail")
async def get_gpt_account_detail(
    req: GPTAccountDetailRequest,
    repo: "AccountRepository" = Depends(get_repo),
):
    account_ref = _clean_text(req.account)
    if not account_ref:
        raise ValidationError("GPT account is required", param="account")
    record = await _lookup_gpt_record(repo, account_ref)
    return _json({"status": "success", **await _refresh_gpt_record_remote_detail(repo, record)})


@router.post("/accounts/token")
async def get_gpt_account_token(
    req: GPTAccountDetailRequest,
    repo: "AccountRepository" = Depends(get_repo),
):
    account_ref = _clean_text(req.account)
    if not account_ref:
        raise ValidationError("GPT account is required", param="account")
    record = await _lookup_gpt_record(repo, account_ref)
    access_token = _record_access_token(record.ext or {})
    if not access_token:
        raise ValidationError(
            "No access token is saved for this GPT account",
            param="account",
            code="missing_access_token",
        )
    return _json(
        {
            "status": "success",
            "access_token": access_token,
            "access_token_masked": _mask(access_token),
            "account": _serialize(record),
        }
    )


@router.post("/accounts/oauth/start")
async def start_gpt_account_oauth(
    req: GPTAccountOAuthStartRequest,
    repo: "AccountRepository" = Depends(get_repo),
):
    email_hint = _clean_text(req.email_hint)
    account_ref = _clean_text(req.account)
    if account_ref:
        record = await _lookup_gpt_record(repo, account_ref)
        email_hint = email_hint or _record_email(record.ext or {})

    try:
        from app.maintainer.gpt_oauth import GPTAccountOAuthError, gpt_oauth_login_service

        data = await run_in_threadpool(gpt_oauth_login_service.start, email_hint)
    except GPTAccountOAuthError as exc:
        raise ValidationError(str(exc), param="login", code="oauth_start_failed") from exc
    except Exception as exc:
        raise ValidationError(str(exc), param="login", code="oauth_start_failed") from exc

    return _json(
        {
            "status": "success",
            **data,
            "email_hint": email_hint,
        }
    )


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
        email = email or _record_email(ext)
        alias = alias or _record_alias(ext)
        plan_type = plan_type or _record_plan_type(ext)

    access_token = ""
    direct_email = ""
    if _looks_like_direct_access_token_payload(req.callback):
        access_token, direct_email = _access_token_from_login_result(req.callback)
        email = direct_email or email

    if not access_token:
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
    if not access_token:
        raise AppError(
            "GPT OAuth login did not return an access token",
            kind=ErrorKind.UPSTREAM,
            code="gpt_oauth_no_token",
            status=502,
        )

    saved_id = record.token if record else ""
    if req.save:
        if record is None:
            record = await _find_gpt_record_by_email(repo, email)
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
    saved_id = record.token if record else saved_id

    return _json(
        {
            "status": "success",
            "access_token": access_token,
            "access_token_masked": _mask(access_token),
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
        record = await _lookup_gpt_record(repo, account_ref)
        ext = record.ext or {}
        email = email or _record_email(ext)
        password = password or _record_password(ext)
        mail_token = mail_token or _record_mail_token(ext)
        email_provider = email_provider or _record_email_provider(ext)
        alias = alias or _record_alias(ext)
        plan_type = plan_type or _record_plan_type(ext)

    if not email:
        raise ValidationError("Email is required", param="email")
    if not password:
        raise ValidationError("Password is required", param="password")
    if not mail_token:
        raise ValidationError("Mail token is required", param="mail_token")

    try:
        from app.maintainer import gpt as gpt_module

        login_result = await run_in_threadpool(
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

    access_token, session_email = _access_token_from_login_result(login_result)
    email = session_email or email
    if not access_token:
        raise AppError(
            "GPT account login did not return an access token",
            kind=ErrorKind.UPSTREAM,
            code="gpt_login_no_token",
            status=502,
        )

    saved_id = record.token if record else ""
    if req.save:
        if record is None:
            record = await _find_gpt_record_by_email(repo, email)
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
    saved_id = record.token if record else saved_id

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
    "GPTAccountDetailRequest",
    "GPTAccountLoginRequest",
    "GPTAccountOAuthStartRequest",
    "GPTAccountOAuthFinishRequest",
    "_ext_for_item",
    "_summary",
    "_export_record",
    "_delete_record_tokens",
    "get_gpt_account_token",
    "gpt_account_record_token",
    "gpt_account_credential_record_token",
]
