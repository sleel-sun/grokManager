"""API-key authentication dependencies for FastAPI routes."""

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass

from fastapi import Cookie, Header, HTTPException, Query, Response, status
from fastapi.security import HTTPBearer

from app.platform.config.snapshot import get_config

_security = HTTPBearer(auto_error=False, scheme_name="API Key")
WEBUI_SESSION_COOKIE = "grokmanager_webui_session"
WEBUI_LOGOUT_COOKIE = "grokmanager_webui_logged_out"
_WEBUI_SESSION_MAX_AGE = 7 * 24 * 60 * 60
_WEBUI_LOGOUT_MAX_AGE = 7 * 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class WebUIUser:
    id: str
    username: str
    display_name: str = ""
    allow_nsfw: bool = True
    legacy: bool = False
    anonymous: bool = False

    def public_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name or self.username,
            "allow_nsfw": webui_user_allows_nsfw(self),
            "legacy": self.legacy,
            "anonymous": self.anonymous,
            "storage_scope": self.id,
        }


@dataclass(frozen=True, slots=True)
class _WebUIUserCredential:
    user: WebUIUser
    password: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_keys() -> list[str]:
    raw = get_config("app.api_key", "")
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(k).strip() for k in raw if str(k).strip()]
    return [k.strip() for k in str(raw).split(",") if k.strip()]


def get_admin_key() -> str:
    """Return configured ``app.app_key`` (admin password)."""
    return str(get_config("app.app_key", "grok2api") or "")


def get_webui_key() -> str:
    """Return configured ``app.webui_key`` (webui access key)."""
    return str(get_config("app.webui_key", "") or "")


def _webui_user_id(username: str) -> str:
    normalized = str(username or "").strip() or "user"
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", normalized.lower()).strip("-_")[:32] or "user"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10]
    return f"{slug}-{digest}"


def _parse_webui_user_lines(raw: str) -> list[object]:
    entries: list[object] = []
    for line in raw.splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        username, sep, password = text.partition("=")
        if not sep:
            username, sep, password = text.partition(":")
        if sep and username.strip() and password.strip():
            entries.append({"username": username.strip(), "password": password.strip()})
    return entries


def _raw_webui_users() -> object:
    raw = get_config("app.webui_users", [])
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text[0] in "[{":
            try:
                return json.loads(text)
            except (json.JSONDecodeError, TypeError):
                return []
        return _parse_webui_user_lines(text)
    return raw


def _iter_webui_user_entries(raw: object) -> list[object]:
    if isinstance(raw, dict):
        for key in ("users", "webui_users"):
            value = raw.get(key)
            if isinstance(value, list):
                return value
        return [{"username": username, "password": password} for username, password in raw.items()]
    if isinstance(raw, list):
        return raw
    return []


