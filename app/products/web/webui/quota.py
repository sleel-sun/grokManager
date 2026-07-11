"""Per-WebUI-user daily quota accounting."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from app.platform.errors import RateLimitError
from app.platform.paths import data_path

if TYPE_CHECKING:
    from app.platform.auth.middleware import WebUIUser

QuotaBucket = Literal["grok", "gpt"]

_USAGE_PATH = data_path("webui", "user_quota_usage.json")
_LOCK_PATH = data_path("webui", "user_quota_usage.lock")
_THREAD_LOCK = threading.Lock()


def _today_key() -> str:
    return date.today().isoformat()


def _user_key(user: "WebUIUser") -> str:
    return str(getattr(user, "id", "") or getattr(user, "username", "") or "anonymous")


def _quota_limit(user: "WebUIUser", bucket: QuotaBucket) -> int:
    value = getattr(user, f"{bucket}_daily_quota", 0)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


class _FileLock:
    def __enter__(self):
        _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._fh = _LOCK_PATH.open("a+", encoding="utf-8")
        try:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        self._fh.close()


def _empty_store(today: str | None = None) -> dict:
    return {"date": today or _today_key(), "users": {}}


def _read_store_sync(path: Path = _USAGE_PATH) -> dict:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return _empty_store()
    if not isinstance(parsed, dict):
        return _empty_store()
    today = _today_key()
    if parsed.get("date") != today:
        return _empty_store(today)
    users = parsed.get("users")
    if not isinstance(users, dict):
        parsed["users"] = {}
    return parsed


def _write_store_sync(store: dict, path: Path = _USAGE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(store, fh, ensure_ascii=False, separators=(",", ":"))
            fh.write("\n")
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _bucket_status(limit: int, used: int) -> dict[str, int | bool | None]:
    remaining = None if limit <= 0 else max(0, limit - used)
    return {
        "limit": limit,
        "used": used,
        "remaining": remaining,
        "unlimited": limit <= 0,
    }


def quota_status_for_user(user: "WebUIUser") -> dict[str, object]:
    """Return today's quota status for one WebUI user."""
    with _THREAD_LOCK, _FileLock():
        store = _read_store_sync()
        usage = store.get("users", {}).get(_user_key(user), {})
        if not isinstance(usage, dict):
            usage = {}
        grok_used = int(usage.get("grok") or 0)
        gpt_used = int(usage.get("gpt") or 0)
    grok_limit = _quota_limit(user, "grok")
    gpt_limit = _quota_limit(user, "gpt")
    return {
        "date": _today_key(),
        "grok": _bucket_status(grok_limit, grok_used),
        "gpt": _bucket_status(gpt_limit, gpt_used),
    }


def quota_status_for_users(users: list["WebUIUser"]) -> dict[str, dict[str, object]]:
    """Return today's quota status keyed by WebUI user id."""
    with _THREAD_LOCK, _FileLock():
        store = _read_store_sync()
        raw_users = store.get("users", {})
        raw_users = raw_users if isinstance(raw_users, dict) else {}
    result: dict[str, dict[str, object]] = {}
    for user in users:
        usage = raw_users.get(_user_key(user), {})
        usage = usage if isinstance(usage, dict) else {}
        result[_user_key(user)] = {
            "date": _today_key(),
            "grok": _bucket_status(_quota_limit(user, "grok"), int(usage.get("grok") or 0)),
            "gpt": _bucket_status(_quota_limit(user, "gpt"), int(usage.get("gpt") or 0)),
        }
    return result


def consume_user_quota(
    user: "WebUIUser",
    bucket: QuotaBucket,
    *,
    amount: int = 1,
) -> dict[str, object]:
    """Consume quota for *user* and raise 429 when the daily limit is exceeded."""
    amount = max(1, int(amount or 1))
    limit = _quota_limit(user, bucket)
    if limit <= 0:
        return quota_status_for_user(user)

    with _THREAD_LOCK, _FileLock():
        store = _read_store_sync()
        users = store.setdefault("users", {})
        if not isinstance(users, dict):
            users = {}
            store["users"] = users
        key = _user_key(user)
        usage = users.setdefault(key, {})
        if not isinstance(usage, dict):
            usage = {}
            users[key] = usage
        used = int(usage.get(bucket) or 0)
        if used + amount > limit:
            raise RateLimitError(
                f"WebUI user {getattr(user, 'username', key)!r} exceeded daily {bucket.upper()} quota "
                f"({used}/{limit})."
            )
        usage[bucket] = used + amount
        _write_store_sync(store)

    return quota_status_for_user(user)


__all__ = [
    "consume_user_quota",
    "quota_status_for_user",
    "quota_status_for_users",
]
