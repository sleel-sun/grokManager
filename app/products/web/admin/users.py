"""Admin API for WebUI user management."""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.platform.auth.middleware import WebUIUser, _webui_user_id
from app.platform.config.snapshot import config
from app.products.web.webui.quota import quota_status_for_users

router = APIRouter(prefix="/webui/users", tags=["Admin - WebUI Users"])

_ALL_GPT_IMAGE_MODELS = ("gpt-image-1", "gpt-image-2", "codex-gpt-image-2")
_USERNAME_RE = re.compile(r"^[^\s/:=]{1,64}$")


class WebUIUserPayload(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    key: str = Field(min_length=1, max_length=256)
    api_key: str = Field(default="", max_length=256)
    display_name: str = Field(default="", max_length=96)
    enabled: bool = True
    allow_nsfw: bool = True
    gpt_enabled: bool = False
    gpt_models: list[str] = Field(default_factory=list)
    gpt_image_quality: str = "1k"
    grok_daily_quota: int = Field(default=0, ge=0)
    gpt_daily_quota: int = Field(default=0, ge=0)

    @field_validator("username")
    @classmethod
    def _validate_username(cls, value: str) -> str:
        username = value.strip()
        if not _USERNAME_RE.match(username):
            raise ValueError("username must be 1-64 chars and cannot contain whitespace, /, :, or =")
        return username

    @field_validator("key")
    @classmethod
    def _validate_key(cls, value: str) -> str:
        key = value.strip()
        if not key:
            raise ValueError("key is required")
        return key

    @field_validator("api_key")
    @classmethod
    def _strip_api_key(cls, value: str) -> str:
        return value.strip()

    @field_validator("display_name")
    @classmethod
    def _strip_display_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("gpt_image_quality")
    @classmethod
    def _validate_quality(cls, value: str) -> str:
        quality = _gpt_quality_config_value(value, "1k")
        if quality not in {"1k", "2k", "4k"}:
            raise ValueError("gpt_image_quality must be one of 1k, 2k, 4k")
        return quality


class WebUIUsersPayload(BaseModel):
    users: list[WebUIUserPayload] = Field(default_factory=list)


def _bool_config_value(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return default
        if text in {"1", "true", "yes", "on", "enabled", "allow"}:
            return True
        if text in {"0", "false", "no", "off", "disabled", "deny", "blocked"}:
            return False
    return bool(value)


def _list_config_value(value: object) -> list[str]:
    if value is None:
        raw: list[object] = []
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raw = []
        elif text[0] in "[{":
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                parsed = None
            raw = parsed if isinstance(parsed, list) else [part.strip() for part in text.split(",")]
        else:
            raw = [part.strip() for part in text.replace("\n", ",").split(",")]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = []

    seen: set[str] = set()
    result: list[str] = []
    for item in raw:
        model = str(item or "").strip()
        if model and model not in seen:
            seen.add(model)
            result.append(model)
    return result


def _gpt_quality_config_value(value: object, default: str = "1k") -> str:
    text = str(value or default).strip().lower()
    aliases = {
        "1": "1k",
        "1k": "1k",
        "1024": "1k",
        "2": "2k",
        "2k": "2k",
        "2048": "2k",
        "4": "4k",
        "4k": "4k",
        "4096": "4k",
        "premium": "4k",
        "pro": "4k",
    }
    return aliases.get(text, default)


def _quota_config_value(value: object, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _parse_user_lines(raw: str) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for line in raw.splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        username, sep, key = text.partition("=")
        if not sep:
            username, sep, key = text.partition(":")
        if sep and username.strip() and key.strip():
            entries.append({"username": username.strip(), "key": key.strip()})
    return entries


def _iter_user_entries(raw: object) -> list[object]:
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text[0] in "[{":
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return []
            return _iter_user_entries(parsed)
        return _parse_user_lines(text)
    if isinstance(raw, dict):
        for key in ("users", "webui_users"):
            value = raw.get(key)
            if isinstance(value, list):
                return value
        return [{"username": username, "key": key} for username, key in raw.items()]
    if isinstance(raw, list):
        return raw
    return []


def _normalize_user_entry(entry: object) -> dict[str, object] | None:
    if isinstance(entry, str):
        parsed = _parse_user_lines(entry)
        if not parsed:
            return None
        entry = parsed[0]
    if not isinstance(entry, dict):
        return None

    username = str(entry.get("username") or entry.get("name") or entry.get("id") or "").strip()
    key = str(entry.get("key") or entry.get("password") or entry.get("token") or "").strip()
    if not username or not key:
        return None
    api_key = str(
        entry.get("api_key")
        or entry.get("apiKey")
        or entry.get("openai_api_key")
        or entry.get("openaiApiKey")
        or entry.get("api_call_key")
        or entry.get("apiCallKey")
        or ""
    ).strip()

    display_name = str(entry.get("display_name") or entry.get("displayName") or username).strip()
    enabled = _bool_config_value(entry.get("enabled", True), True)
    allow_nsfw = _bool_config_value(
        entry.get("allow_nsfw", entry.get("allowNsfw", entry.get("nsfw", entry.get("enable_nsfw")))),
        True,
    )
    gpt_enabled_raw = entry.get(
        "gpt_enabled",
        entry.get("gptEnabled", entry.get("allow_gpt", entry.get("allowGpt"))),
    )
    gpt_models_raw = entry.get(
        "gpt_models",
        entry.get("gptModels", entry.get("gpt_image_models", entry.get("allowed_gpt_models"))),
    )
    gpt_enabled = _bool_config_value(gpt_enabled_raw, False) if gpt_enabled_raw is not None else False
    gpt_models = list(_ALL_GPT_IMAGE_MODELS) if gpt_enabled else _list_config_value(gpt_models_raw)
    if gpt_models:
        gpt_enabled = True

    return {
        "username": username,
        "key": key,
        "api_key": api_key,
        "display_name": display_name or username,
        "enabled": enabled,
        "allow_nsfw": allow_nsfw,
        "gpt_enabled": gpt_enabled,
        "gpt_models": gpt_models if gpt_enabled else [],
        "gpt_image_quality": _gpt_quality_config_value(
            entry.get(
                "gpt_image_quality",
                entry.get("gptImageQuality", entry.get("gpt_quality", entry.get("max_gpt_image_quality"))),
            ),
            "1k",
        ),
        "grok_daily_quota": _quota_config_value(
            entry.get(
                "grok_daily_quota",
                entry.get("grokDailyQuota", entry.get("grok_quota", entry.get("grokQuota"))),
            ),
            0,
        ),
        "gpt_daily_quota": _quota_config_value(
            entry.get(
                "gpt_daily_quota",
                entry.get("gptDailyQuota", entry.get("gpt_quota", entry.get("gptQuota"))),
            ),
            0,
        ),
    }


def normalize_webui_users(raw: object) -> list[dict[str, object]]:
    users: list[dict[str, object]] = []
    seen: set[str] = set()
    for entry in _iter_user_entries(raw):
        user = _normalize_user_entry(entry)
        if user is None:
            continue
        username_key = str(user["username"]).lower()
        if username_key in seen:
            continue
        seen.add(username_key)
        users.append(user)
    return users


def _dump_user(user: WebUIUserPayload) -> dict[str, object]:
    gpt_models = list(_ALL_GPT_IMAGE_MODELS) if user.gpt_enabled else []
    return {
        "username": user.username,
        "key": user.key,
        "api_key": user.api_key,
        "display_name": user.display_name or user.username,
        "enabled": user.enabled,
        "allow_nsfw": user.allow_nsfw,
        "gpt_enabled": user.gpt_enabled,
        "gpt_models": gpt_models,
        "gpt_image_quality": user.gpt_image_quality,
        "grok_daily_quota": user.grok_daily_quota,
        "gpt_daily_quota": user.gpt_daily_quota,
    }


def _ensure_unique(users: list[dict[str, object]]) -> None:
    seen: set[str] = set()
    api_keys: set[str] = set()
    for user in users:
        username = str(user.get("username") or "").strip().lower()
        if username in seen:
            raise HTTPException(status_code=409, detail=f"Duplicate WebUI username: {user.get('username')}")
        seen.add(username)
        api_key = str(user.get("api_key") or "").strip()
        if api_key:
            if api_key in api_keys:
                raise HTTPException(status_code=409, detail="Duplicate WebUI user API key")
            api_keys.add(api_key)


async def _save_users(users: list[dict[str, object]]) -> list[dict[str, object]]:
    _ensure_unique(users)
    await config.update({"app": {"webui_users": users}})
    await config.load()
    return normalize_webui_users(config.get("app.webui_users", []))


def _current_users() -> list[dict[str, object]]:
    return normalize_webui_users(config.get("app.webui_users", []))


def _summary(users: list[dict[str, object]]) -> dict[str, int]:
    return {
        "total": len(users),
        "enabled": sum(1 for user in users if user.get("enabled") is not False),
        "disabled": sum(1 for user in users if user.get("enabled") is False),
        "nsfw_allowed": sum(1 for user in users if user.get("allow_nsfw") is not False),
        "gpt_enabled": sum(1 for user in users if user.get("gpt_enabled") is True),
        "grok_limited": sum(1 for user in users if int(user.get("grok_daily_quota") or 0) > 0),
        "gpt_limited": sum(1 for user in users if int(user.get("gpt_daily_quota") or 0) > 0),
    }


def _webui_user_from_payload(user: dict[str, object]) -> WebUIUser:
    return WebUIUser(
        id=_webui_user_id(str(user.get("username") or "")),
        username=str(user.get("username") or ""),
        display_name=str(user.get("display_name") or user.get("username") or ""),
        grok_daily_quota=int(user.get("grok_daily_quota") or 0),
        gpt_daily_quota=int(user.get("gpt_daily_quota") or 0),
    )


def _users_with_quota_usage(users: list[dict[str, object]]) -> list[dict[str, object]]:
    quota_users = [_webui_user_from_payload(user) for user in users]
    usage = quota_status_for_users(quota_users)
    result: list[dict[str, object]] = []
    for user in users:
        row = dict(user)
        row["quota_usage"] = usage.get(_webui_user_id(str(user.get("username") or "")), {})
        result.append(row)
    return result


def _response(users: list[dict[str, object]]) -> dict[str, Any]:
    return {
        "users": _users_with_quota_usage(users),
        "summary": _summary(users),
        "webui_enabled": config.get_bool("app.webui_enabled", False),
        "legacy_key_configured": bool(str(config.get("app.webui_key", "") or "").strip()),
    }


@router.get("")
async def list_webui_users():
    users = _current_users()
    return _response(users)


@router.put("")
async def replace_webui_users(req: WebUIUsersPayload):
    users = [_dump_user(user) for user in req.users]
    return _response(await _save_users(users))


@router.post("")
async def create_webui_user(req: WebUIUserPayload):
    users = _current_users()
    candidate = _dump_user(req)
    users.append(candidate)
    return _response(await _save_users(users))


@router.patch("/{username}")
async def update_webui_user(username: str, req: WebUIUserPayload):
    users = _current_users()
    target = username.strip().lower()
    changed = False
    replacement = _dump_user(req)
    for index, user in enumerate(users):
        if str(user.get("username") or "").strip().lower() == target:
            users[index] = replacement
            changed = True
            break
    if not changed:
        raise HTTPException(status_code=404, detail="WebUI user not found")
    return _response(await _save_users(users))


@router.delete("/{username}")
async def delete_webui_user(username: str):
    users = _current_users()
    target = username.strip().lower()
    next_users = [
        user for user in users
        if str(user.get("username") or "").strip().lower() != target
    ]
    if len(next_users) == len(users):
        raise HTTPException(status_code=404, detail="WebUI user not found")
    return _response(await _save_users(next_users))


__all__ = ["router", "normalize_webui_users"]
