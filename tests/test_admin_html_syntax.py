"""Static syntax checks for admin HTML inline scripts.

Catches regressions like duplicate `const` declarations that abort the
inline `<script>` and silently break the form submit handler / polling.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC_ADMIN_DIR = Path(__file__).resolve().parent.parent / "app" / "statics" / "admin"


def _extract_inline_scripts(html: str) -> list[str]:
    """Return inline `<script>` bodies that do NOT have a src= attribute."""
    pattern = re.compile(
        r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>",
        re.DOTALL | re.IGNORECASE,
    )
    scripts: list[str] = []
    for match in pattern.finditer(html):
        attrs = match.group("attrs") or ""
        if " src=" in attrs or "\tsrc=" in attrs:
            continue
        body = match.group("body").strip()
        if not body:
            continue
        scripts.append(body)
    return scripts


def _node_available() -> bool:
    return shutil.which("node") is not None


def _node_syntax_check(body: str) -> tuple[bool, str]:
    """Run `node --check` on the script body. Returns (ok, message)."""
    result = subprocess.run(
        ["node", "--check", "-"],
        input=body,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        return True, ""
    return False, (result.stderr or result.stdout).strip()


@pytest.mark.skipif(not _node_available(), reason="node binary not installed")
@pytest.mark.parametrize("html_file", sorted(STATIC_ADMIN_DIR.glob("*.html")))
def test_admin_html_inline_scripts_have_no_syntax_errors(html_file: Path) -> None:
    """Every inline <script> in an admin HTML file must parse cleanly under node.

    A SyntaxError in an inline <script> silently aborts the entire script
    in the browser, which has historically broken the form submit handler
    and the status-polling loop (e.g. duplicate `const running` in
    renderStatus). Catch these at CI time instead of in production.
    """
    html = html_file.read_text(encoding="utf-8")
    scripts = _extract_inline_scripts(html)
    if not scripts:
        pytest.skip(f"no inline <script> in {html_file.name}")
    for idx, body in enumerate(scripts):
        ok, message = _node_syntax_check(body)
        assert ok, (
            f"{html_file.name} inline <script> #{idx} has a syntax error:\n{message}\n"
            f"This will silently abort the entire inline script in the browser, "
            f"breaking form handlers and polling. Common causes: duplicate `const`/`let` "
            f"declarations in the same scope, stray top-level `return`, or mismatched braces."
        )


def test_maintainer_page_uses_absolute_admin_api_prefix() -> None:
    """Pause/resume/stop must never resolve to relative ``api/maintainer/...``."""
    html = (STATIC_ADMIN_DIR / "maintainer.html").read_text(encoding="utf-8")

    assert "const MAINTAINER_ADMIN_API = '/admin/api';" in html
    assert "fetch(MAINTAINER_ADMIN_API + path" in html
    assert "verifyKey(MAINTAINER_ADMIN_API + '/verify'" in html


def test_config_uses_multi_user_toggle_instead_of_inline_user_editor() -> None:
    """Config should expose only the WebUI password and multi-user toggle."""
    html = (STATIC_ADMIN_DIR / "config.html").read_text(encoding="utf-8")

    assert "key: 'webui_key'" in html
    assert "key: 'webui_multi_user_enabled'" in html
    assert "config.schema.fields.webuiMultiUser.label" in html
    assert "key: 'webui_users'" not in html
    assert "multiple: 'multiple'" not in html
    assert "selectedOptions" not in html


def test_webui_user_management_page_is_registered() -> None:
    """The dedicated WebUI user manager should have its own page and API."""
    router_py = (STATIC_ADMIN_DIR.parent.parent / "products" / "web" / "router.py").read_text(encoding="utf-8")
    admin_init = (
        STATIC_ADMIN_DIR.parent.parent / "products" / "web" / "admin" / "__init__.py"
    ).read_text(encoding="utf-8")
    header = (STATIC_ADMIN_DIR / "header.html").read_text(encoding="utf-8")
    html = (STATIC_ADMIN_DIR / "users.html").read_text(encoding="utf-8")
    js = (STATIC_ADMIN_DIR.parent / "js" / "admin-users.js").read_text(encoding="utf-8")

    assert '@router.get("/admin/users", include_in_schema=False)' in router_py
    assert 'return _serve_html("admin/users.html")' in router_py
    assert "from .users import router as _users_router" in admin_init
    assert "router.include_router(_users_router)" in admin_init
    assert 'href="/admin/users"' in header
    assert 'href="/admin/users"' in header.split('href="/admin/config"')[0]
    assert 'data-i18n="header.users"' in header
    assert "setUsersNavVisible(await loadMultiUserEnabled())" in js
    assert 'id="usersBody"' in html
    assert 'id="importPanel"' in html
    assert "/static/js/admin-users.js?v={{APP_VERSION}}-v2" in html
    assert "const API = '/admin/api/webui/users';" in js
    assert "function saveUsers()" in js
    assert "function parseImportText(text)" in js
    assert "regen-api-key" in js
    assert "regenerateApiKey" in js
    assert 'data-i18n="users.columns.grokQuota"' in html
    assert 'data-i18n="users.columns.gptQuota"' in html
    assert "grok_daily_quota" in js
    assert "gpt_daily_quota" in js


def test_cache_page_local_images_expose_image_host_links() -> None:
    """Local image cache rows should expose copyable image-host URLs."""
    html = (STATIC_ADMIN_DIR / "cache.html").read_text(encoding="utf-8")
    cache_py = (
        STATIC_ADMIN_DIR.parent.parent / "products" / "web" / "admin" / "cache.py"
    ).read_text(encoding="utf-8")

    assert 'id="btn-imagehost-copy"' in html
    assert "function publicFileUrl(name)" in html
    assert "function copyImageHostUrl(name)" in html
    assert "function copyImageHostMarkdown(name)" in html
    assert "function copyImageHostList()" in html
    assert "cache.copyImageUrl" in html
    assert "public_base_url" in cache_py
    assert 'get_str("app.app_url"' in cache_py