def _webui_user_credentials() -> list[_WebUIUserCredential]:
    credentials: list[_WebUIUserCredential] = []
    seen: set[str] = set()
    for entry in _iter_webui_user_entries(_raw_webui_users()):
        if isinstance(entry, str):
            parsed = _parse_webui_user_lines(entry)
            if not parsed:
                continue
            entry = parsed[0]
        if not isinstance(entry, dict):
            continue
        enabled = entry.get("enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() not in {"0", "false", "no", "off", "disabled"}
        if not enabled:
            continue
        username = str(
            entry.get("username")
            or entry.get("name")
            or entry.get("id")
            or ""
        ).strip()
        password = str(
            entry.get("password")
            or entry.get("key")
            or entry.get("token")
            or ""
        ).strip()
        if not username or not password or username in seen:
            continue
        seen.add(username)
        display_name = str(entry.get("display_name") or entry.get("displayName") or username).strip()
        allow_nsfw = _bool_config_value(
            entry.get(
                "allow_nsfw",
                entry.get("allowNsfw", entry.get("nsfw", entry.get("enable_nsfw"))),
            ),
            True,
        )
        credentials.append(
            _WebUIUserCredential(
                user=WebUIUser(
                    id=_webui_user_id(username),
                    username=username,
                    display_name=display_name,
                    allow_nsfw=allow_nsfw,
                ),
                password=password,
            )
        )
    return credentials


def is_webui_enabled() -> bool:
    """Whether the webui entry is enabled."""
    val = get_config("app.webui_enabled", False)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in {"1", "true", "yes", "on"}
    return bool(val)


def _extract_bearer(authorization: str | None) -> str | None:
    if not isinstance(authorization, str):
        return None
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def _constant_time_equal(value: str, expected: str) -> bool:
    return hmac.compare_digest(value.encode("utf-8"), expected.encode("utf-8"))


def _truthy_header(value: str | None) -> bool:
    if not isinstance(value, str):
        return False
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


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


def _extract_basic(authorization: str | None) -> tuple[str, str] | None:
    if not isinstance(authorization, str):
        return None
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "basic" or not token:
        return None
    try:
        decoded = base64.b64decode(token.strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    username, sep, password = decoded.partition(":")
    if not sep:
        return None
    return username, password


def _extract_webui_query_token(token: str | None) -> tuple[str, str] | str | None:
    raw = str(token or "").strip()
    if not raw:
        return None
    if raw.startswith("basic:"):
        encoded = raw[len("basic:") :]
        try:
            decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        username, sep, password = decoded.partition(":")
        if not sep:
            return None
        return username, password
    return raw


def _legacy_webui_user() -> WebUIUser:
    return WebUIUser(
        id="legacy",
        username="legacy",
        display_name="WebUI",
        legacy=True,
    )


def _anonymous_webui_user() -> WebUIUser:
    return WebUIUser(
        id="anonymous",
        username="anonymous",
        display_name="Anonymous",
        anonymous=True,
    )


def _webui_session_secret() -> bytes:
    material = (
        get_admin_key()
        or ",".join(_get_keys())
        or get_webui_key()
        or "grokmanager-webui-session"
    )
    return hashlib.sha256(f"webui-session:{material}".encode("utf-8")).digest()


def _b64url_json(data: dict[str, object]) -> str:
    raw = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64url_json(data: str) -> dict[str, object] | None:
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _webui_session_signature(payload: str) -> str:
    return hmac.new(_webui_session_secret(), payload.encode("ascii"), hashlib.sha256).hexdigest()


def _webui_credential_proof(kind: str, username: str, password: str) -> str:
    raw = f"{kind}\0{username}\0{password}".encode("utf-8")
    return hmac.new(_webui_session_secret(), raw, hashlib.sha256).hexdigest()


def _webui_session_payload_for_user(user: WebUIUser) -> dict[str, object] | None:
    if user.anonymous:
        return {
            "v": 1,
            "kind": "anonymous",
            "id": user.id,
            "username": user.username,
            "proof": _webui_credential_proof("anonymous", user.username, ""),
        }
    if user.legacy:
        password = get_webui_key()
        if not password:
            return None
        return {
            "v": 1,
            "kind": "legacy",
            "id": user.id,
            "username": user.username,
            "proof": _webui_credential_proof("legacy", user.username, password),
        }
    for item in _webui_user_credentials():
        if _constant_time_equal(user.username, item.user.username):
            return {
                "v": 1,
                "kind": "basic",
                "id": item.user.id,
                "username": item.user.username,
                "proof": _webui_credential_proof("basic", item.user.username, item.password),
            }
    return None


def build_webui_session_cookie(user: WebUIUser) -> str | None:
    """Return a signed session cookie value for an authenticated WebUI user."""
    payload = _webui_session_payload_for_user(user)
    if not payload:
        return None
    encoded = _b64url_json(payload)
    return f"{encoded}.{_webui_session_signature(encoded)}"


def set_webui_session_cookie(response: Response, user: WebUIUser) -> None:
    token = build_webui_session_cookie(user)
    if not token:
        return
    response.set_cookie(
        WEBUI_SESSION_COOKIE,
        token,
        max_age=_WEBUI_SESSION_MAX_AGE,
        path="/",
        httponly=True,
        samesite="lax",
    )


def clear_webui_session_cookie(response: Response) -> None:
    """Remove the HttpOnly WebUI session cookie."""
    response.delete_cookie(
        WEBUI_SESSION_COOKIE,
        path="/",
        httponly=True,
        samesite="lax",
    )


def set_webui_logout_cookie(response: Response) -> None:
    """Mark this browser as explicitly logged out of WebUI."""
    response.set_cookie(
        WEBUI_LOGOUT_COOKIE,
        "1",
        max_age=_WEBUI_LOGOUT_MAX_AGE,
        path="/",
        httponly=True,
        samesite="lax",
    )


def clear_webui_logout_cookie(response: Response) -> None:
    """Remove the WebUI explicit logout marker."""
    response.delete_cookie(
        WEBUI_LOGOUT_COOKIE,
        path="/",
        httponly=True,
        samesite="lax",
    )


def authenticate_webui_session_cookie(value: str | None) -> WebUIUser | None:
    """Authenticate a signed WebUI session cookie against the current config."""
    raw = str(value or "").strip()
    if not raw or "." not in raw or not is_webui_enabled():
        return None
    encoded, _, signature = raw.partition(".")
    if not encoded or not signature:
        return None
    if not hmac.compare_digest(signature, _webui_session_signature(encoded)):
        return None
    payload = _unb64url_json(encoded)
    if not payload or payload.get("v") != 1:
        return None
    kind = str(payload.get("kind") or "")
    username = str(payload.get("username") or "")
    proof = str(payload.get("proof") or "")

    if kind == "anonymous":
        if _webui_user_credentials() or get_webui_key():
            return None
        expected = _webui_credential_proof("anonymous", username or "anonymous", "")
        return _anonymous_webui_user() if hmac.compare_digest(proof, expected) else None

    if kind == "legacy":
        password = get_webui_key()
        expected = _webui_credential_proof("legacy", username or "legacy", password)
        return _legacy_webui_user() if password and hmac.compare_digest(proof, expected) else None

    if kind == "basic":
        for item in _webui_user_credentials():
            if not _constant_time_equal(username, item.user.username):
                continue
            expected = _webui_credential_proof("basic", item.user.username, item.password)
            return item.user if hmac.compare_digest(proof, expected) else None
    return None


def authenticate_webui_credentials(
    *,
    username: str | None = None,
    password: str | None = None,
    bearer_token: str | None = None,
) -> WebUIUser | None:
    """Return the authenticated WebUI user, preserving legacy single-key mode."""
    if not is_webui_enabled():
        return None

    users = _webui_user_credentials()
    webui_key = get_webui_key()
    if users:
        if username is not None and password is not None:
            for item in users:
                if _constant_time_equal(username, item.user.username) and _constant_time_equal(password, item.password):
                    return item.user
        token = str(bearer_token or password or "")
        if not str(username or "").strip() and webui_key and token and _constant_time_equal(token, webui_key):
            return _legacy_webui_user()
        return None

    if not webui_key:
        return _anonymous_webui_user()

    token = str(bearer_token or password or "")
    if token and _constant_time_equal(token, webui_key):
        return _legacy_webui_user()
    return None


def authenticate_webui_authorization(authorization: str | None) -> WebUIUser | None:
    basic = _extract_basic(authorization)
    if basic is not None:
        username, password = basic
        return authenticate_webui_credentials(username=username, password=password)
    return authenticate_webui_credentials(bearer_token=_extract_bearer(authorization))


def authenticate_webui_token(token: str | None) -> WebUIUser | None:
    parsed = _extract_webui_query_token(token)
    if parsed is None:
        return authenticate_webui_credentials(bearer_token="")
    if isinstance(parsed, tuple):
        username, password = parsed
        return authenticate_webui_credentials(username=username, password=password)
    return authenticate_webui_credentials(bearer_token=parsed)


def webui_user_allows_nsfw(
    user: WebUIUser | None,
    *,
    global_enabled: bool | None = None,
) -> bool:
    """Return whether a WebUI user may request NSFW image generation."""
    if global_enabled is None:
        global_enabled = _bool_config_value(get_config("features.enable_nsfw", True), True)
    if not global_enabled:
        return False
    if user is not None and not user.allow_nsfw:
        return False
    return True


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

async def verify_api_key(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
) -> None:
    """Validate Bearer token against configured ``api_key``.

    Accepts either ``Authorization: Bearer <key>`` (OpenAI / grok2api style)
    or ``X-API-Key: <key>`` (official Anthropic SDK style) so that agents
    targeting the Anthropic-compatible endpoint work without reconfiguration.
    """
    allowed_keys = _get_keys()
    if not allowed_keys:
        return

    token = _extract_bearer(authorization) or x_api_key or None
    if token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid Authorization header.")

    if not any(hmac.compare_digest(token, k) for k in allowed_keys):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid API key.")


async def verify_admin_key(
    authorization: str | None = Header(default=None),
    app_key: str | None = Query(default=None),
) -> None:
    """Validate Bearer token against ``app.app_key`` (admin access).

    Accepts either ``Authorization: Bearer <key>`` header or ``?app_key=<key>``
    query parameter (the latter is needed for EventSource which cannot send headers).
    """
    key = get_admin_key()
    if not key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Admin key is not configured.")

    token = _extract_bearer(authorization) or app_key
    if token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing authentication token.")

    if not hmac.compare_digest(token, key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid authentication token.")


async def verify_webui_key(
    authorization: str | None = Header(default=None),
    x_webui_auth_only: str | None = Header(default=None, alias="x-webui-auth-only"),
    x_webui_login_intent: str | None = Header(default=None, alias="x-webui-login-intent"),
    webui_session: str | None = Cookie(default=None, alias=WEBUI_SESSION_COOKIE),
    webui_logged_out: str | None = Cookie(default=None, alias=WEBUI_LOGOUT_COOKIE),
) -> WebUIUser:
    """Validate Bearer token for webui endpoints."""
    if not is_webui_enabled():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "WebUI access is disabled.")

    if _truthy_header(webui_logged_out) and not _truthy_header(x_webui_login_intent):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "WebUI session was logged out.")

    user = authenticate_webui_authorization(authorization)
    if user is not None:
        return user

    if isinstance(authorization, str) and authorization:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid authentication token.")

    if _truthy_header(x_webui_auth_only):
        if not _webui_user_credentials() and not get_webui_key():
            return _anonymous_webui_user()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing authentication token.")

    user = authenticate_webui_session_cookie(webui_session)
    if user is not None:
        return user

    if _webui_user_credentials() or get_webui_key():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing authentication token.")
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "WebUI access is disabled.")

__all__ = [
    "verify_api_key",
    "verify_admin_key",
    "verify_webui_key",
    "get_admin_key",
    "get_webui_key",
    "is_webui_enabled",
    "authenticate_webui_authorization",
    "authenticate_webui_credentials",
    "authenticate_webui_session_cookie",
    "authenticate_webui_token",
    "build_webui_session_cookie",
    "clear_webui_logout_cookie",
    "clear_webui_session_cookie",
    "webui_user_allows_nsfw",
    "set_webui_logout_cookie",
    "set_webui_session_cookie",
    "WEBUI_LOGOUT_COOKIE",
    "WEBUI_SESSION_COOKIE",
    "WebUIUser",
]
