"""Manual OAuth + PKCE login for ordinary GPT accounts."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import threading
import time
import uuid
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse


AUTH_BASE = "https://auth.openai.com"
PLATFORM_BASE = "https://platform.openai.com"
PLATFORM_OAUTH_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"
PLATFORM_OAUTH_REDIRECT_URI = f"{PLATFORM_BASE}/auth/callback"
PLATFORM_OAUTH_AUDIENCE = "https://api.openai.com/v1"
PLATFORM_AUTH0_CLIENT = "eyJuYW1lIjoiYXV0aDAtc3BhLWpzIiwidmVyc2lvbiI6IjEuMjEuMCJ9"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)
SEC_CH_UA = '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"'
_OAUTH_PROXY_ENV_KEYS = (
    "MAINTAINER_PROXY",
    "GROK_PROXY_EGRESS_PROXY_URL",
    "HTTPS_PROXY",
    "HTTP_PROXY",
)


class GPTAccountOAuthError(RuntimeError):
    """Expected manual OAuth flow failure surfaced to the admin UI."""


def _generate_pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    return verifier, challenge


class GPTAccountOAuthService:
    """Tracks short-lived PKCE sessions and exchanges callback codes for tokens."""

    _SESSION_TTL_SECONDS = 10 * 60
    _MAX_SESSIONS = 64

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, dict[str, Any]] = {}

    def _purge_expired_locked(self) -> None:
        now = time.time()
        expired = [
            sid
            for sid, item in self._sessions.items()
            if now - float(item.get("created_at") or 0) > self._SESSION_TTL_SECONDS
        ]
        for sid in expired:
            self._sessions.pop(sid, None)
        if len(self._sessions) <= self._MAX_SESSIONS:
            return
        ordered = sorted(self._sessions.items(), key=lambda kv: kv[1]["created_at"])
        for sid, _item in ordered[: len(self._sessions) - self._MAX_SESSIONS]:
            self._sessions.pop(sid, None)

    def start(self, email_hint: str = "") -> dict[str, Any]:
        verifier, challenge = _generate_pkce()
        session_id = uuid.uuid4().hex
        state = f"{session_id}.{secrets.token_urlsafe(16)}"
        params = {
            "issuer": AUTH_BASE,
            "client_id": PLATFORM_OAUTH_CLIENT_ID,
            "audience": PLATFORM_OAUTH_AUDIENCE,
            "redirect_uri": PLATFORM_OAUTH_REDIRECT_URI,
            "device_id": str(uuid.uuid4()),
            "screen_hint": "login_or_signup",
            "max_age": "0",
            "scope": "openid profile email offline_access",
            "response_type": "code",
            "response_mode": "query",
            "state": state,
            "nonce": secrets.token_urlsafe(32),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "auth0Client": PLATFORM_AUTH0_CLIENT,
        }
        email_hint = str(email_hint or "").strip()
        if email_hint:
            params["login_hint"] = email_hint

        with self._lock:
            self._purge_expired_locked()
            self._sessions[session_id] = {
                "code_verifier": verifier,
                "state": state,
                "created_at": time.time(),
                "redirect_uri": PLATFORM_OAUTH_REDIRECT_URI,
            }

        return {
            "session_id": session_id,
            "authorize_url": f"{AUTH_BASE}/api/accounts/authorize?{urlencode(params)}",
            "expires_in": self._SESSION_TTL_SECONDS,
            "redirect_uri_prefix": PLATFORM_OAUTH_REDIRECT_URI,
        }

    @staticmethod
    def extract_code_from_callback(value: str) -> tuple[str, str]:
        raw = str(value or "").strip()
        if not raw:
            return "", ""
        if raw.startswith(("http://", "https://")):
            try:
                parsed = parse_qs(urlparse(raw).query)
            except Exception as exc:
                raise GPTAccountOAuthError(f"Failed to parse callback URL: {exc}") from exc
            code = str((parsed.get("code") or [""])[0]).strip()
            state = str((parsed.get("state") or [""])[0]).strip()
            if not code:
                error = str(
                    (parsed.get("error_description") or parsed.get("error") or [""])[0]
                ).strip()
                raise GPTAccountOAuthError(error or "Callback URL does not contain a code parameter")
            return code, state
        return raw, ""

    @staticmethod
    def proxy_url_from_env() -> str:
        for key in _OAUTH_PROXY_ENV_KEYS:
            value = os.getenv(key, "").strip()
            if value:
                return value
        return ""

    def finish(self, session_id: str, callback: str) -> dict[str, str]:
        body_sid = str(session_id or "").strip()
        code, state = self.extract_code_from_callback(callback)
        if not code:
            raise GPTAccountOAuthError("Missing code or callback URL")

        state_sid = state.split(".", 1)[0] if state else ""
        candidate_sids = [sid for sid in (state_sid, body_sid) if sid]
        if not candidate_sids:
            raise GPTAccountOAuthError("Missing session_id and callback URL state")

        with self._lock:
            self._purge_expired_locked()
            session = None
            picked_sid = ""
            for sid in candidate_sids:
                session = self._sessions.get(sid)
                if session is not None:
                    picked_sid = sid
                    break

        if session is None:
            raise GPTAccountOAuthError("OAuth session is expired or missing; generate a new authorize URL")
        if state and session.get("state") and state != session["state"]:
            raise GPTAccountOAuthError("OAuth state does not match; generate a new authorize URL")

        tokens = self._exchange_code(
            code,
            str(session["code_verifier"]),
            str(session.get("redirect_uri") or PLATFORM_OAUTH_REDIRECT_URI),
        )
        with self._lock:
            self._sessions.pop(picked_sid, None)
        return tokens

    @staticmethod
    def _exchange_code(code: str, code_verifier: str, redirect_uri: str) -> dict[str, str]:
        try:
            from curl_cffi import requests

            session = requests.Session(impersonate="chrome", verify=False)
            proxy_url = GPTAccountOAuthService.proxy_url_from_env()
            if proxy_url:
                session.proxies = {"http": proxy_url, "https": proxy_url}
        except Exception as exc:
            raise GPTAccountOAuthError(f"Failed to initialize OAuth HTTP client: {exc}") from exc

        try:
            response = session.post(
                f"{AUTH_BASE}/api/accounts/oauth/token",
                headers={
                    "accept": "*/*",
                    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "auth0-client": PLATFORM_AUTH0_CLIENT,
                    "cache-control": "no-cache",
                    "content-type": "application/json",
                    "origin": PLATFORM_BASE,
                    "pragma": "no-cache",
                    "referer": f"{PLATFORM_BASE}/",
                    "sec-ch-ua": SEC_CH_UA,
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                    "sec-fetch-dest": "empty",
                    "sec-fetch-mode": "cors",
                    "sec-fetch-site": "same-site",
                    "user-agent": USER_AGENT,
                },
                json={
                    "client_id": PLATFORM_OAUTH_CLIENT_ID,
                    "code_verifier": code_verifier,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                timeout=60,
            )
        except Exception as exc:
            raise GPTAccountOAuthError(f"Token exchange network error: {exc}") from exc
        finally:
            session.close()

        try:
            data = response.json() if response.text else {}
        except Exception:
            data = {}

        if response.status_code != 200 or not isinstance(data, dict):
            detail = ""
            if isinstance(data, dict):
                detail = str(
                    data.get("error_description")
                    or data.get("error")
                    or data.get("message")
                    or ""
                )
            if not detail:
                detail = str(getattr(response, "text", "") or "")[:300]
            raise GPTAccountOAuthError(
                f"OpenAI rejected token exchange (HTTP {response.status_code})"
                f"{': ' + detail if detail else ''}"
            )

        access_token = str(data.get("access_token") or "").strip()
        if not access_token:
            raise GPTAccountOAuthError("OpenAI returned an empty access_token")

        return {
            "access_token": access_token,
            "refresh_token": str(data.get("refresh_token") or "").strip(),
            "id_token": str(data.get("id_token") or "").strip(),
        }


gpt_oauth_login_service = GPTAccountOAuthService()


__all__ = [
    "GPTAccountOAuthError",
    "GPTAccountOAuthService",
    "gpt_oauth_login_service",
]
