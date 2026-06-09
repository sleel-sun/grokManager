from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.platform.auth import middleware as auth_middleware
from app.products.web.webui import mcp as mcp_module


def _basic(username: str, password: str) -> str:
    raw = f"{username}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _query_basic(username: str, password: str) -> str:
    raw = base64.urlsafe_b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return "basic:" + raw.rstrip("=")


def _configure_auth(monkeypatch: pytest.MonkeyPatch, values: dict[str, object]) -> None:
    def fake_get_config(key: str, default=None):
        return values.get(key, default)

    monkeypatch.setattr(auth_middleware, "get_config", fake_get_config)


def test_webui_users_require_username_password_and_return_stable_user(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_auth(
        monkeypatch,
        {
            "app.webui_enabled": True,
            "app.webui_key": "legacy-secret",
            "app.webui_users": [
                {"username": "alice", "password": "alice-secret", "display_name": "Alice"},
                {"username": "bob", "password": "bob-secret", "enabled": False},
            ],
        },
    )

    user = asyncio.run(auth_middleware.verify_webui_key(authorization=_basic("alice", "alice-secret")))
    assert user.username == "alice"
    assert user.display_name == "Alice"
    assert user.id == auth_middleware._webui_user_id("alice")
    assert user.public_dict() == {
        "id": auth_middleware._webui_user_id("alice"),
        "username": "alice",
        "display_name": "Alice",
        "legacy": False,
        "anonymous": False,
        "storage_scope": auth_middleware._webui_user_id("alice"),
    }

    legacy = asyncio.run(auth_middleware.verify_webui_key(authorization="Bearer legacy-secret"))
    assert legacy.legacy is True
    assert legacy.id == "legacy"

    for authorization in (
        "Bearer alice-secret",
        _basic("alice", "wrong"),
        _basic("bob", "bob-secret"),
    ):
        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(auth_middleware.verify_webui_key(authorization=authorization))
        assert excinfo.value.status_code == 401


def test_webui_users_accept_json_and_line_config_for_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_auth(
        monkeypatch,
        {
            "app.webui_enabled": True,
            "app.webui_users": json.dumps(
                [{"username": "carol", "password": "carol-secret", "displayName": "Carol"}]
            ),
        },
    )
    assert auth_middleware.authenticate_webui_authorization(_basic("carol", "carol-secret")).username == "carol"

    _configure_auth(
        monkeypatch,
        {
            "app.webui_enabled": True,
            "app.webui_users": "dave=dave-secret\n# ignored\n",
        },
    )
    assert auth_middleware.authenticate_webui_authorization(_basic("dave", "dave-secret")).username == "dave"


def test_webui_legacy_single_key_and_anonymous_modes_stay_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_auth(
        monkeypatch,
        {
            "app.webui_enabled": True,
            "app.webui_key": "legacy-secret",
            "app.webui_users": [],
        },
    )
    legacy = asyncio.run(auth_middleware.verify_webui_key(authorization="Bearer legacy-secret"))
    assert legacy.legacy is True
    assert legacy.id == "legacy"

    _configure_auth(
        monkeypatch,
        {
            "app.webui_enabled": True,
            "app.webui_key": "",
            "app.webui_users": [],
        },
    )
    anonymous = asyncio.run(auth_middleware.verify_webui_key(authorization=None))
    assert anonymous.anonymous is True
    assert anonymous.id == "anonymous"


def test_webui_query_token_supports_basic_credentials_for_websockets(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_auth(
        monkeypatch,
        {
            "app.webui_enabled": True,
            "app.webui_users": [{"username": "alice", "password": "alice-secret"}],
        },
    )

    assert auth_middleware.authenticate_webui_token(_query_basic("alice", "alice-secret")).username == "alice"
    assert auth_middleware.authenticate_webui_token("alice-secret") is None


def test_webui_session_cookie_authenticates_current_user(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_auth(
        monkeypatch,
        {
            "app.app_key": "admin-secret",
            "app.webui_enabled": True,
            "app.webui_users": [{"username": "alice", "password": "alice-secret", "display_name": "Alice"}],
        },
    )

    user = auth_middleware.authenticate_webui_authorization(_basic("alice", "alice-secret"))
    assert user is not None
    cookie = auth_middleware.build_webui_session_cookie(user)
    assert cookie

    restored = auth_middleware.authenticate_webui_session_cookie(cookie)
    assert restored is not None
    assert restored.username == "alice"
    assert restored.id == user.id

    _configure_auth(
        monkeypatch,
        {
            "app.app_key": "admin-secret",
            "app.webui_enabled": True,
            "app.webui_users": [{"username": "alice", "password": "changed-secret", "display_name": "Alice"}],
        },
    )
    assert auth_middleware.authenticate_webui_session_cookie(cookie) is None


def test_webui_explicit_auth_does_not_fall_back_to_session_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_auth(
        monkeypatch,
        {
            "app.app_key": "admin-secret",
            "app.webui_enabled": True,
            "app.webui_users": [{"username": "alice", "password": "alice-secret", "display_name": "Alice"}],
        },
    )

    user = auth_middleware.authenticate_webui_authorization(_basic("alice", "alice-secret"))
    assert user is not None
    cookie = auth_middleware.build_webui_session_cookie(user)
    assert cookie

    restored = asyncio.run(auth_middleware.verify_webui_key(webui_session=cookie))
    assert restored.username == "alice"

    with pytest.raises(HTTPException) as bad_auth_excinfo:
        asyncio.run(auth_middleware.verify_webui_key(authorization=_basic("alice", "wrong"), webui_session=cookie))
    assert bad_auth_excinfo.value.status_code == 401

    with pytest.raises(HTTPException) as auth_only_excinfo:
        asyncio.run(auth_middleware.verify_webui_key(x_webui_auth_only="1", webui_session=cookie))
    assert auth_only_excinfo.value.status_code == 401


def test_webui_logout_cookie_blocks_auto_session_restore(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_auth(
        monkeypatch,
        {
            "app.app_key": "admin-secret",
            "app.webui_enabled": True,
            "app.webui_users": [{"username": "alice", "password": "alice-secret", "display_name": "Alice"}],
        },
    )

    user = auth_middleware.authenticate_webui_authorization(_basic("alice", "alice-secret"))
    assert user is not None
    cookie = auth_middleware.build_webui_session_cookie(user)
    assert cookie

    for kwargs in (
        {"webui_session": cookie},
        {"authorization": _basic("alice", "alice-secret")},
    ):
        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(
                auth_middleware.verify_webui_key(
                    x_webui_auth_only="1",
                    webui_logged_out="1",
                    **kwargs,
                )
            )
        assert excinfo.value.status_code == 401

    restored = asyncio.run(
        auth_middleware.verify_webui_key(
            authorization=_basic("alice", "alice-secret"),
            x_webui_auth_only="1",
            x_webui_login_intent="1",
            webui_logged_out="1",
        )
    )
    assert restored.username == "alice"


def test_webui_logout_route_clears_http_only_session_cookie() -> None:
    root = Path(__file__).resolve().parent.parent
    router_py = (root / "app" / "products" / "web" / "router.py").read_text(encoding="utf-8")
    auth_js = (root / "app" / "statics" / "js" / "auth.js").read_text(encoding="utf-8")

    assert '@router.get("/webui/logout", include_in_schema=False)' in router_py
    assert 'RedirectResponse("/webui/login?logout=1")' in router_py
    assert "clear_webui_session_cookie(response)" in router_py
    assert "set_webui_logout_cookie(response)" in router_py
    assert "clear_webui_logout_cookie(response)" in router_py
    assert "function webuiLogout() { webuiAuth.clear(); webuiMarkLoggedOut(); location.href='/webui/logout'; }" in auth_js
    assert "function webuiLogout() { webuiAuth.clear(); location.href='/webui/login'; }" not in auth_js


def test_mcp_server_store_is_isolated_by_webui_user(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(mcp_module, "_STORE_PATH", tmp_path / "webui" / "mcp_servers.json")
    monkeypatch.setattr(mcp_module, "data_path", lambda *parts: tmp_path.joinpath(*parts))

    alice = auth_middleware.WebUIUser(
        id=auth_middleware._webui_user_id("alice"),
        username="alice",
        display_name="Alice",
    )
    bob = auth_middleware.WebUIUser(
        id=auth_middleware._webui_user_id("bob"),
        username="bob",
        display_name="Bob",
    )
    legacy = auth_middleware.WebUIUser(id="legacy", username="legacy", legacy=True)

    alice_servers = [{"id": "alice-server", "name": "Alice MCP", "command": "python", "enabled": True}]
    bob_servers = [{"id": "bob-server", "name": "Bob MCP", "command": "node", "enabled": True}]
    legacy_servers = [{"id": "legacy-server", "name": "Legacy MCP", "command": "bash", "enabled": True}]

    alice_path = mcp_module._store_path_for_user(alice)
    bob_path = mcp_module._store_path_for_user(bob)
    legacy_path = mcp_module._store_path_for_user(legacy)

    mcp_module._write_store_sync(alice_servers, alice_path)
    mcp_module._write_store_sync(bob_servers, bob_path)
    mcp_module._write_store_sync(legacy_servers, legacy_path)

    assert [item["id"] for item in mcp_module._read_store_sync(alice_path)] == ["alice-server"]
    assert [item["id"] for item in mcp_module._read_store_sync(bob_path)] == ["bob-server"]
    assert [item["id"] for item in mcp_module._read_store_sync(legacy_path)] == ["legacy-server"]

    assert alice_path == tmp_path / "webui" / "users" / alice.id / "mcp_servers.json"
    assert bob_path == tmp_path / "webui" / "users" / bob.id / "mcp_servers.json"
    assert legacy_path == tmp_path / "webui" / "mcp_servers.json"


def test_webui_frontend_uses_scoped_storage_keys() -> None:
    root = Path(__file__).resolve().parent.parent
    auth_js = (root / "app" / "statics" / "js" / "auth.js").read_text(encoding="utf-8")
    chat_js = (root / "app" / "statics" / "js" / "webui" / "chat.js").read_text(encoding="utf-8")

    assert "async function webuiScopedStorageKey(baseKey)" in auth_js
    assert "return `${baseKey}.${webuiStorageScopeSuffix(await webuiStorageScope())}`;" in auth_js
    assert "const hasUserIdentity = Boolean(rawUser.id || rawUser.username || rawStorageScope || auth.user_id);" in auth_js
    assert "anonymous: Boolean(rawUser.anonymous || (!hasUserIdentity && !username && !password))" in auth_js
    assert "return auth.storage_scope || (auth.user && (auth.user.storage_scope || auth.user.storageScope || auth.user.id)) || 'anonymous';" in auth_js
    assert "await initStorageScope();" in chat_js
    assert "let currentStorageScope = 'anonymous';" in chat_js
    assert "let requireSessionStorageScope = false;" in chat_js
    assert "storeKey = await webuiScopedStorageKey(STORE_KEY);" in chat_js
    assert "sidebarStoreKey = await webuiScopedStorageKey(SIDEBAR_STORE_KEY);" in chat_js
    assert "mcpSettingsKey = await webuiScopedStorageKey(MCP_SETTINGS_KEY);" in chat_js
    assert "searchSettingsKey = await webuiScopedStorageKey(SEARCH_SETTINGS_KEY);" in chat_js
    assert "const shouldMigrateLegacy = Boolean(auth.user && (auth.user.legacy || auth.user.anonymous));" in chat_js
    assert "currentStorageScope = webuiStorageScopeSuffix(await webuiStorageScope());" in chat_js
    assert "requireSessionStorageScope = !shouldMigrateLegacy;" in chat_js
    assert "const scopedSessions = rawSessions.filter(sessionMatchesStorageScope);" in chat_js
    assert "storageScope: currentStorageScope" in chat_js
    assert "function sessionMatchesStorageScope(item)" in chat_js
    assert "if (!rawScope) return false;" in chat_js


def test_webui_login_page_uses_legacy_browser_safe_auth_script() -> None:
    root = Path(__file__).resolve().parent.parent
    html = (root / "app" / "statics" / "webui" / "login.html").read_text(encoding="utf-8")
    auth_js = (root / "app" / "statics" / "js" / "auth.js").read_text(encoding="utf-8")

    assert "renderSiteFooter?.()" not in html
    assert "/static/js/auth.js?v={{APP_VERSION}}-logoutfix5" in html
    assert "if (params.get('logout') === '1') webuiMarkLoggedOut();" in html
    assert "skipAutoLogin = webuiWasLoggedOut();" in html
    assert "verifyWebuiAccess(VERIFY, username, password, { authOnly: true, loginIntent: true })" in html
    assert "if (!skipAutoLogin && await verifyStoredWebuiAccess(VERIFY, { authOnly: true }))" in html
    assert "crypto?.subtle" not in auth_js
    assert "function _cryptoSubtle()" in auth_js
    assert "'X-WebUI-Auth-Only': '1'" in auth_js
    assert "'X-WebUI-Login-Intent': '1'" in auth_js


def test_webui_pages_use_same_auth_cache_buster() -> None:
    root = Path(__file__).resolve().parent.parent
    expected = "/static/js/auth.js?v={{APP_VERSION}}-logoutfix5"
    for name in ("login.html", "chat.html", "masonry.html", "chatkit.html"):
        html = (root / "app" / "statics" / "webui" / name).read_text(encoding="utf-8")
        assert expected in html, f"{name} must not load a stale auth.js cache key"

    chat_html = (root / "app" / "statics" / "webui" / "chat.html").read_text(encoding="utf-8")
    assert "/static/js/webui/chat.js?v={{APP_VERSION}}-isolate1" in chat_html


def test_webui_multi_user_i18n_keys_exist_for_all_locales() -> None:
    root = Path(__file__).resolve().parent.parent
    for path in sorted((root / "app" / "statics" / "i18n").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["login"]["webuiUsernamePlaceholder"]
        assert data["config"]["schema"]["fields"]["webuiUsers"]["label"]
        assert data["config"]["schema"]["fields"]["webuiUsers"]["desc"]
        users = data["config"]["webuiUsers"]
        for key in ("add", "empty", "username", "password", "displayName", "enabled", "remove"):
            assert users[key], f"{path.name} missing config.webuiUsers.{key}"
