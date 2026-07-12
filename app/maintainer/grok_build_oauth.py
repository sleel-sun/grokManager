"""Automate Grok Build device authorization from existing Grok SSO tokens."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from curl_cffi import requests

from app.platform.config.snapshot import get_config

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_DEFAULT_LOGGER = logging.getLogger(__name__)
_POOL_THREAD_LOCK = threading.RLock()
_POOL_ENTRY_LOCKS_GUARD = threading.Lock()
_POOL_ENTRY_LOCKS: dict[str, threading.RLock] = {}
_AUTO_REFRESH_RETRY_AFTER: dict[str, float] = {}


def _oauth_config() -> tuple[str, str, str]:
    cfg = get_config()
    client_id = cfg.get_str(
        "grok_build.oauth_client_id",
        "b1a00492-073a-47ea-816f-4c329264a828",
    )
    token_url = cfg.get_str(
        "grok_build.oauth_token_url",
        "https://auth.x.ai/oauth2/token",
    )
    scope = cfg.get_str(
        "grok_build.oauth_scope",
        "openid profile email offline_access grok-cli:access api:access "
        "conversations:read conversations:write",
    )
    return client_id, token_url, scope


def _proxy_url(explicit: str | None = None) -> str:
    cfg = get_config()
    for value in (
        explicit,
        cfg.get_str("grok_build.oauth_proxy", ""),
        cfg.get_str("grok_build.proxy", ""),
        os.getenv("MAINTAINER_PROXY"),
        os.getenv("GROK_PROXY_EGRESS_PROXY_URL"),
        os.getenv("HTTPS_PROXY"),
        os.getenv("https_proxy"),
        os.getenv("HTTP_PROXY"),
        os.getenv("http_proxy"),
    ):
        normalized = str(value or "").strip()
        if normalized:
            return normalized
    return ""


def _proxy_log_label(proxy: str) -> str:
    if not proxy:
        return "(none)"
    try:
        parsed = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
        hostname = parsed.hostname or "?"
        port = f":{parsed.port}" if parsed.port else ""
        userinfo = "user:***@" if parsed.username else ""
        return urlunsplit(
            (parsed.scheme or "http", f"{userinfo}{hostname}{port}", "", "", "")
        )
    except (TypeError, ValueError):
        return "(proxy)"


def _configure_proxy(session: requests.Session, proxy: str | None = None) -> str:
    resolved = _proxy_url(proxy)
    if resolved:
        session.proxies = {"http": resolved, "https": resolved}
    return resolved


def _body_contains(response: Any, *markers: str) -> bool:
    try:
        body = str(response.text or "").lower()
    except Exception:
        return False
    return any(marker.lower() in body for marker in markers)


def request_device_code(session: requests.Session) -> dict[str, Any]:
    client_id, token_url, scope = _oauth_config()
    device_url = token_url.rsplit("/", 1)[0] + "/device/code"
    response = session.post(
        device_url,
        data={"client_id": client_id, "scope": scope},
        impersonate="chrome",
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"device authorization failed: HTTP {response.status_code}: "
            f"{response.text[:240]}"
        )
    payload = response.json()
    if not payload.get("device_code") or not payload.get("user_code"):
        raise RuntimeError("device authorization returned no device code")
    return payload


def poll_device_token(
    session: requests.Session,
    device_code: str,
    *,
    expires_in: int,
    interval: int,
) -> dict[str, Any]:
    client_id, token_url, _scope = _oauth_config()
    deadline = time.monotonic() + max(30, expires_in)
    delay = max(1, interval)
    while time.monotonic() < deadline:
        try:
            response = session.post(
                token_url,
                data={
                    "grant_type": DEVICE_GRANT,
                    "client_id": client_id,
                    "device_code": device_code,
                },
                timeout=30,
                impersonate="chrome",
            )
        except Exception:
            time.sleep(delay)
            continue
        try:
            payload = response.json()
        except Exception:
            payload = None
        if (
            response.status_code == 200
            and isinstance(payload, dict)
            and payload.get("access_token")
        ):
            return payload
        if response.status_code >= 500 or not isinstance(payload, dict):
            time.sleep(delay)
            continue
        error = str(payload.get("error") or "")
        if error == "slow_down":
            delay += 5
        elif error not in {"authorization_pending", "slow_down"}:
            raise RuntimeError(
                f"device token exchange failed: {error or response.status_code}"
            )
        time.sleep(delay)
    raise TimeoutError("device authorization expired")


def authorize_device_with_sso(
    session: requests.Session,
    token: str,
    verification_url: str,
    user_code: str,
) -> None:
    session.cookies.set("sso", token, domain=".x.ai")
    account = session.get(
        "https://accounts.x.ai/",
        impersonate="chrome",
        timeout=30,
    )
    if "sign-in" in account.url or "sign-up" in account.url:
        raise RuntimeError("SSO token is invalid for accounts.x.ai")

    session.get(verification_url, impersonate="chrome", timeout=30)
    verify = session.post(
        "https://auth.x.ai/oauth2/device/verify",
        data={"user_code": user_code},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        impersonate="chrome",
        timeout=30,
        allow_redirects=True,
    )
    if "consent" not in verify.url:
        if not _body_contains(
            verify,
            "consent",
            "authorize grok build",
            "授权 grok build",
        ):
            raise RuntimeError(f"device verification failed: {verify.url}")

    approve = session.post(
        "https://auth.x.ai/oauth2/device/approve",
        data={
            "user_code": user_code,
            "action": "allow",
            "principal_type": "User",
            "principal_id": "",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        impersonate="chrome",
        timeout=30,
        allow_redirects=True,
    )
    if "done" not in approve.url:
        if not _body_contains(
            approve,
            "device authorized",
            "device has been authorized",
            "设备已授权",
            "authorization complete",
            "done",
        ):
            raise RuntimeError(f"device approval failed: {approve.url}")


def pool_path() -> Path:
    configured = get_config().get_str("grok_build.auth_file", "data/grok_auth.json")
    path = Path(configured).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


class _PoolFileLock:
    def __init__(self, path: Path) -> None:
        self._path = path.with_suffix(path.suffix + ".lock")
        self._fh: Any = None

    def __enter__(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._path.open("a+", encoding="utf-8")
        try:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        self._fh.close()


@contextmanager
def pool_file_lock(path: Path | None = None):
    resolved = path or pool_path()
    with _POOL_THREAD_LOCK, _PoolFileLock(resolved):
        yield resolved


@contextmanager
def pool_entry_refresh_lock(source_id: str):
    with _POOL_ENTRY_LOCKS_GUARD:
        thread_lock = _POOL_ENTRY_LOCKS.setdefault(source_id, threading.RLock())
    digest = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:24]
    lock_path = pool_path().with_name(f".{pool_path().name}.{digest}.refresh")
    with thread_lock, _PoolFileLock(lock_path):
        yield


def _read_pool_document_unlocked(
    path: Path, *, missing_ok: bool = True
) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if missing_ok:
            return {}
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid Grok Build OAuth pool: {path}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"Invalid Grok Build OAuth pool: {path}")
    return document


def read_pool_document() -> dict[str, Any]:
    with pool_file_lock() as path:
        return _read_pool_document_unlocked(path)


def _write_pool_document_unlocked(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(document, fh, ensure_ascii=True, indent=2)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _is_single_credential(document: dict[str, Any]) -> bool:
    return "key" in document or "access_token" in document


def pool_entries(document: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    source = read_pool_document() if document is None else document
    if _is_single_credential(source):
        return {"default": source}
    return {
        str(source_id): value
        for source_id, value in source.items()
        if isinstance(value, dict) and (value.get("key") or value.get("access_token"))
    }


def _aggregate_document(document: dict[str, Any]) -> dict[str, Any]:
    return {"default": document} if _is_single_credential(document) else document


def save_pool_entry(
    source_id: str,
    entry: dict[str, Any],
    *,
    require_existing: bool = False,
) -> bool:
    with pool_file_lock() as path:
        document = _read_pool_document_unlocked(path)
        if source_id == "default" and _is_single_credential(document):
            _write_pool_document_unlocked(path, dict(entry))
            return True
        document = _aggregate_document(document)
        if require_existing and source_id not in document:
            return False
        document[source_id] = dict(entry)
        _write_pool_document_unlocked(path, document)
        return True


def save_pool_entry_if_refresh_token(
    source_id: str,
    entry: dict[str, Any],
    expected_refresh_token: str,
) -> bool:
    """Persist a refresh result only if no other worker rotated it first."""
    with pool_file_lock() as path:
        document = _read_pool_document_unlocked(path)
        if source_id == "default" and _is_single_credential(document):
            current = document
        else:
            document = _aggregate_document(document)
            current = document.get(source_id)
        if not isinstance(current, dict):
            return False
        if str(current.get("refresh_token") or "") != expected_refresh_token:
            return False
        if source_id == "default" and _is_single_credential(document):
            _write_pool_document_unlocked(path, dict(entry))
        else:
            document[source_id] = dict(entry)
            _write_pool_document_unlocked(path, document)
        return True


def delete_pool_entries(source_ids: list[str]) -> tuple[list[str], list[str]]:
    requested = list(
        dict.fromkeys(str(source_id or "").strip() for source_id in source_ids)
    )
    requested = [source_id for source_id in requested if source_id]
    if not requested:
        return [], []

    with pool_file_lock() as path:
        document = _read_pool_document_unlocked(path)
        if _is_single_credential(document):
            deleted = ["default"] if "default" in requested else []
            not_found = [source_id for source_id in requested if source_id != "default"]
            if deleted:
                _write_pool_document_unlocked(path, {})
            return deleted, not_found

        deleted = [source_id for source_id in requested if source_id in document]
        not_found = [source_id for source_id in requested if source_id not in document]
        if deleted:
            for source_id in deleted:
                del document[source_id]
            _write_pool_document_unlocked(path, document)
        return deleted, not_found


def delete_pool_entry(source_id: str) -> bool:
    deleted, _not_found = delete_pool_entries([source_id])
    return bool(deleted)


def parse_pool_expiry(value: Any) -> float:
    if isinstance(value, (int, float)):
        stamp = float(value)
        return stamp / 1000 if stamp > 10_000_000_000 else stamp
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _jwt_email(value: Any) -> str:
    parts = str(value or "").split(".")
    if len(parts) < 2:
        return ""
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    for key in ("email", "preferred_username", "upn"):
        email = str(claims.get(key) or "").strip()
        if email:
            return email
    return ""


def save_pool_credential(source_id: str, tokens: dict[str, Any]) -> None:
    expires_in = int(tokens.get("expires_in") or 3600)
    email = _jwt_email(tokens.get("id_token")) or _jwt_email(
        tokens.get("access_token")
    )
    save_pool_entry(
        source_id,
        {
            "key": tokens["access_token"],
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token", ""),
            "id_token": tokens.get("id_token", ""),
            "expires_at": time.time() + expires_in,
            "oidc_issuer": "https://auth.x.ai",
            "oidc_client_id": _oauth_config()[0],
            "source": "grok_sso_device_flow",
            "email": email,
            "updated_at": time.time(),
        },
    )


def refresh_pool_credential(
    source_id: str,
    *,
    proxy: str | None = None,
    timeout_sec: float = 30.0,
) -> dict[str, Any]:
    with pool_entry_refresh_lock(source_id):
        entry = pool_entries().get(source_id)
        if not isinstance(entry, dict):
            raise ValueError("Grok Build OAuth credential not found")
        refresh_token = str(entry.get("refresh_token") or "").strip()
        if not refresh_token:
            raise ValueError("Grok Build OAuth credential has no refresh token")

        client_id, token_url, _scope = _oauth_config()
        with requests.Session() as session:
            _configure_proxy(session, proxy)
            response = session.post(
                token_url,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "refresh_token": refresh_token,
                },
                timeout=max(1.0, float(timeout_sec)),
                impersonate="chrome",
            )
        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError("Grok Build OAuth refresh returned invalid JSON") from exc
        if response.status_code != 200 or not isinstance(payload, dict):
            raise RuntimeError(
                f"Grok Build OAuth refresh failed ({response.status_code})"
            )
        access_token = str(payload.get("access_token") or "").strip()
        if not access_token:
            raise RuntimeError("Grok Build OAuth refresh returned no access token")

        refreshed = dict(entry)
        refreshed["key"] = access_token
        refreshed["access_token"] = access_token
        refreshed["refresh_token"] = payload.get("refresh_token") or refresh_token
        if payload.get("id_token"):
            refreshed["id_token"] = payload["id_token"]
        expires_in = max(1, int(payload.get("expires_in") or 3600))
        refreshed["expires_at"] = time.time() + expires_in
        refreshed["updated_at"] = time.time()
        refreshed["email"] = (
            _jwt_email(refreshed.get("id_token"))
            or _jwt_email(access_token)
            or str(entry.get("email") or "")
        )
        saved = save_pool_entry_if_refresh_token(source_id, refreshed, refresh_token)
    return {
        "source_id": source_id,
        "updated": saved,
        "conflict": not saved,
        "has_refresh_token": bool(refreshed["refresh_token"]),
        "expires_at": refreshed["expires_at"],
    }


def refresh_due_pool_credentials(
    *,
    refresh_before_expiry_s: float = 900.0,
    limit: int = 0,
) -> dict[str, int]:
    now = time.time()
    entries = pool_entries()
    due: list[tuple[float, str]] = []
    for source_id, entry in entries.items():
        expiry = parse_pool_expiry(entry.get("expires_at"))
        if (
            str(entry.get("refresh_token") or "").strip()
            and expiry
            and expiry <= now + max(0.0, float(refresh_before_expiry_s))
        ):
            due.append((expiry, source_id))
    due.sort()
    ready = [
        item
        for item in due
        if _AUTO_REFRESH_RETRY_AFTER.get(item[1], 0.0) <= now
    ]
    selected = ready if limit <= 0 else ready[:limit]
    refreshed = failed = conflicts = 0
    for _expiry, source_id in selected:
        try:
            result = refresh_pool_credential(source_id)
        except Exception as exc:
            failed += 1
            _AUTO_REFRESH_RETRY_AFTER[source_id] = now + 900.0
            _DEFAULT_LOGGER.warning(
                "Grok Build OAuth automatic refresh failed source_id=%s error=%s",
                source_id,
                type(exc).__name__,
            )
        else:
            _AUTO_REFRESH_RETRY_AFTER.pop(source_id, None)
            if result["conflict"]:
                conflicts += 1
            else:
                refreshed += 1
    return {
        "checked": len(entries),
        "eligible": sum(
            1
            for entry in entries.values()
            if str(entry.get("refresh_token") or "").strip()
        ),
        "due": len(due),
        "selected": len(selected),
        "refreshed": refreshed,
        "failed": failed,
        "conflicts": conflicts,
        "deferred": len(due) - len(ready),
        "skipped": max(0, len(ready) - len(selected)),
    }


def authorize_sso_account(
    token: str,
    source_id: str,
    *,
    proxy: str | None = None,
    poll_timeout_sec: float = 90.0,
) -> dict[str, Any]:
    with requests.Session() as session:
        _configure_proxy(session, proxy)
        device = request_device_code(session)
        verification_url = str(
            device.get("verification_uri_complete")
            or device.get("verification_uri")
            or ""
        )
        if not verification_url:
            raise RuntimeError("device authorization returned no verification URL")
        authorize_device_with_sso(
            session,
            token,
            verification_url,
            str(device["user_code"]),
        )
        tokens = poll_device_token(
            session,
            str(device["device_code"]),
            expires_in=min(
                int(device.get("expires_in") or 1800),
                max(30, int(poll_timeout_sec)),
            ),
            interval=int(device.get("interval") or 5),
        )
        save_pool_credential(source_id, tokens)
        return {
            "source_id": source_id,
            "has_refresh_token": bool(tokens.get("refresh_token")),
        }


def source_id_for_sso(token: str) -> str:
    digest = hashlib.sha256(str(token or "").strip().encode("utf-8")).hexdigest()
    return "sso:" + digest[:24]


def authorize_sso_accounts(
    tokens: list[str],
    *,
    delay_sec: float = 0.0,
    required: bool = False,
    proxy: str | None = None,
    poll_timeout_sec: float = 90.0,
    logger: logging.Logger | None = None,
) -> list[dict[str, Any]]:
    """Authorize newly registered SSO tokens sequentially.

    Sequential execution is intentional: every successful authorization updates
    the same credential pool file.
    """
    log = logger or _DEFAULT_LOGGER
    resolved_proxy = _proxy_url(proxy)
    unique_tokens = list(
        dict.fromkeys(str(token).strip() for token in tokens if str(token).strip())
    )
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, token in enumerate(unique_tokens):
        source_id = source_id_for_sso(token)
        try:
            result = authorize_sso_account(
                token,
                source_id,
                proxy=resolved_proxy or None,
                poll_timeout_sec=poll_timeout_sec,
            )
            results.append(result)
            log.info(
                "Grok Build OAuth authorized source_id=%s proxy=%s",
                source_id,
                _proxy_log_label(resolved_proxy),
            )
        except Exception as exc:
            errors.append(f"{source_id}: {type(exc).__name__}")
            log.warning(
                "Grok Build OAuth failed source_id=%s error=%s",
                source_id,
                type(exc).__name__,
            )
        if index + 1 < len(unique_tokens) and delay_sec > 0:
            time.sleep(max(0.0, float(delay_sec)))
    if errors and required:
        raise RuntimeError(f"Grok Build OAuth failed for {len(errors)} account(s)")
    return results


__all__ = [
    "authorize_sso_account",
    "authorize_sso_accounts",
    "authorize_device_with_sso",
    "delete_pool_entry",
    "delete_pool_entries",
    "parse_pool_expiry",
    "poll_device_token",
    "pool_entries",
    "pool_entry_refresh_lock",
    "pool_file_lock",
    "pool_path",
    "read_pool_document",
    "refresh_due_pool_credentials",
    "refresh_pool_credential",
    "request_device_code",
    "save_pool_entry",
    "save_pool_entry_if_refresh_token",
    "save_pool_credential",
    "source_id_for_sso",
]
