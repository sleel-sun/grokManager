from __future__ import annotations

import json
from pathlib import Path


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

    assert "function buildPreviewDocument(snippets, index)" in js
    assert "function injectPreviewAssets(html, cssSnippets, jsSnippets)" in js
    assert "function buildCssOnlyDocument(cssSnippets)" in js
    assert "function buildJsOnlyDocument(jsSnippets)" in js
    assert "PREVIEW_IFRAME_SANDBOX = 'allow-scripts allow-forms allow-modals allow-popups'" in js
    assert "iframe.setAttribute('sandbox', PREVIEW_IFRAME_SANDBOX)" in js
    assert "iframe.setAttribute('referrerpolicy', 'no-referrer')" in js
    assert "iframe.srcdoc = lastSrcdoc" in js
    assert "new URL('/webui/code-preview', window.location.origin)" in js
    assert "window.open(url, '_blank')" in js
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

    assert "/static/css/app.css?v={{APP_VERSION}}-codepreview3" in html
    assert "/static/js/webui/chat.js?v={{APP_VERSION}}-codepreview3" in html


def test_code_preview_page_and_route_exist() -> None:
    route = WEBUI_PAGES.read_text(encoding="utf-8")
    html = CODE_PREVIEW_HTML.read_text(encoding="utf-8")

    assert '@router.get("/webui/code-preview")' in route
    assert 'return _serve_html("code-preview.html")' in route
    assert "grok2api_webui_code_preview_" in html
    assert "frame.srcdoc = doc.srcdoc" in html
    assert "window.location.href" in html


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
    assert "payload.deepsearch = searchSettings.preset === 'deeper' ? 'deeper' : 'default'" in js
    assert ".webui-search-btn.active" in css
    assert ".webui-search-mode" in css
