from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.platform.auth import middleware as auth_middleware
from app.products.web.webui import attachments as attachments_module
from app.products.web.webui import code_preview as code_preview_module


ROOT = Path(__file__).resolve().parent.parent
WEBUI_PAGES = ROOT / "app" / "products" / "web" / "webui" / "pages.py"
CHAT_JS = ROOT / "app" / "statics" / "js" / "webui" / "chat.js"
APP_CSS = ROOT / "app" / "statics" / "css" / "app.css"
CHAT_HTML = ROOT / "app" / "statics" / "webui" / "chat.html"
CODE_PREVIEW_HTML = ROOT / "app" / "statics" / "webui" / "code-preview.html"
I18N_DIR = ROOT / "app" / "statics" / "i18n"


def _chat_js() -> str:
    return CHAT_JS.read_text(encoding="utf-8")


def test_chat_output_code_blocks_are_enhanced_with_preview_controls() -> None:
    js = _chat_js()

    assert "function enhanceCodePreviews(root)" in js
    assert "root.querySelectorAll('pre > code')" in js
    assert "previewBtn.textContent = text('webui.chat.preview', 'Preview')" in js
    assert "enhanceCodePreviews(card);" in js
    assert js.count("enhanceCodePreviews(card);") >= 2


def test_code_preview_supports_html_css_js_and_sandboxed_iframes() -> None:
    js = _chat_js()

    assert "CODE_PREVIEWS_ENDPOINT = '/webui/api/code-previews'" in js
    assert "function buildPreviewDocument(snippets, index)" in js
    assert "function injectPreviewAssets(html, cssSnippets, jsSnippets)" in js
    assert "function buildCssOnlyDocument(cssSnippets)" in js
    assert "function buildJsOnlyDocument(jsSnippets)" in js
    assert "PREVIEW_IFRAME_SANDBOX = 'allow-scripts allow-forms allow-modals allow-popups'" in js
    assert "iframe.setAttribute('sandbox', PREVIEW_IFRAME_SANDBOX)" in js
    assert "iframe.setAttribute('referrerpolicy', 'no-referrer')" in js
    assert "iframe.srcdoc = lastSrcdoc" in js
    assert "return new URL(rawUrl, window.location.origin).href" in js
    assert "new URL('/webui/code-preview', window.location.origin)" in js
    assert "window.open('about:blank', '_blank')" in js
    assert "opened.location.href = await savePreviewDocument(srcdoc)" in js
    assert "opened.opener = null" in js
    assert "window.open('', '_blank')" not in js


def test_fallback_markdown_preserves_code_fence_language_for_preview() -> None:
    js = _chat_js()

    assert "codeLanguage = line.slice(3).trim()" in js
    assert 'data-lang="${escapeHtml(lang)}"' in js
    assert "javascript: 'js'" in js
    assert "css3: 'css'" in js
    assert "htm: 'html'" in js


def test_code_preview_styles_are_present() -> None:
    css = APP_CSS.read_text(encoding="utf-8")

    for selector in (
        ".code-preview-shell",
        ".code-preview-toolbar",
        ".code-preview-actions",
        ".code-preview-btn",
        ".code-preview-panel",
        ".code-preview-frame",
    ):
        assert selector in css


def test_chat_page_busts_cached_preview_assets() -> None:
    html = CHAT_HTML.read_text(encoding="utf-8")

    assert "/static/css/app.css?v={{APP_VERSION}}-downloads1" in html
    assert "/static/js/webui/chat.js?v={{APP_VERSION}}-downloads1" in html


def test_code_preview_page_and_route_exist() -> None:
    route = WEBUI_PAGES.read_text(encoding="utf-8")
    html = CODE_PREVIEW_HTML.read_text(encoding="utf-8")

    assert '@router.get("/webui/code-preview")' in route
    assert 'return _serve_html("code-preview.html")' in route
    assert "fetch('/webui/api/code-previews/' + encodeURIComponent(id)" in html
    assert "frame.srcdoc = doc.srcdoc" in html
    assert "window.location.href" in html
    assert "grok2api_webui_code_preview_" not in html
    assert "localStorage" not in html


def _configure_code_preview(monkeypatch, tmp_path: Path) -> dict[str, object]:
    monkeypatch.setattr(code_preview_module, "_STORE_DIR", tmp_path / "code_previews")
    config: dict[str, object] = {
        "app.webui_enabled": True,
        "app.webui_key": "secret",
    }

    def fake_get_config(key: str, default=None):
        return config.get(key, default)

    monkeypatch.setattr(auth_middleware, "get_config", fake_get_config)
    return config


def test_code_preview_api_creates_shareable_server_side_links(monkeypatch, tmp_path: Path) -> None:
    _configure_code_preview(monkeypatch, tmp_path)
    payload = {"srcdoc": "<!doctype html><html><body>shared</body></html>", "title": "Shared preview"}

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(auth_middleware.verify_webui_key(authorization=None))
    assert excinfo.value.status_code == 401

    asyncio.run(auth_middleware.verify_webui_key(authorization="Bearer secret"))
    created = asyncio.run(
        code_preview_module.create_code_preview(
            code_preview_module.CodePreviewCreateRequest(**payload)
        )
    )
    assert created.status_code == 200
    body = json.loads(created.body)
    preview_id = body["id"]
    assert body["url"] == f"/webui/code-preview?id={preview_id}"

    shared = asyncio.run(code_preview_module.get_code_preview(preview_id))
    assert shared.status_code == 200
    assert shared.headers["cache-control"] == "no-store"
    shared_body = json.loads(shared.body)
    assert shared_body["srcdoc"] == payload["srcdoc"]
    assert shared_body["title"] == "Shared preview"


