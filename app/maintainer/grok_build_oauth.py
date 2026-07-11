"""Automate Grok Build device authorization from existing Grok SSO tokens."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from curl_cffi import requests

from app.platform.config.snapshot import get_config

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


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
        payload = response.json()
        if response.status_code == 200 and payload.get("access_token"):
            return payload
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
        raise RuntimeError(f"device approval failed: {approve.url}")


def _pool_path() -> Path:
    configured = get_config().get_str("grok_build.auth_file", "data/grok_auth.json")
    path = Path(configured).expanduser()
    return path if path.is_absolute() else Path.cwd() / path


def save_pool_credential(source_id: str, tokens: dict[str, Any]) -> None:
    path = _pool_path()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        document = {}
    except (OSError, json.JSONDecodeError):
        document = {}
    if not isinstance(document, dict):
        document = {}
    expires_in = int(tokens.get("expires_in") or 3600)
    document[source_id] = {
        "key": tokens["access_token"],
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token", ""),
        "id_token": tokens.get("id_token", ""),
        "expires_at": time.time() + expires_in,
        "oidc_issuer": "https://auth.x.ai",
        "oidc_client_id": _oauth_config()[0],
        "source": "grok_sso_device_flow",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(document, ensure_ascii=True, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def authorize_sso_account(token: str, source_id: str) -> dict[str, Any]:
    with requests.Session() as session:
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
            expires_in=int(device.get("expires_in") or 1800),
            interval=int(device.get("interval") or 5),
        )
        save_pool_credential(source_id, tokens)
        return {
            "source_id": source_id,
            "has_refresh_token": bool(tokens.get("refresh_token")),
        }


__all__ = [
    "authorize_sso_account",
    "authorize_device_with_sso",
    "poll_device_token",
    "request_device_code",
    "save_pool_credential",
]