def test_code_preview_share_reads_respect_webui_enabled(monkeypatch, tmp_path: Path) -> None:
    config = _configure_code_preview(monkeypatch, tmp_path)
    created = asyncio.run(
        code_preview_module.create_code_preview(
            code_preview_module.CodePreviewCreateRequest(srcdoc="<p>shared</p>")
        )
    )
    preview_id = json.loads(created.body)["id"]

    config["app.webui_enabled"] = False

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(code_preview_module.get_code_preview(preview_id))
    assert excinfo.value.status_code == 404

    with pytest.raises(HTTPException) as create_excinfo:
        asyncio.run(
            code_preview_module.create_code_preview(
                code_preview_module.CodePreviewCreateRequest(srcdoc="<p>disabled</p>")
            )
        )
    assert create_excinfo.value.status_code == 404


def test_code_preview_storage_expires_old_records(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(code_preview_module, "_STORE_DIR", tmp_path)
    preview_id = "expired_preview"
    created_at = int(time.time()) - code_preview_module._PREVIEW_TTL_SECONDS - 1
    path = tmp_path / f"{preview_id}.json"
    path.write_text(
        json.dumps(
            {
                "id": preview_id,
                "title": "Expired",
                "srcdoc": "<p>expired</p>",
                "created_at": created_at,
            }
        ),
        encoding="utf-8",
    )

    assert code_preview_module._read_preview_sync(preview_id) is None
    assert not path.exists()


def test_code_preview_i18n_keys_exist_for_all_locales() -> None:
    required = {
        "preview",
        "previewHide",
        "previewRefresh",
        "previewOpen",
        "previewCopyUrl",
        "previewUrlCopied",
        "previewSaveFailed",
        "previewFrameTitle",
        "previewOpenFailed",
    }

    for path in sorted(I18N_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        chat = data["webui"]["chat"]
        missing = required - set(chat)
        assert not missing, f"{path.name} missing keys: {sorted(missing)}"


def test_webui_chat_exposes_web_search_controls() -> None:
    html = CHAT_HTML.read_text(encoding="utf-8")
    js = _chat_js()
    css = APP_CSS.read_text(encoding="utf-8")

    assert 'id="webSearchBtn"' in html
    assert 'id="webSearchPreset"' in html
    assert 'value="default"' in html
    assert 'value="deeper"' in html
    assert "SEARCH_SETTINGS_KEY = 'grok2api_webui_search_settings_v1'" in js
    assert "function syncSearchControls()" in js
    assert "webSearchPreset.disabled = Boolean(sending || !isChatModel)" in js
    assert "webSearchPreset.disabled = Boolean(sending || !searchSettings.enabled || !isChatModel)" not in js
    assert "payload.deepsearch = searchSettings.preset === 'deeper' ? 'deeper' : 'default'" in js
    assert ".webui-search-btn.active" in css
    assert ".webui-search-mode" in css


def test_webui_chat_mcp_management_exposes_tool_discovery() -> None:
    html = CHAT_HTML.read_text(encoding="utf-8")
    js = _chat_js()
    css = APP_CSS.read_text(encoding="utf-8")

    assert 'id="mcpDiscoverToolsBtn"' in html
    assert 'id="mcpStatus"' in html
    assert 'id="mcpJsonInput"' in html
    assert 'id="mcpJsonImportBtn"' in html
    assert "MCP_TOOLS_ENDPOINT = '/webui/api/mcp/tools'" in js
    assert "MCP_IMPORT_ENDPOINT = '/webui/api/mcp/servers/import'" in js
    assert "async function loadMcpTools()" in js
    assert "async function importMcpServersFromJson()" in js
    assert "formatMcpToolSummary(server.id)" in js
    assert "autoSelectHint" in js
    assert ".webui-mcp-status" in css
    assert ".webui-mcp-item-badge" in css
    assert ".webui-mcp-item-tools" in css
    assert ".webui-mcp-json-input" in css


def test_webui_chat_renders_document_links_as_downloads() -> None:
    js = _chat_js()
    css = APP_CSS.read_text(encoding="utf-8")
    package = (ROOT / "app" / "products" / "web" / "webui" / "__init__.py").read_text(
        encoding="utf-8"
    )

    assert "ATTACHMENT_DOWNLOAD_ENDPOINT = '/webui/api/attachments/download'" in js
    assert "function enhanceAttachmentDownloads(root)" in js
    assert "normalizeAttachmentContent(normalizeMediaContent(source))" in js
    assert "link.setAttribute('download', filename)" in js
    assert "proxiedDownloadHref(originalHref, filename)" in js
    assert "assets.grok.com" in js
    assert ".msg-download-attachment" in css
    assert "from .attachments import router as attachments_router" in package
    assert "router.include_router(attachments_router)" in package


def test_webui_attachment_download_helpers_are_restricted_to_asset_hosts() -> None:
    assert (
        attachments_module._validate_download_reference(
            "https://assets.grok.com/users/u/report.pdf"
        )
        == "https://assets.grok.com/users/u/report.pdf"
    )
    assert attachments_module._validate_download_reference("/users/u/report.pdf") == "/users/u/report.pdf"
    assert attachments_module._safe_filename("../report final.pdf", "/x") == "report final.pdf"
    assert "filename*=UTF-8''report%20final.pdf" in attachments_module._content_disposition(
        "report final.pdf"
    )

    with pytest.raises(Exception):
        attachments_module._validate_download_reference("https://example.invalid/report.pdf")
