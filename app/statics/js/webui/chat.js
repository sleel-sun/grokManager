(() => {
  const VERIFY_ENDPOINT = '/webui/api/verify';
  const MODELS_ENDPOINT = '/webui/api/models';
  const CHAT_ENDPOINT = '/webui/api/chat/completions';
  const MCP_SERVERS_ENDPOINT = '/webui/api/mcp/servers';
  const MCP_IMPORT_ENDPOINT = '/webui/api/mcp/servers/import';
  const MCP_TOOLS_ENDPOINT = '/webui/api/mcp/tools';
  const CODE_PREVIEWS_ENDPOINT = '/webui/api/code-previews';
  const ATTACHMENT_DOWNLOAD_ENDPOINT = '/webui/api/attachments/download';
  const IMAGE_STUDIO_CACHE_ENDPOINT = '/webui/api/images/history/cache-url';
  const IMAGE_STUDIO_PENDING_REFERENCE_KEY = 'grokmanager.image_studio.pending_reference.v1';
  const PREFERRED_MODEL = 'grok-4.20-0309';
  const STORE_KEY = 'grok2api_webui_chat_sessions_v1';
  const SIDEBAR_STORE_KEY = 'grok2api_webui_sidebar_collapsed_v1';
  const SIDEBAR_COMPACT_QUERY = '(max-width: 960px)';
  const MCP_SETTINGS_KEY = 'grok2api_webui_mcp_settings_v1';
  const SEARCH_SETTINGS_KEY = 'grok2api_webui_search_settings_v1';
  const PREVIEW_IFRAME_SANDBOX = 'allow-scripts allow-forms allow-modals allow-popups';
  const PREVIEW_CSP = [
    "default-src 'none'",
    "img-src data: blob: https: http:",
    "media-src data: blob: https: http:",
    "font-src data: https: http:",
    "style-src 'unsafe-inline' https: http:",
    "script-src 'unsafe-inline' https: http:",
    "connect-src https: http:",
    "frame-src data: blob: https: http:",
    "form-action 'none'",
  ].join('; ');

  const chatLayout = document.getElementById('chatLayout');
  const modelSelect = document.getElementById('modelSelect');
  const systemInput = document.getElementById('systemInput');
  const thread = document.getElementById('thread');
  const emptyState = document.getElementById('emptyState');
  const statusEl = document.getElementById('status');
  const promptInput = document.getElementById('promptInput');
  const sendBtn = document.getElementById('sendBtn');
  const newChatBtn = document.getElementById('newChatBtn');
  const sidebarToggleBtn = document.getElementById('sidebarToggleBtn');
  const sessionList = document.getElementById('sessionList');
  const uploadBtn = document.getElementById('uploadBtn');
  const fileInput = document.getElementById('fileInput');
  const uploadMeta = document.getElementById('uploadMeta');
  const webSearchBtn = document.getElementById('webSearchBtn');
  const webSearchPreset = document.getElementById('webSearchPreset');
  const mcpBtn = document.getElementById('mcpBtn');
  const mcpModal = document.getElementById('mcpModal');
  const mcpCloseBtn = document.getElementById('mcpCloseBtn');
  const mcpEnabled = document.getElementById('mcpEnabled');
  const mcpAuto = document.getElementById('mcpAuto');
  const mcpToolChoice = document.getElementById('mcpToolChoice');
  const mcpRefreshBtn = document.getElementById('mcpRefreshBtn');
  const mcpDiscoverToolsBtn = document.getElementById('mcpDiscoverToolsBtn');
  const mcpStatus = document.getElementById('mcpStatus');
  const mcpServerList = document.getElementById('mcpServerList');
  const mcpFormTitle = document.getElementById('mcpFormTitle');
  const mcpResetFormBtn = document.getElementById('mcpResetFormBtn');
  const mcpNameInput = document.getElementById('mcpNameInput');
  const mcpCommandInput = document.getElementById('mcpCommandInput');
  const mcpArgsInput = document.getElementById('mcpArgsInput');
  const mcpEnvInput = document.getElementById('mcpEnvInput');
  const mcpCwdInput = document.getElementById('mcpCwdInput');
  const mcpTimeoutInput = document.getElementById('mcpTimeoutInput');
  const mcpServerEnabledInput = document.getElementById('mcpServerEnabledInput');
  const mcpJsonInput = document.getElementById('mcpJsonInput');
  const mcpJsonReplaceInput = document.getElementById('mcpJsonReplaceInput');
  const mcpJsonImportBtn = document.getElementById('mcpJsonImportBtn');
  const mcpDeleteBtn = document.getElementById('mcpDeleteBtn');
  const mcpSaveBtn = document.getElementById('mcpSaveBtn');
  const sessionModal = document.getElementById('sessionModal');
  const sessionModalTitle = document.getElementById('sessionModalTitle');
  const sessionModalDesc = document.getElementById('sessionModalDesc');
  const sessionModalInputWrap = document.getElementById('sessionModalInputWrap');
  const sessionModalInput = document.getElementById('sessionModalInput');
  const sessionModalCancel = document.getElementById('sessionModalCancel');
  const sessionModalConfirm = document.getElementById('sessionModalConfirm');

  let sessions = [];
  let currentSessionId = '';
  let messages = [];
  let abortController = null;
  let sending = false;
  let pendingFiles = [];
  let modalResolver = null;
  let sidebarCollapsed = false;
  let availableModels = [];
  let activeEdit = null;
  let mcpServers = [];
  let mcpToolStatusByServerId = {};
  let mcpToolsLoading = false;
  let mcpSettings = { enabled: false, auto: true, selectedIds: [], toolChoice: 'auto' };
  let searchSettings = { enabled: false, preset: 'default' };
  let editingMcpServerId = '';
  let storeKey = STORE_KEY;
  let sidebarStoreKey = SIDEBAR_STORE_KEY;
  let mcpSettingsKey = MCP_SETTINGS_KEY;
  let searchSettingsKey = SEARCH_SETTINGS_KEY;
  let currentStorageScope = 'anonymous';
  let requireSessionStorageScope = false;
  const PROMPT_MIN_HEIGHT = 36;
  const PROMPT_MAX_HEIGHT = 108;
  let pendingThreadScrollFrame = 0;
  let sessionListRenderSignature = '';
  const sidebarCompactMedia = typeof window.matchMedia === 'function' ? window.matchMedia(SIDEBAR_COMPACT_QUERY) : null;

  function text(key, fallback, params) {
    if (typeof window.t !== 'function') return fallback;
    const value = t(key, params);
    return value === key ? fallback : value;
  }

  function toast(message, type = 'info') {
    if (typeof showToast === 'function') showToast(message, type);
  }

  function formatModelOptionLabel(modelId, fallbackName) {
    const normalized = String(modelId || '').trim().toLowerCase();
    if (!normalized) return fallbackName || '';

    return normalized
      .split('-')
      .filter(Boolean)
      .map((part) => part ? part.charAt(0).toUpperCase() + part.slice(1) : part)
      .join(' ');
  }

  function currentSystemPrompt() {
    return systemInput ? (systemInput.value || '').trim() : '';
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function hasVisibleReasoning(value) {
    return typeof value === 'string' && value.trim().length > 0;
  }

  function hasMessageContent(value) {
    const textValue = typeof value === 'string' ? value : extractTextContent(value);
    return Boolean((textValue || '').trim());
  }

  function sanitizeUrl(value) {
    try {
      const url = new URL(value, window.location.origin);
      return ['http:', 'https:', 'mailto:'].includes(url.protocol) ? url.href : '';
    } catch {
      return '';
    }
  }

  function sanitizeRenderedHtml(html) {
    const template = document.createElement('template');
    template.innerHTML = html;

    const blockedTags = new Set(['script', 'style', 'iframe', 'object', 'embed', 'link', 'meta']);

    function walk(node) {
      if (node.nodeType !== Node.ELEMENT_NODE) return;
      const el = node;
      const tag = el.tagName.toLowerCase();

      if (blockedTags.has(tag)) {
        el.remove();
        return;
      }

      Array.from(el.attributes).forEach((attr) => {
        const name = attr.name.toLowerCase();
        const value = attr.value || '';
        if (name.startsWith('on')) {
          el.removeAttribute(attr.name);
          return;
        }
        if ((name === 'href' || name === 'src') && !sanitizeUrl(value)) {
          el.removeAttribute(attr.name);
          return;
        }
        if (name === 'target') {
          el.setAttribute('target', '_blank');
        }
      });

      Array.from(el.children).forEach((child) => walk(child));
    }

    Array.from(template.content.children).forEach((child) => walk(child));
    return template.innerHTML;
  }

  const DOCUMENT_EXTENSION_RE = /\.(pdf|docx?|xlsx?|pptx?|csv|tsv|txt|md|rtf|json|jsonl|xml|html?|zip|gz|tgz|tar|7z|rar|epub)(?:[?#]|$)/i;
  const PROXIED_DOWNLOAD_HOSTS = new Set([
    'assets.grok.com',
    'grok.x.ai',
    'imgen.x.ai',
    'imagine-public.x.ai',
  ]);

  function isDocumentFilename(value) {
    return DOCUMENT_EXTENSION_RE.test(String(value || '').trim());
  }

  function isDocumentUrl(value) {
    const raw = String(value || '').trim();
    if (!raw || raw.startsWith('data:')) return false;
    if (isImageUrl(raw) || isVideoUrl(raw)) return false;
    try {
      const url = new URL(raw, window.location.origin);
      return isDocumentFilename(url.pathname) || isDocumentFilename(url.search);
    } catch {
      return isDocumentFilename(raw);
    }
  }

  function filenameFromDownloadLink(href, label) {
    const cleanLabel = String(label || '').trim();
    if (isDocumentFilename(cleanLabel) && !/^https?:\/\//i.test(cleanLabel)) {
      return cleanLabel.replace(/[\\/:*?"<>|]+/g, '_').slice(0, 120) || 'attachment';
    }
    try {
      const url = new URL(href, window.location.origin);
      const pathName = decodeURIComponent(url.pathname.split('/').filter(Boolean).pop() || '');
      if (pathName && pathName !== 'content') return pathName.replace(/[\\/:*?"<>|]+/g, '_').slice(0, 120);
    } catch {}
    return (cleanLabel || 'attachment').replace(/[\\/:*?"<>|]+/g, '_').slice(0, 120) || 'attachment';
  }

  function shouldProxyDownloadUrl(href) {
    try {
      const url = new URL(href, window.location.origin);
      return PROXIED_DOWNLOAD_HOSTS.has(url.hostname);
    } catch {
      return false;
    }
  }

  function proxiedDownloadHref(href, filename) {
    if (!shouldProxyDownloadUrl(href)) return href;
    const url = new URL(ATTACHMENT_DOWNLOAD_ENDPOINT, window.location.origin);
    url.searchParams.set('url', href);
    if (filename) url.searchParams.set('filename', filename);
    return url.href;
  }

  function normalizeAttachmentContent(source) {
    return String(source || '').replace(/^(https?:\/\/\S+|\/[^\s]+)$/gm, (match) => {
      const url = match.trim();
      if (!isDocumentUrl(url)) return match;
      return `[${filenameFromDownloadLink(url, '')}](${url})`;
    });
  }

  function enhanceAttachmentDownloads(root) {
    if (!root || typeof root.querySelectorAll !== 'function') return;
    root.querySelectorAll('a[href]').forEach((link) => {
      if (link.closest('pre, code, .msg-download-attachment')) return;
      const originalHref = link.getAttribute('href') || '';
      const label = (link.textContent || '').trim();
      if (!isDocumentUrl(originalHref) && !isDocumentFilename(label)) return;

      const filename = filenameFromDownloadLink(originalHref, label);
      const href = proxiedDownloadHref(originalHref, filename);
      const name = document.createElement('span');
      name.className = 'msg-download-name';
      name.textContent = filename;
      const action = document.createElement('span');
      action.className = 'msg-download-action';
      action.textContent = text('webui.chat.downloadAttachment', 'Download');
      const icon = document.createElement('span');
      icon.className = 'msg-download-icon';
      icon.textContent = 'DOC';

      link.classList.add('msg-download-attachment');
      link.href = href;
      link.setAttribute('download', filename);
      link.setAttribute('rel', 'noreferrer');
      link.removeAttribute('target');
      link.replaceChildren(icon, name, action);
    });
  }

  function renderInlineMarkdown(source) {
    let html = escapeHtml(source);
    html = html.replace(/`([^`]+)`/g, (_, code) => `<code>${code}</code>`);
    html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, label, href) => {
      const safeHref = sanitizeUrl(href.trim());
      const safeLabel = label.trim() || href.trim();
      return safeHref
        ? `<a href="${escapeHtml(safeHref)}" target="_blank" rel="noreferrer">${safeLabel}</a>`
        : safeLabel;
    });
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/(^|[^\*])\*([^*]+)\*/g, '$1<em>$2</em>');
    return html;
  }

  function renderMarkdown(source) {
    const lines = String(source || '').replace(/\r\n?/g, '\n').split('\n');
    const html = [];
    const paragraph = [];
    let listType = '';
    let listItems = [];
    let inCodeBlock = false;
    let codeLines = [];
    let codeLanguage = '';

    function flushParagraph() {
      if (!paragraph.length) return;
      html.push(`<p>${paragraph.map((line) => renderInlineMarkdown(line)).join('<br>')}</p>`);
      paragraph.length = 0;
    }

    function flushList() {
      if (!listItems.length) return;
      html.push(`<${listType}>${listItems.map((item) => `<li>${renderInlineMarkdown(item)}</li>`).join('')}</${listType}>`);
      listItems = [];
      listType = '';
    }

    function flushCodeBlock() {
      if (!inCodeBlock) return;
      const lang = normalizeCodeLanguage(codeLanguage);
      const attrs = lang ? ` class="language-${escapeHtml(lang)}" data-lang="${escapeHtml(lang)}"` : '';
      html.push(`<pre><code${attrs}>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
      inCodeBlock = false;
      codeLines = [];
      codeLanguage = '';
    }

    for (const line of lines) {
      if (line.startsWith('```')) {
        flushParagraph();
        flushList();
        if (inCodeBlock) {
          flushCodeBlock();
        } else {
          inCodeBlock = true;
          codeLines = [];
          codeLanguage = line.slice(3).trim();
        }
        continue;
      }

      if (inCodeBlock) {
        codeLines.push(line);
        continue;
      }

      const trimmed = line.trim();
      const headingMatch = trimmed.match(/^(#{1,6})\s+(.*)$/);
      const unorderedMatch = trimmed.match(/^[-*+]\s+(.*)$/);
      const orderedMatch = trimmed.match(/^\d+\.\s+(.*)$/);
      const quoteMatch = trimmed.match(/^>\s?(.*)$/);

      if (!trimmed) {
        flushParagraph();
        flushList();
        continue;
      }

      if (headingMatch) {
        flushParagraph();
        flushList();
        const level = headingMatch[1].length;
        html.push(`<h${level}>${renderInlineMarkdown(headingMatch[2])}</h${level}>`);
        continue;
      }

      if (unorderedMatch || orderedMatch) {
        flushParagraph();
        const nextType = unorderedMatch ? 'ul' : 'ol';
        const itemText = unorderedMatch ? unorderedMatch[1] : orderedMatch[1];
        if (listType && listType !== nextType) flushList();
        listType = nextType;
        listItems.push(itemText);
        continue;
      }

      flushList();

      if (quoteMatch) {
        flushParagraph();
        html.push(`<blockquote>${renderInlineMarkdown(quoteMatch[1])}</blockquote>`);
        continue;
      }

      paragraph.push(line);
    }

    flushParagraph();
    flushList();
    flushCodeBlock();
    return html.join('') || '<p></p>';
  }

  function _extractMath(source) {
    const placeholders = [];
    // Display math: $$...$$ (must come before inline to avoid double-match)
    let out = source.replace(/\$\$([\s\S]+?)\$\$/g, (_, tex) => {
      const i = placeholders.length;
      placeholders.push({ tex, display: true });
      return `\x02MATH${i}\x03`;
    });
    // Inline math: $...$  (single-line only, no space at edges to avoid false positives)
    out = out.replace(/\$([^\n$]+?)\$/g, (_, tex) => {
      const i = placeholders.length;
      placeholders.push({ tex, display: false });
      return `\x02MATH${i}\x03`;
    });
    return { out, placeholders };
  }

  function renderRichMarkdown(source) {
    if (window.marked && typeof window.marked.parse === 'function') {
      let toRender = normalizeAttachmentContent(normalizeMediaContent(source));
      let placeholders = [];

      if (window.katex) {
        const extracted = _extractMath(toRender);
        toRender = extracted.out;
        placeholders = extracted.placeholders;
      }

      let rendered = window.marked.parse(toRender, {
        async: false,
        breaks: true,
        gfm: true,
      });

      if (window.katex && placeholders.length) {
        rendered = rendered.replace(/\x02MATH(\d+)\x03/g, (_, idx) => {
          const { tex, display } = placeholders[parseInt(idx, 10)];
          try {
            return window.katex.renderToString(tex, { displayMode: display, throwOnError: false });
          } catch (_e) {
            return escapeHtml(display ? `$$${tex}$$` : `$${tex}$`);
          }
        });
      }

      return sanitizeRenderedHtml(rendered);
    }
    return renderMarkdown(source);
  }

  function normalizeCodeLanguage(value) {
    const raw = String(value || '').trim().toLowerCase();
    if (!raw) return '';
    const first = raw.split(/\s+/)[0].replace(/[^a-z0-9_+#.-]/g, '');
    const aliases = {
      htm: 'html',
      xhtml: 'html',
      xml: 'html',
      svg: 'svg',
      javascript: 'js',
      ecmascript: 'js',
      mjs: 'js',
      cjs: 'js',
      jsx: 'js',
      css3: 'css',
    };
    return aliases[first] || first;
  }

  function codeLanguageFor(codeEl) {
    if (!codeEl) return '';
    const explicit = codeEl.getAttribute('data-lang') || codeEl.getAttribute('lang') || '';
    if (explicit) return normalizeCodeLanguage(explicit);
    for (const className of Array.from(codeEl.classList || [])) {
      const match = className.match(/^(?:language|lang)-(.+)$/i);
      if (match) return normalizeCodeLanguage(match[1]);
    }
    return '';
  }

  function looksLikeHtmlCode(value) {
    const trimmed = String(value || '').trim();
    if (!trimmed) return false;
    return /^<!doctype\s+html/i.test(trimmed)
      || /^<html[\s>]/i.test(trimmed)
      || /^<svg[\s>]/i.test(trimmed)
      || /<\/(?:html|body|head|style|script|svg)>/i.test(trimmed)
      || /<(?:div|main|section|article|header|footer|nav|button|form|canvas|video|img|style|script|svg)[\s>]/i.test(trimmed);
  }

  function isHtmlSnippet(snippet) {
    return snippet && (snippet.language === 'html' || snippet.language === 'svg' || looksLikeHtmlCode(snippet.code));
  }

  function isCssSnippet(snippet) {
    return snippet && snippet.language === 'css';
  }

  function isJsSnippet(snippet) {
    return snippet && snippet.language === 'js';
  }

  function escapeRawTextEndTag(value, tagName) {
    const pattern = new RegExp(`</${tagName}`, 'gi');
    return String(value || '').replace(pattern, `<\\/${tagName}`);
  }

  function buildPreviewMeta() {
    return [
      '<meta charset="UTF-8">',
      '<meta name="viewport" content="width=device-width, initial-scale=1.0">',
      `<meta http-equiv="Content-Security-Policy" content="${escapeHtml(PREVIEW_CSP)}">`,
      '<base target="_blank">',
    ].join('');
  }

  function buildStyleTags(cssSnippets) {
    return cssSnippets
      .map((snippet) => `<style>\n${escapeRawTextEndTag(snippet.code, 'style')}\n</style>`)
      .join('\n');
  }

  function buildScriptTags(jsSnippets) {
    return jsSnippets
      .map((snippet) => `<script>\n${escapeRawTextEndTag(snippet.code, 'script')}\n</script>`)
      .join('\n');
  }

  function injectPreviewAssets(html, cssSnippets, jsSnippets) {
    const headAssets = `${buildPreviewMeta()}${buildStyleTags(cssSnippets)}`;
    const scriptAssets = buildScriptTags(jsSnippets);
    let doc = String(html || '').trim();

    if (!/^<!doctype\s+html/i.test(doc) && !/<html[\s>]/i.test(doc)) {
      return [
        '<!doctype html>',
        '<html>',
        `<head>${headAssets}</head>`,
        `<body>${doc}${scriptAssets}</body>`,
        '</html>',
      ].join('');
    }

    if (/<head[\s>]/i.test(doc)) {
      doc = doc.replace(/<head([^>]*)>/i, `<head$1>${headAssets}`);
    } else if (/<html[\s>]/i.test(doc)) {
      doc = doc.replace(/<html([^>]*)>/i, `<html$1><head>${headAssets}</head>`);
    } else {
      doc = `${headAssets}${doc}`;
    }

    if (/<\/body>/i.test(doc)) {
      doc = doc.replace(/<\/body>/i, `${scriptAssets}</body>`);
    } else {
      doc += scriptAssets;
    }

    return doc;
  }

  function buildJsOnlyDocument(jsSnippets) {
    const consoleBootstrap = `
<script>
(function () {
  var output = document.getElementById('__gm_preview_console');
  var original = { log: console.log, warn: console.warn, error: console.error };
  function format(value) {
    if (typeof value === 'string') return value;
    try { return JSON.stringify(value, null, 2); } catch (_e) { return String(value); }
  }
  function write(kind, args) {
    if (!output) return;
    var line = document.createElement('div');
    line.className = 'console-line console-' + kind;
    line.textContent = Array.prototype.slice.call(args).map(format).join(' ');
    output.appendChild(line);
  }
  ['log', 'warn', 'error'].forEach(function (kind) {
    console[kind] = function () {
      write(kind, arguments);
      original[kind].apply(console, arguments);
    };
  });
  window.addEventListener('error', function (event) {
    write('error', [event.message || 'Script error']);
  });
})();
</script>`;
    return [
      '<!doctype html>',
      '<html>',
      '<head>',
      buildPreviewMeta(),
      '<style>',
      'body{margin:0;padding:18px;font:14px/1.55 ui-sans-serif,system-ui,sans-serif;background:#fff;color:#171717}',
      '.preview-note{margin:0 0 12px;color:#666}',
      '#__gm_preview_console{display:grid;gap:6px;padding:12px;border:1px solid #e5e5e5;border-radius:12px;background:#fafafa;white-space:pre-wrap;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}',
      '.console-warn{color:#8a5a00}.console-error{color:#b42318}',
      '</style>',
      '</head>',
      '<body>',
      '<p class="preview-note">JavaScript preview output</p>',
      '<div id="__gm_preview_console"></div>',
      consoleBootstrap,
      buildScriptTags(jsSnippets),
      '</body>',
      '</html>',
    ].join('');
  }

  function buildCssOnlyDocument(cssSnippets) {
    return [
      '<!doctype html>',
      '<html>',
      '<head>',
      buildPreviewMeta(),
      buildStyleTags(cssSnippets),
      '</head>',
      '<body>',
      '<main class="preview-css-sample">',
      '<h1>CSS Preview</h1>',
      '<p>This sample content uses the CSS from the code block.</p>',
      '<button type="button">Button</button>',
      '<div class="card"><strong>Sample card</strong><span>Preview surface</span></div>',
      '</main>',
      '</body>',
      '</html>',
    ].join('');
  }

  function buildPreviewDocument(snippets, index) {
    const current = snippets[index];
    if (!current || !current.code.trim()) return '';

    const cssSnippets = snippets.filter(isCssSnippet);
    const jsSnippets = snippets.filter(isJsSnippet);

    if (isHtmlSnippet(current)) {
      return injectPreviewAssets(current.code, cssSnippets, jsSnippets);
    }

    const htmlSnippet = snippets.find(isHtmlSnippet);
    if (htmlSnippet && (isCssSnippet(current) || isJsSnippet(current))) {
      return injectPreviewAssets(htmlSnippet.code, cssSnippets, jsSnippets);
    }

    if (isCssSnippet(current)) {
      return buildCssOnlyDocument([current]);
    }

    if (isJsSnippet(current)) {
      return buildJsOnlyDocument([current]);
    }

    return '';
  }

  function shouldOfferCodePreview(snippets, index) {
    return Boolean(buildPreviewDocument(snippets, index));
  }

  function codeLanguageLabel(snippet) {
    if (!snippet) return 'Code';
    if (snippet.language) return snippet.language.toUpperCase();
    if (looksLikeHtmlCode(snippet.code)) return 'HTML';
    return 'Code';
  }

  async function savePreviewDocument(srcdoc) {
    const title = text('webui.chat.previewFrameTitle', 'Code preview');
    const headers = {
      'Content-Type': 'application/json',
      ...(await getAuthHeaders()),
    };
    const res = await fetch(CODE_PREVIEWS_ENDPOINT, {
      method: 'POST',
      headers,
      body: JSON.stringify({ srcdoc, title }),
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => '');
      throw new Error(detail || `HTTP ${res.status}`);
    }
    const data = await res.json().catch(() => null);
    const rawUrl = data && data.url ? String(data.url) : '';
    if (rawUrl) {
      return new URL(rawUrl, window.location.origin).href;
    }
    const id = data && data.id ? String(data.id) : '';
    if (id) {
      const url = new URL('/webui/code-preview', window.location.origin);
      url.searchParams.set('id', id);
      return url.href;
    }
    throw new Error(text('webui.chat.previewSaveFailed', 'Failed to save preview URL'));
  }

  function openPreviewPlaceholder() {
    const opened = window.open('about:blank', '_blank');
    if (!opened) {
      toast(text('webui.chat.previewOpenFailed', 'Preview popup was blocked'), 'error');
      return null;
    }
    try {
      opened.opener = null;
    } catch (_e) {}
    try {
      opened.document.title = text('webui.chat.previewFrameTitle', 'Code preview');
      opened.document.body.innerHTML = '<div style="font:14px system-ui;padding:24px;color:#555">Preparing preview...</div>';
    } catch (_e) {}
    return opened;
  }

  async function openPreviewDocument(srcdoc) {
    const opened = openPreviewPlaceholder();
    if (!opened) return;
    try {
      opened.location.href = await savePreviewDocument(srcdoc);
    } catch (error) {
      try {
        opened.close();
      } catch (_e) {}
      toast(error.message || text('webui.chat.previewSaveFailed', 'Failed to save preview URL'), 'error');
    }
  }

  function enhanceCodePreviews(root) {
    if (!root) return;
    const snippets = Array.from(root.querySelectorAll('pre > code')).map((codeEl) => ({
      pre: codeEl.closest('pre'),
      codeEl,
      code: codeEl.textContent || '',
      language: codeLanguageFor(codeEl),
    })).filter((snippet) => snippet.pre);

    snippets.forEach((snippet, index) => {
      const { pre } = snippet;
      if (!shouldOfferCodePreview(snippets, index) || pre.dataset.previewEnhanced === 'true') return;
      pre.dataset.previewEnhanced = 'true';

      const shell = document.createElement('div');
      shell.className = 'code-preview-shell';
      const toolbar = document.createElement('div');
      toolbar.className = 'code-preview-toolbar';

      const label = document.createElement('div');
      label.className = 'code-preview-label';
      label.textContent = codeLanguageLabel(snippet);

      const actions = document.createElement('div');
      actions.className = 'code-preview-actions';

      const previewBtn = document.createElement('button');
      previewBtn.type = 'button';
      previewBtn.className = 'code-preview-btn';
      previewBtn.textContent = text('webui.chat.preview', 'Preview');

      const refreshBtn = document.createElement('button');
      refreshBtn.type = 'button';
      refreshBtn.className = 'code-preview-btn';
      refreshBtn.textContent = text('webui.chat.previewRefresh', 'Refresh');
      refreshBtn.hidden = true;

      const openBtn = document.createElement('button');
      openBtn.type = 'button';
      openBtn.className = 'code-preview-btn';
      openBtn.textContent = text('webui.chat.previewOpen', 'Open');
      openBtn.hidden = true;

      const copyUrlBtn = document.createElement('button');
      copyUrlBtn.type = 'button';
      copyUrlBtn.className = 'code-preview-btn';
      copyUrlBtn.textContent = text('webui.chat.previewCopyUrl', 'Copy URL');
      copyUrlBtn.hidden = true;

      actions.appendChild(previewBtn);
      actions.appendChild(refreshBtn);
      actions.appendChild(openBtn);
      actions.appendChild(copyUrlBtn);
      toolbar.appendChild(label);
      toolbar.appendChild(actions);

      pre.parentNode.insertBefore(shell, pre);
      shell.appendChild(toolbar);
      shell.appendChild(pre);

      let panel = null;
      let iframe = null;
      let lastSrcdoc = '';
      let lastPreviewUrl = '';

      function renderPreview() {
        lastSrcdoc = buildPreviewDocument(snippets, index);
        if (!lastSrcdoc) return;
        if (!panel) {
          panel = document.createElement('div');
          panel.className = 'code-preview-panel';
          iframe = document.createElement('iframe');
          iframe.className = 'code-preview-frame';
          iframe.setAttribute('sandbox', PREVIEW_IFRAME_SANDBOX);
          iframe.setAttribute('referrerpolicy', 'no-referrer');
          iframe.title = text('webui.chat.previewFrameTitle', 'Code preview');
          panel.appendChild(iframe);
          shell.appendChild(panel);
        }
        iframe.srcdoc = lastSrcdoc;
        lastPreviewUrl = '';
      }

      async function getPreviewUrl() {
        const srcdoc = lastSrcdoc || buildPreviewDocument(snippets, index);
        if (!srcdoc) return '';
        if (!lastPreviewUrl) {
          lastPreviewUrl = await savePreviewDocument(srcdoc);
        }
        return lastPreviewUrl;
      }

      previewBtn.addEventListener('click', () => {
        const open = shell.classList.toggle('is-preview-open');
        previewBtn.textContent = open
          ? text('webui.chat.previewHide', 'Hide preview')
          : text('webui.chat.preview', 'Preview');
        refreshBtn.hidden = !open;
        openBtn.hidden = !open;
        copyUrlBtn.hidden = !open;
        if (open) renderPreview();
      });

      refreshBtn.addEventListener('click', renderPreview);
      openBtn.addEventListener('click', async () => {
        const srcdoc = lastSrcdoc || buildPreviewDocument(snippets, index);
        if (srcdoc) await openPreviewDocument(srcdoc);
      });
      copyUrlBtn.addEventListener('click', async () => {
        try {
          const url = await getPreviewUrl();
          if (!url) return;
          await copyToClipboard(url);
          toast(text('webui.chat.previewUrlCopied', 'Preview URL copied'), 'info');
        } catch (error) {
          toast(error.message || String(error), 'error');
        }
      });
    });
  }

  function isImageUrl(value) {
    const normalized = String(value || '').trim().toLowerCase();
    return normalized.includes('/v1/files/image')
      || /\.(png|jpe?g|gif|webp|bmp|svg)(\?|#|$)/.test(normalized)
      || normalized.startsWith('data:image/');
  }

  function isVideoUrl(value) {
    const normalized = String(value || '').trim().toLowerCase();
    return normalized.includes('/v1/files/video')
      || /\.(mp4|webm|mov|m4v|ogg)(\?|#|$)/.test(normalized);
  }

  function isReferenceableImageUrl(value) {
    return String(value || '').trim().toLowerCase().includes('/v1/files/image');
  }

  function normalizeMediaContent(source) {
    const input = String(source || '').replace(/\[video\]\(([^)]+)\)/gi, '$1');
    return input.replace(/^(https?:\/\/\S+|\/v1\/files\/(?:image|video)\?id=\S+|data:image\/[^\s]+)$/gm, (match) => {
      const url = match.trim();
      if (isImageUrl(url)) return `![image](${url})`;
      if (isVideoUrl(url)) return `<video controls preload="metadata" src="${escapeHtml(url)}"></video>`;
      return match;
    });
  }

  async function openImageReferenceInStudio(url) {
    const cleanUrl = String(url || '').trim();
    if (!isReferenceableImageUrl(cleanUrl)) return;
    try {
      sessionStorage.setItem(IMAGE_STUDIO_PENDING_REFERENCE_KEY, JSON.stringify({
        url: cleanUrl,
        name: text('webui.chat.referenceImageName', 'Chat image'),
        created_at: Date.now(),
      }));
    } catch {}

    try {
      const res = await fetch(IMAGE_STUDIO_CACHE_ENDPOINT, {
        method: 'POST',
        headers: await webuiAuthHeaders(true),
        body: JSON.stringify({
          url: cleanUrl,
          prompt: text('webui.chat.referenceImagePrompt', 'Referenced from chat'),
          model: modelSelect.value || 'chat',
          mode: 'cache',
          size: '1024x1024',
          quality: '1k',
        }),
      });
      if (!res.ok) throw new Error(await res.text().catch(() => `HTTP ${res.status}`));
    } catch (error) {
      try {
        sessionStorage.removeItem(IMAGE_STUDIO_PENDING_REFERENCE_KEY);
      } catch {}
      toast(error.message || String(error), 'error');
      return;
    }

    location.href = '/webui/image-studio';
  }

  function appendAssistantImageActions(container, url, index) {
    if (!container || container.querySelector('.msg-media-actions')) return;
    const actionBar = document.createElement('div');
    actionBar.className = 'msg-media-actions';

    const referenceBtn = document.createElement('button');
    referenceBtn.type = 'button';
    referenceBtn.className = 'msg-media-reference-btn';
    referenceBtn.textContent = text('webui.chat.referenceEdit', '引用编辑');
    referenceBtn.dataset.referenceImage = url;
    referenceBtn.dataset.referenceName = text('webui.chat.referenceImageLabel', 'Result {index}', { index: index + 1 });
    referenceBtn.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      void openImageReferenceInStudio(url);
    });

    actionBar.appendChild(referenceBtn);
    container.appendChild(actionBar);
  }

  function enhanceAssistantImageReferences(root) {
    if (!root) return;
    Array.from(root.querySelectorAll('img')).forEach((img, index) => {
      const url = img.getAttribute('src') || '';
      if (!isReferenceableImageUrl(url)) return;
      const container = img.closest('.msg-inline-media') || img.parentElement;
      if (!container) return;
      container.classList.add('msg-generated-media');
      appendAssistantImageActions(container, url, index);
    });
  }

  function extractTextContent(content) {
    if (typeof content === 'string') return content;
    if (!Array.isArray(content)) return '';
    return content
      .filter((block) => block && block.type === 'text' && typeof block.text === 'string' && block.text.trim())
      .map((block) => block.text.trim())
      .join('\n');
  }

  function extractImageUrls(content) {
    if (!Array.isArray(content)) return [];
    return content.flatMap((block) => {
      if (!block || block.type !== 'image_url') return [];
      const image = block.image_url;
      if (typeof image === 'string' && image.trim()) return [image.trim()];
      if (image && typeof image.url === 'string' && image.url.trim()) return [image.url.trim()];
      return [];
    });
  }

  function extractFileItems(content) {
    if (!Array.isArray(content)) return [];
    return content.flatMap((block) => {
      if (!block || typeof block !== 'object') return [];
      if (block.type === 'input_audio') {
        const audio = block.input_audio || {};
        const filename = String(audio.filename || '').trim();
        return [{ kind: 'audio', name: filename || 'audio' }];
      }
      if (block.type === 'file') {
        const file = block.file || {};
        const filename = String(file.filename || '').trim();
        return [{ kind: 'file', name: filename || 'file' }];
      }
      return [];
    });
  }

  function dataUrlMime(value) {
    const match = String(value || '').match(/^data:([^;,]+)[;,]/i);
    return match ? match[1].toLowerCase() : 'application/octet-stream';
  }

  function fallbackNameForMime(mime) {
    if (mime.startsWith('image/')) return `image.${mime.split('/')[1] || 'png'}`;
    if (mime.startsWith('audio/')) return `audio.${mime.split('/')[1] || 'wav'}`;
    return `file.${mime.split('/')[1] || 'bin'}`;
  }

  function extractEditablePendingFiles(content) {
    if (!Array.isArray(content)) return [];
    return content.flatMap((block) => {
      if (!block || typeof block !== 'object') return [];
      if (block.type === 'image_url') {
        const image = block.image_url;
        const url = typeof image === 'string' ? image : image && typeof image.url === 'string' ? image.url : '';
        if (!url || !url.startsWith('data:')) return [];
        const mime = dataUrlMime(url);
        return [{
          name: fallbackNameForMime(mime),
          type: mime,
          size: 0,
          dataUrl: url,
        }];
      }
      if (block.type === 'input_audio') {
        const audio = block.input_audio || {};
        const data = String(audio.data || '').trim();
        if (!data) return [];
        const mime = dataUrlMime(data);
        return [{
          name: String(audio.filename || '').trim() || fallbackNameForMime(mime),
          type: mime,
          size: 0,
          dataUrl: data,
        }];
      }
      if (block.type === 'file') {
        const file = block.file || {};
        const data = String(file.file_data || '').trim();
        if (!data) return [];
        const mime = dataUrlMime(data);
        return [{
          name: String(file.filename || '').trim() || fallbackNameForMime(mime),
          type: mime,
          size: 0,
          dataUrl: data,
        }];
      }
      return [];
    });
  }

  async function copyToClipboard(value) {
    const textValue = String(value || '');
    if (!textValue) return;
    if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
      await navigator.clipboard.writeText(textValue);
      return;
    }
    const textarea = document.createElement('textarea');
    textarea.value = textValue;
    textarea.setAttribute('readonly', '');
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand('copy');
    textarea.remove();
  }

  function beginEditMessage(messageIndex, content) {
    activeEdit = {
      messageIndex,
      text: extractTextContent(content) || (typeof content === 'string' ? content : ''),
      files: extractEditablePendingFiles(content),
    };
    renderThread();
  }

  function summarizeMessageContent(content) {
    const textContent = extractTextContent(content).trim();
    const imageCount = extractImageUrls(content).length;
    const fileCount = extractFileItems(content).length;
    const parts = [];
    if (textContent) parts.push(textContent);
    if (imageCount) parts.push(`[${imageCount} image${imageCount > 1 ? 's' : ''}]`);
    if (fileCount) parts.push(`[${fileCount} file${fileCount > 1 ? 's' : ''}]`);
    return parts.join('\n\n');
  }

  function renderMessageContent(card, role, content) {
    if (Array.isArray(content)) {
      const textContent = extractTextContent(content);
      const imageUrls = extractImageUrls(content);
      const fileItems = extractFileItems(content);
      if (role === 'assistant') {
        const parts = [];
        if (textContent.trim()) parts.push(renderRichMarkdown(textContent));
        if (imageUrls.length) {
          parts.push(imageUrls.map((url) => (
            `<div class="msg-inline-media"><img src="${escapeHtml(url)}" alt="image" loading="lazy"></div>`
          )).join(''));
        }
        card.innerHTML = parts.join('') || '<p></p>';
        enhanceAssistantImageReferences(card);
        enhanceAttachmentDownloads(card);
        enhanceCodePreviews(card);
        return;
      }

      const body = document.createElement('div');
      body.className = 'msg-user-parts';
      if (textContent.trim()) {
        const textNode = document.createElement('div');
        textNode.className = 'msg-user-text';
        textNode.textContent = textContent;
        body.appendChild(textNode);
      }
      if (imageUrls.length) {
        const gallery = document.createElement('div');
        gallery.className = 'msg-user-gallery';
        imageUrls.forEach((url) => {
          const img = document.createElement('img');
          img.src = url;
          img.alt = 'image';
          img.loading = 'lazy';
          gallery.appendChild(img);
        });
        body.appendChild(gallery);
      }
      if (fileItems.length) {
        const attachments = document.createElement('div');
        attachments.className = 'msg-user-files';
        fileItems.forEach((item) => {
          const chip = document.createElement('div');
          chip.className = 'msg-user-file';
          chip.textContent = item.name;
          attachments.appendChild(chip);
        });
        body.appendChild(attachments);
      }
      card.replaceChildren(body);
      return;
    }

    if (role === 'assistant') {
      card.innerHTML = renderRichMarkdown(content);
      enhanceAssistantImageReferences(card);
      enhanceAttachmentDownloads(card);
      enhanceCodePreviews(card);
      return;
    }
    card.textContent = content;
  }

  function renderAssistantWaiting(card) {
    card.innerHTML = '<div class="msg-loading" aria-hidden="true"><span class="msg-loading-spinner"></span></div>';
  }

  function parseSseEvent(chunk) {
    let event = 'message';
    const dataLines = [];
    for (const line of chunk.split('\n')) {
      if (line.startsWith('event:')) {
        event = line.slice(6).trim() || 'message';
        continue;
      }
      if (line.startsWith('data:')) {
        dataLines.push(line.slice(5).trimStart());
      }
    }
    return { event, data: dataLines.join('\n') };
  }

  function loadStore() {
    try {
      const raw = localStorage.getItem(storeKey);
      if (!raw) return { sessions: [], currentSessionId: '' };
      const parsed = JSON.parse(raw);
      const rawSessions = Array.isArray(parsed)
        ? parsed
        : (Array.isArray(parsed && parsed.sessions) ? parsed.sessions : []);
      const scopedSessions = rawSessions.filter(sessionMatchesStorageScope);
      const storedCurrentId = Array.isArray(parsed)
        ? (scopedSessions[0] && scopedSessions[0].id || '')
        : (parsed && parsed.currentSessionId ? String(parsed.currentSessionId) : '');
      return {
        sessions: scopedSessions,
        currentSessionId: scopedSessions.some((item) => item && item.id === storedCurrentId)
          ? storedCurrentId
          : (scopedSessions[0] && scopedSessions[0].id || ''),
      };
    } catch {
      return { sessions: [], currentSessionId: '' };
    }
  }

  function persistStore() {
    const serializedSessions = sessions.map((session) => ({
      ...session,
      storageScope: currentStorageScope,
      messages: Array.isArray(session.messages)
        ? session.messages.map((message) => ({
            ...message,
            content: Array.isArray(message.content)
              ? summarizeMessageContent(message.content)
              : message.content,
          }))
        : [],
    }));
    localStorage.setItem(storeKey, JSON.stringify({ sessions: serializedSessions, currentSessionId }));
  }

  function applySidebarState() {
    if (!chatLayout || !sidebarToggleBtn) return;
    chatLayout.classList.toggle('sidebar-collapsed', sidebarCollapsed);
    sidebarToggleBtn.setAttribute('aria-expanded', String(!sidebarCollapsed));
  }

  function isCompactSidebar() {
    return Boolean(sidebarCompactMedia && sidebarCompactMedia.matches);
  }

  function persistSidebarState() {
    try {
      localStorage.setItem(sidebarStoreKey, String(sidebarCollapsed));
    } catch {}
  }

  function collapseSidebarOnCompact() {
    if (!isCompactSidebar() || sidebarCollapsed) return;
    sidebarCollapsed = true;
    applySidebarState();
    persistSidebarState();
  }

  function loadSidebarState() {
    try {
      sidebarCollapsed = localStorage.getItem(sidebarStoreKey) === 'true';
    } catch {
      sidebarCollapsed = false;
    }
    if (isCompactSidebar()) sidebarCollapsed = true;
    applySidebarState();
  }

  function toggleSidebar() {
    sidebarCollapsed = !sidebarCollapsed;
    applySidebarState();
    persistSidebarState();
  }

  function createSessionTitle(messagesList) {
    const firstUser = messagesList.find((item) => {
      if (!item || item.role !== 'user') return false;
      return Boolean(extractTextContent(item.content).trim());
    });
    if (!firstUser) return text('webui.chat.untitled', 'New Chat');
    const trimmed = extractTextContent(firstUser.content).trim().replace(/\s+/g, ' ');
    return trimmed.length > 24 ? `${trimmed.slice(0, 24)}...` : trimmed;
  }

  function createSession() {
    return {
      id: `chat_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
      title: text('webui.chat.untitled', 'New Chat'),
      titleLocked: false,
      model: modelSelect.value || PREFERRED_MODEL,
      system: '',
      storageScope: currentStorageScope,
      messages: [],
      updatedAt: Date.now(),
    };
  }

  function normalizeSession(item) {
    return {
      id: item && item.id ? String(item.id) : `chat_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
      title: item && item.title ? String(item.title) : text('webui.chat.untitled', 'New Chat'),
      titleLocked: Boolean(item && item.titleLocked),
      model: item && item.model ? String(item.model) : PREFERRED_MODEL,
      system: item && item.system ? String(item.system) : '',
      storageScope: currentStorageScope,
      messages: Array.isArray(item && item.messages)
        ? item.messages
          .filter((entry) => {
            if (!entry || typeof entry.role !== 'string') return false;
            if (!['user', 'assistant', 'error'].includes(entry.role)) return false;
            return typeof entry.content === 'string' || Array.isArray(entry.content);
          })
          .map((entry) => ({
            ...entry,
            reasoning_content: entry && entry.role === 'assistant' && hasVisibleReasoning(entry.reasoning_content)
              ? entry.reasoning_content
              : '',
            createdAt: Number(entry && entry.createdAt) || Date.now(),
            feedback: entry && typeof entry.feedback === 'string' ? entry.feedback : '',
          }))
        : [],
      updatedAt: Number(item && item.updatedAt) || Date.now(),
    };
  }

  function setAssistantFeedback(messageIndex, feedback) {
    const session = getCurrentSession();
    const message = session && session.messages && session.messages[messageIndex];
    if (!session || !message || message.role !== 'assistant') return;
    message.feedback = message.feedback === feedback ? '' : feedback;
    session.updatedAt = Date.now();
    persistStore();
    renderThread();
  }

  function regenerateAssistantAt(messageIndex) {
    const session = getCurrentSession();
    if (!session || sending || messageIndex < 0) return;

    let userIndex = -1;
    for (let index = messageIndex - 1; index >= 0; index -= 1) {
      if (messages[index] && messages[index].role === 'user') {
        userIndex = index;
        break;
      }
    }
    if (userIndex < 0) return;

    const userContent = messages[userIndex].content;
    promptInput.value = extractTextContent(userContent) || (typeof userContent === 'string' ? userContent : '');
    pendingFiles = extractEditablePendingFiles(userContent);
    messages = messages.slice(0, userIndex);
    session.messages = messages;
    session.updatedAt = Date.now();
    activeEdit = null;
    renderUploadMeta();
    renderSessionList();
    renderThread();
    resizePromptInput();
    void sendMessage();
  }

  function getCurrentSession() {
    return sessions.find((item) => item.id === currentSessionId) || null;
  }

  function moveSessionToTop(session) {
    sessions = [session, ...sessions.filter((item) => item.id !== session.id)];
  }

  async function getAuthHeaders() {
    return webuiAuthHeaders();
  }

  async function ensureAccess() {
    if (await verifyStoredWebuiAccess(VERIFY_ENDPOINT)) return true;
    location.href = '/webui/login';
    return false;
  }

  function migrateLegacyStorageKey(baseKey, scopedKey, shouldMigrate) {
    if (!shouldMigrate || !baseKey || !scopedKey || baseKey === scopedKey) return;
    try {
      if (localStorage.getItem(scopedKey) || !localStorage.getItem(baseKey)) return;
      localStorage.setItem(scopedKey, localStorage.getItem(baseKey));
    } catch {}
  }

  async function initStorageScope() {
    const auth = await webuiAuth.get();
    const shouldMigrateLegacy = Boolean(auth.user && (auth.user.legacy || auth.user.anonymous));
    currentStorageScope = webuiStorageScopeSuffix(await webuiStorageScope());
    requireSessionStorageScope = !shouldMigrateLegacy;
    storeKey = await webuiScopedStorageKey(STORE_KEY);
    sidebarStoreKey = await webuiScopedStorageKey(SIDEBAR_STORE_KEY);
    mcpSettingsKey = await webuiScopedStorageKey(MCP_SETTINGS_KEY);
    searchSettingsKey = await webuiScopedStorageKey(SEARCH_SETTINGS_KEY);
    migrateLegacyStorageKey(STORE_KEY, storeKey, shouldMigrateLegacy);
    migrateLegacyStorageKey(SIDEBAR_STORE_KEY, sidebarStoreKey, shouldMigrateLegacy);
    migrateLegacyStorageKey(MCP_SETTINGS_KEY, mcpSettingsKey, shouldMigrateLegacy);
    migrateLegacyStorageKey(SEARCH_SETTINGS_KEY, searchSettingsKey, shouldMigrateLegacy);
  }

  function sessionMatchesStorageScope(item) {
    if (!requireSessionStorageScope) return true;
    const rawScope = item && (item.storageScope || item.storage_scope || item.userScope || item.ownerScope);
    if (!rawScope) return false;
    return webuiStorageScopeSuffix(rawScope) === currentStorageScope;
  }

  function setStatus(textValue) {
    if (statusEl) statusEl.textContent = textValue;
  }

  function loadMcpSettings() {
    try {
      const raw = localStorage.getItem(mcpSettingsKey);
      const parsed = raw ? JSON.parse(raw) : {};
      mcpSettings = {
        enabled: Boolean(parsed && parsed.enabled),
        auto: parsed && typeof parsed.auto === 'boolean' ? parsed.auto : true,
        selectedIds: Array.isArray(parsed && parsed.selectedIds)
          ? parsed.selectedIds.map((id) => String(id))
          : [],
        toolChoice: ['auto', 'required', 'none'].includes(parsed && parsed.toolChoice)
          ? parsed.toolChoice
          : 'auto',
      };
    } catch {
      mcpSettings = { enabled: false, auto: true, selectedIds: [], toolChoice: 'auto' };
    }
  }

  function persistMcpSettings() {
    try {
      localStorage.setItem(mcpSettingsKey, JSON.stringify(mcpSettings));
    } catch {}
  }

  function loadSearchSettings() {
    try {
      const raw = localStorage.getItem(searchSettingsKey);
      const parsed = raw ? JSON.parse(raw) : {};
      searchSettings = {
        enabled: Boolean(parsed && parsed.enabled),
        preset: ['default', 'deeper'].includes(parsed && parsed.preset)
          ? parsed.preset
          : 'default',
      };
    } catch {
      searchSettings = { enabled: false, preset: 'default' };
    }
  }

  function persistSearchSettings() {
    try {
      localStorage.setItem(searchSettingsKey, JSON.stringify(searchSettings));
    } catch {}
  }

  function syncSearchControls() {
    const isChatModel = currentModelCapability() === 'chat';
    if (webSearchPreset) {
      webSearchPreset.value = searchSettings.preset || 'default';
      webSearchPreset.disabled = Boolean(sending || !isChatModel);
      webSearchPreset.title = isChatModel
        ? text('webui.chat.search.mode', 'Search depth')
        : text('webui.chat.search.chatOnly', 'Web search is only available for chat models');
    }
    if (!webSearchBtn) return;
    const enabled = Boolean(searchSettings.enabled && isChatModel);
    webSearchBtn.classList.toggle('active', enabled);
    webSearchBtn.disabled = Boolean(sending || !isChatModel);
    webSearchBtn.setAttribute('aria-pressed', enabled ? 'true' : 'false');
    webSearchBtn.textContent = enabled
      ? text('webui.chat.search.onButton', '联网中')
      : text('webui.chat.search.button', '联网');
    webSearchBtn.title = isChatModel
      ? text('webui.chat.search.manage', 'Enable web search')
      : text('webui.chat.search.chatOnly', 'Web search is only available for chat models');
  }

  function formatMcpStatusLine(enabledCount, selectedCount, toolCount) {
    if (!enabledCount) {
      return text('webui.chat.mcp.noEnabled', 'No enabled MCP plugins');
    }
    if (!mcpSettings.enabled) {
      return text('webui.chat.mcp.disabledHint', 'MCP is configured but disabled for chat');
    }
    if (mcpSettings.auto) {
      return text('webui.chat.mcp.autoHint', 'Auto mode will use all {n} enabled plugins', { n: enabledCount });
    }
    if (!selectedCount) {
      return text('webui.chat.mcp.noneSelected', 'Select at least one enabled plugin');
    }
    if (toolCount) {
      return text('webui.chat.mcp.selectedWithTools', '{selected} selected, {tools} tools discovered', {
        selected: selectedCount,
        tools: toolCount,
      });
    }
    return text('webui.chat.mcp.selectedCount', '{n} selected', { n: selectedCount });
  }

  function syncMcpControls() {
    if (mcpEnabled) mcpEnabled.checked = Boolean(mcpSettings.enabled);
    if (mcpAuto) mcpAuto.checked = Boolean(mcpSettings.auto);
    if (mcpToolChoice) mcpToolChoice.value = mcpSettings.toolChoice || 'auto';
    const enabledCount = mcpServers.filter((server) => server && server.enabled).length;
    const selectedCount = mcpSettings.auto
      ? enabledCount
      : mcpSettings.selectedIds.filter((id) => mcpServers.some((server) => server.id === id && server.enabled)).length;
    const callable = mcpSettings.toolChoice !== 'none';
    const toolCount = Object.values(mcpToolStatusByServerId).reduce((sum, info) => {
      if (!info || !Array.isArray(info.tools)) return sum;
      return sum + info.tools.length;
    }, 0);
    if (mcpStatus) {
      mcpStatus.textContent = formatMcpStatusLine(enabledCount, selectedCount, toolCount);
      mcpStatus.classList.toggle('is-warning', Boolean(mcpSettings.enabled && callable && enabledCount && !selectedCount));
    }
    if (mcpDiscoverToolsBtn) {
      mcpDiscoverToolsBtn.disabled = Boolean(mcpToolsLoading || sending || !enabledCount);
      mcpDiscoverToolsBtn.textContent = mcpToolsLoading
        ? text('webui.chat.mcp.discoveringTools', 'Discovering...')
        : text('webui.chat.mcp.discoverTools', 'Discover tools');
    }
    if (mcpJsonImportBtn) {
      mcpJsonImportBtn.disabled = Boolean(sending);
    }
    if (mcpBtn) {
      mcpBtn.classList.toggle('active', Boolean(mcpSettings.enabled && callable && selectedCount));
      mcpBtn.textContent = selectedCount
        ? `MCP ${selectedCount}`
        : text('webui.chat.mcp.button', 'MCP');
      mcpBtn.title = mcpSettings.enabled && callable
        ? text('webui.chat.mcp.enabledTitle', 'MCP enabled')
        : text('webui.chat.mcp.manage', 'Manage MCP plugins');
    }
  }

  function mcpAuthHeaders(contentType = false) {
    return getAuthHeaders().then((headers) => ({
      ...(contentType ? { 'Content-Type': 'application/json' } : {}),
      ...headers,
    }));
  }

  async function loadMcpServers() {
    const headers = await mcpAuthHeaders();
    const res = await fetch(MCP_SERVERS_ENDPOINT, { headers, cache: 'no-store' });
    if (!res.ok) throw new Error(`mcp servers ${res.status}`);
    const data = await res.json();
    mcpServers = Array.isArray(data && data.servers) ? data.servers : [];
    const existingIds = new Set(mcpServers.map((server) => server && server.id).filter(Boolean));
    mcpSettings.selectedIds = mcpSettings.selectedIds.filter((id) => existingIds.has(id));
    mcpToolStatusByServerId = Object.fromEntries(
      Object.entries(mcpToolStatusByServerId).filter(([id]) => existingIds.has(id))
    );
    persistMcpSettings();
    renderMcpServers();
    syncMcpControls();
  }

  async function loadMcpTools() {
    mcpToolsLoading = true;
    syncMcpControls();
    try {
      const headers = await mcpAuthHeaders();
      const res = await fetch(MCP_TOOLS_ENDPOINT, { headers, cache: 'no-store' });
      if (!res.ok) throw new Error(`mcp tools ${res.status}`);
      const data = await res.json();
      const next = {};
      (Array.isArray(data && data.servers) ? data.servers : []).forEach((server) => {
        if (!server || !server.server_id) return;
        next[String(server.server_id)] = {
          error: server.error ? String(server.error) : '',
          tools: Array.isArray(server.tools) ? server.tools : [],
        };
      });
      mcpToolStatusByServerId = next;
      renderMcpServers();
    } finally {
      mcpToolsLoading = false;
      syncMcpControls();
    }
  }

  function openMcpModal() {
    if (!mcpModal) return;
    mcpModal.classList.add('open');
    mcpModal.setAttribute('aria-hidden', 'false');
    loadMcpServers().catch((error) => {
      toast(error.message || String(error), 'error');
    });
  }

  function closeMcpModal() {
    if (!mcpModal) return;
    mcpModal.classList.remove('open');
    mcpModal.setAttribute('aria-hidden', 'true');
  }

  function parseLines(value) {
    return String(value || '')
      .replace(/\r\n?/g, '\n')
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean);
  }

  function parseEnv(value) {
    const env = {};
    parseLines(value).forEach((line) => {
      const index = line.indexOf('=');
      if (index <= 0) return;
      const key = line.slice(0, index).trim();
      if (!key) return;
      env[key] = line.slice(index + 1);
    });
    return env;
  }

  function formatEnv(env) {
    if (!env || typeof env !== 'object') return '';
    return Object.keys(env)
      .sort()
      .map((key) => `${key}=${env[key] == null ? '' : env[key]}`)
      .join('\n');
  }

  function resetMcpForm(server) {
    editingMcpServerId = server && server.id ? server.id : '';
    if (mcpFormTitle) {
      mcpFormTitle.textContent = editingMcpServerId
        ? text('webui.chat.mcp.editTitle', 'Edit MCP Plugin')
        : text('webui.chat.mcp.addTitle', 'Add MCP Plugin');
    }
    if (mcpNameInput) mcpNameInput.value = server && server.name ? server.name : '';
    if (mcpCommandInput) mcpCommandInput.value = server && server.command ? server.command : '';
    if (mcpArgsInput) mcpArgsInput.value = Array.isArray(server && server.args) ? server.args.join('\n') : '';
    if (mcpEnvInput) mcpEnvInput.value = formatEnv(server && server.env);
    if (mcpCwdInput) mcpCwdInput.value = server && server.cwd ? server.cwd : '';
    if (mcpTimeoutInput) mcpTimeoutInput.value = String(server && server.timeout_s ? server.timeout_s : 30);
    if (mcpServerEnabledInput) mcpServerEnabledInput.checked = server ? Boolean(server.enabled) : true;
    if (mcpDeleteBtn) mcpDeleteBtn.hidden = !editingMcpServerId;
  }

  function formatMcpToolSummary(serverId) {
    const status = mcpToolStatusByServerId[String(serverId || '')];
    if (!status) return text('webui.chat.mcp.toolsUnknown', 'Tools not discovered');
    if (status.error) return text('webui.chat.mcp.toolsError', 'Tool discovery failed: {error}', { error: status.error });
    const tools = Array.isArray(status.tools) ? status.tools : [];
    if (!tools.length) return text('webui.chat.mcp.toolsEmpty', 'No tools exposed');
    const names = tools
      .map((tool) => String((tool && (tool.name || tool.title)) || '').trim())
      .filter(Boolean)
      .slice(0, 4);
    return text('webui.chat.mcp.toolsSummary', '{n} tools: {tools}', {
      n: tools.length,
      tools: names.join(', ') || '-',
    });
  }

  function renderMcpServers() {
    if (!mcpServerList) return;
    if (!mcpServers.length) {
      mcpServerList.innerHTML = `<div class="webui-mcp-empty">${escapeHtml(text('webui.chat.mcp.empty', 'No MCP plugins installed yet.'))}</div>`;
      syncMcpControls();
      return;
    }

    const fragment = document.createDocumentFragment();
    mcpServers.forEach((server) => {
      if (!server || !server.id) return;
      const item = document.createElement('div');
      item.className = `webui-mcp-item${server.enabled ? '' : ' disabled'}`;

      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.checked = (mcpSettings.auto && server.enabled) || mcpSettings.selectedIds.includes(server.id);
      checkbox.disabled = Boolean(mcpSettings.auto || !server.enabled);
      checkbox.title = mcpSettings.auto
        ? text('webui.chat.mcp.autoSelectHint', 'Turn off auto mode to choose plugins manually')
        : text('webui.chat.mcp.selectPlugin', 'Select this plugin');
      checkbox.addEventListener('change', () => {
        const selected = new Set(mcpSettings.selectedIds);
        if (checkbox.checked) selected.add(server.id);
        else selected.delete(server.id);
        mcpSettings.selectedIds = Array.from(selected);
        persistMcpSettings();
        syncMcpControls();
      });

      const body = document.createElement('button');
      body.type = 'button';
      body.className = 'webui-mcp-item-body';
      body.addEventListener('click', () => resetMcpForm(server));

      const title = document.createElement('span');
      title.className = 'webui-mcp-item-title';
      const titleText = document.createElement('span');
      titleText.textContent = server.name || server.id;
      const badge = document.createElement('span');
      badge.className = `webui-mcp-item-badge${server.enabled ? '' : ' disabled'}`;
      badge.textContent = server.enabled
        ? text('webui.chat.mcp.enabledBadge', 'Enabled')
        : text('webui.chat.mcp.disabledBadge', 'Disabled');
      title.appendChild(titleText);
      title.appendChild(badge);

      const command = document.createElement('span');
      command.className = 'webui-mcp-item-command';
      const args = Array.isArray(server.args) ? server.args.join(' ') : '';
      command.textContent = [server.command, args].filter(Boolean).join(' ');

      const tools = document.createElement('span');
      tools.className = `webui-mcp-item-tools${mcpToolStatusByServerId[server.id] && mcpToolStatusByServerId[server.id].error ? ' error' : ''}`;
      tools.textContent = formatMcpToolSummary(server.id);

      body.appendChild(title);
      body.appendChild(command);
      body.appendChild(tools);
      item.appendChild(checkbox);
      item.appendChild(body);
      fragment.appendChild(item);
    });
    mcpServerList.replaceChildren(fragment);
  }

  function parseMcpImportConfig() {
    const raw = (mcpJsonInput && mcpJsonInput.value || '').trim();
    if (!raw) throw new Error(text('webui.chat.mcp.importEmpty', 'Paste MCP JSON first'));
    try {
      return JSON.parse(raw);
    } catch (_error) {
      throw new Error(text('webui.chat.mcp.importInvalidJson', 'Invalid JSON'));
    }
  }

  async function importMcpServersFromJson() {
    try {
      const config = parseMcpImportConfig();
      const headers = await mcpAuthHeaders(true);
      const res = await fetch(MCP_IMPORT_ENDPOINT, {
        method: 'POST',
        headers,
        body: JSON.stringify({
          config,
          replace: mcpJsonReplaceInput ? Boolean(mcpJsonReplaceInput.checked) : false,
        }),
      });
      if (!res.ok) throw new Error(await res.text().catch(() => `HTTP ${res.status}`));
      const data = await res.json();
      mcpServers = Array.isArray(data && data.servers) ? data.servers : mcpServers;
      const existingIds = new Set(mcpServers.map((server) => server && server.id).filter(Boolean));
      mcpSettings.selectedIds = mcpSettings.selectedIds.filter((id) => existingIds.has(id));
      mcpToolStatusByServerId = {};
      persistMcpSettings();
      renderMcpServers();
      syncMcpControls();
      toast(
        text('webui.chat.mcp.importDone', 'Imported {created}, updated {updated}, skipped {skipped}', {
          created: data && data.created ? data.created : 0,
          updated: data && data.updated ? data.updated : 0,
          skipped: Array.isArray(data && data.skipped) ? data.skipped.length : 0,
        }),
        'info'
      );
    } catch (error) {
      toast(error.message || String(error), 'error');
    }
  }

  function buildMcpServerPayload() {
    const name = (mcpNameInput && mcpNameInput.value || '').trim();
    const command = (mcpCommandInput && mcpCommandInput.value || '').trim();
    if (!name) throw new Error(text('webui.chat.mcp.nameRequired', 'MCP plugin name is required'));
    if (!command) throw new Error(text('webui.chat.mcp.commandRequired', 'MCP command is required'));
    const timeout = Number(mcpTimeoutInput && mcpTimeoutInput.value || 30);
    return {
      name,
      enabled: mcpServerEnabledInput ? Boolean(mcpServerEnabledInput.checked) : true,
      transport: 'stdio',
      command,
      args: parseLines(mcpArgsInput && mcpArgsInput.value),
      env: parseEnv(mcpEnvInput && mcpEnvInput.value),
      cwd: (mcpCwdInput && mcpCwdInput.value || '').trim() || null,
      timeout_s: Number.isFinite(timeout) ? Math.min(Math.max(timeout, 1), 300) : 30,
    };
  }

  async function saveMcpServer() {
    try {
      const payload = buildMcpServerPayload();
      const headers = await mcpAuthHeaders(true);
      const endpoint = editingMcpServerId
        ? `${MCP_SERVERS_ENDPOINT}/${encodeURIComponent(editingMcpServerId)}`
        : MCP_SERVERS_ENDPOINT;
      const res = await fetch(endpoint, {
        method: editingMcpServerId ? 'PUT' : 'POST',
        headers,
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error(await res.text().catch(() => `HTTP ${res.status}`));
      const data = await res.json();
      resetMcpForm(data && data.server);
      await loadMcpServers();
      toast(text('webui.chat.mcp.saved', 'MCP plugin saved'), 'info');
    } catch (error) {
      toast(error.message || String(error), 'error');
    }
  }

  async function deleteMcpServer() {
    if (!editingMcpServerId) return;
    try {
      const headers = await mcpAuthHeaders();
      const res = await fetch(`${MCP_SERVERS_ENDPOINT}/${encodeURIComponent(editingMcpServerId)}`, {
        method: 'DELETE',
        headers,
      });
      if (!res.ok) throw new Error(await res.text().catch(() => `HTTP ${res.status}`));
      mcpSettings.selectedIds = mcpSettings.selectedIds.filter((id) => id !== editingMcpServerId);
      persistMcpSettings();
      resetMcpForm(null);
      await loadMcpServers();
      toast(text('webui.chat.mcp.deleted', 'MCP plugin deleted'), 'info');
    } catch (error) {
      toast(error.message || String(error), 'error');
    }
  }

  function selectedMcpServerIds() {
    if (mcpSettings.auto) return [];
    return mcpSettings.selectedIds.filter((id) => (
      mcpServers.some((server) => server && server.id === id && server.enabled)
    ));
  }

  function buildMcpPayload() {
    if (!mcpSettings.enabled) return undefined;
    return {
      enabled: true,
      auto: Boolean(mcpSettings.auto),
      server_ids: selectedMcpServerIds(),
      tool_choice: mcpSettings.toolChoice || 'auto',
      max_steps: 2,
    };
  }

  function formatMcpStatus(payload) {
    if (!payload || typeof payload !== 'object') return '';
    if (payload.status === 'ready') {
      return text('webui.chat.mcp.statusReady', 'MCP ready: {n} tools', { n: payload.tool_count || 0 });
    }
    if (payload.status === 'running') {
      return text('webui.chat.mcp.statusRunning', 'Calling MCP tool: {tool}', { tool: payload.tool || 'tool' });
    }
    if (payload.status === 'done') {
      return text('webui.chat.mcp.statusDone', 'MCP tool completed: {tool}', { tool: payload.tool || 'tool' });
    }
    if (payload.status === 'error') {
      return text('webui.chat.mcp.statusError', 'MCP tool failed: {tool}', { tool: payload.tool || 'tool' });
    }
    if (payload.status === 'limit') {
      return text('webui.chat.mcp.statusLimit', 'MCP step limit reached');
    }
    return payload.message || '';
  }

  function resizePromptInput() {
    if (!promptInput) return;
    promptInput.style.height = `${PROMPT_MIN_HEIGHT}px`;
    const nextHeight = Math.min(Math.max(promptInput.scrollHeight, PROMPT_MIN_HEIGHT), PROMPT_MAX_HEIGHT);
    promptInput.style.height = `${nextHeight}px`;
    promptInput.style.overflowY = promptInput.scrollHeight > PROMPT_MAX_HEIGHT ? 'auto' : 'hidden';
  }

  function renderSendButton() {
    if (!sendBtn) return;
    const label = sending
      ? text('webui.chat.stop', 'Stop')
      : text('webui.chat.send', 'Send');
    sendBtn.removeAttribute('data-i18n');
    sendBtn.setAttribute('aria-label', label);
    sendBtn.setAttribute('title', label);
    sendBtn.innerHTML = sending
      ? '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M8 8H16V16H8Z"/></svg>'
      : '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M5 12H19"/><path d="M13 6L19 12L13 18"/></svg>';
  }

  function setSending(next) {
    sending = next;
    promptInput.disabled = next;
    modelSelect.disabled = next;
    if (mcpBtn) mcpBtn.disabled = next;
    syncSearchControls();
    syncMcpControls();
    if (systemInput) systemInput.disabled = next;
    renderSendButton();
  }

  function scrollThread() {
    if (pendingThreadScrollFrame) return;
    pendingThreadScrollFrame = window.requestAnimationFrame(() => {
      pendingThreadScrollFrame = 0;
      thread.scrollTop = thread.scrollHeight;
    });
  }

  function hideEmpty() {
    if (emptyState) emptyState.style.display = 'none';
  }

  function showEmpty() {
    if (emptyState) emptyState.style.display = '';
  }

  function renderUploadMeta() {
    if (!uploadMeta) return;
    if (!pendingFiles.length) {
      uploadMeta.hidden = true;
      uploadMeta.replaceChildren();
      return;
    }

    const row = document.createElement('div');
    row.className = 'webui-upload-meta-row';

    pendingFiles.forEach((file, index) => {
      const chip = document.createElement('div');
      chip.className = 'webui-upload-meta-chip';
      chip.title = file.name || 'file';
      const chars = Array.from(String(file.name || 'file'));

      const label = document.createElement('span');
      label.className = 'webui-upload-meta-chip-label';
      label.textContent = chars.length > 5 ? `${chars.slice(0, 5).join('')}...` : (file.name || 'file');
      chip.appendChild(label);

      const removeBtn = document.createElement('button');
      removeBtn.type = 'button';
      removeBtn.className = 'webui-upload-meta-chip-remove';
      removeBtn.setAttribute('aria-label', `删除 ${file.name || 'file'}`);
      removeBtn.setAttribute('title', `删除 ${file.name || 'file'}`);
      removeBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M8 8L16 16M16 8L8 16" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';
      removeBtn.addEventListener('click', () => {
        pendingFiles = pendingFiles.filter((_, itemIndex) => itemIndex !== index);
        if (fileInput && !pendingFiles.length) fileInput.value = '';
        renderUploadMeta();
      });
      chip.appendChild(removeBtn);

      row.appendChild(chip);
    });

    uploadMeta.hidden = false;
    uploadMeta.replaceChildren(row);
  }

  function currentModelCapability() {
    const selected = modelSelect && modelSelect.value
      ? availableModels.find((item) => item && item.id === modelSelect.value)
      : null;
    return selected && selected.capability ? selected.capability : 'chat';
  }

  async function fileToDataUrl(file) {
    return await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ''));
      reader.onerror = () => reject(reader.error || new Error('file read failed'));
      reader.readAsDataURL(file);
    });
  }

  async function preparePendingFiles(fileList) {
    const files = Array.from(fileList || []);
    const prepared = [];

    for (const file of files) {
      if (!file) continue;
      prepared.push({
        name: file.name || 'file',
        type: file.type || 'application/octet-stream',
        size: Number(file.size) || 0,
        dataUrl: await fileToDataUrl(file),
      });
    }

    return prepared;
  }

  function buildUserMessage(prompt, capability) {
    const textBlock = prompt ? [{ type: 'text', text: prompt }] : [];
    const imageFiles = pendingFiles.filter((file) => (file.type || '').startsWith('image/'));
    const audioFiles = pendingFiles.filter((file) => (file.type || '').startsWith('audio/'));
    const otherFiles = pendingFiles.filter((file) => {
      const mime = file.type || '';
      return !mime.startsWith('image/') && !mime.startsWith('audio/');
    });

    const imageBlocks = imageFiles.map((file) => ({
      type: 'image_url',
      image_url: { url: file.dataUrl },
    }));
    const audioBlocks = audioFiles.map((file) => ({
      type: 'input_audio',
      input_audio: {
        data: file.dataUrl,
        filename: file.name,
      },
    }));
    const fileBlocks = otherFiles.map((file) => ({
      type: 'file',
      file: {
        file_data: file.dataUrl,
        filename: file.name,
      },
    }));

    if (capability === 'image') {
      if (pendingFiles.length) {
        throw new Error(text(
          'webui.chat.errors.imageUploadsNotSupported',
          'Image generation does not accept uploaded references here. Use chat, image edit, or video with a reference image.',
        ));
      }
      return { role: 'user', content: prompt };
    }
    if (capability === 'image_edit') {
      if (!imageBlocks.length) {
        throw new Error(text('webui.chat.errors.imageRequired', 'Image edit requires at least one reference image'));
      }
      if (audioBlocks.length || fileBlocks.length) {
        throw new Error(text('webui.chat.errors.imageOnly', 'Image edit only supports image uploads'));
      }
      return { role: 'user', content: [...textBlock, ...imageBlocks] };
    }
    if (capability === 'video') {
      if (audioBlocks.length || fileBlocks.length) {
        throw new Error(text('webui.chat.errors.videoImageOnly', 'Video generation only supports image reference uploads'));
      }
      return imageBlocks.length
        ? { role: 'user', content: [...textBlock, imageBlocks[0]] }
        : { role: 'user', content: prompt };
    }
    if (imageBlocks.length || audioBlocks.length || fileBlocks.length) {
      return { role: 'user', content: [...textBlock, ...imageBlocks, ...audioBlocks, ...fileBlocks] };
    }
    return { role: 'user', content: prompt };
  }

  function closeSessionModal(result) {
    if (!sessionModal) return;
    sessionModal.classList.remove('open');
    sessionModal.setAttribute('aria-hidden', 'true');
    const resolver = modalResolver;
    modalResolver = null;
    if (resolver) resolver(result);
  }

  function openSessionModal({ title, description = '', confirmLabel, cancelLabel, inputValue = '', withInput = false }) {
    if (!sessionModal) return Promise.resolve(null);
    sessionModalTitle.textContent = title;
    sessionModalDesc.textContent = description;
    sessionModalInputWrap.hidden = !withInput;
    sessionModalInput.value = withInput ? inputValue : '';
    sessionModalCancel.textContent = cancelLabel || text('webui.chat.cancel', 'Cancel');
    sessionModalConfirm.textContent = confirmLabel || text('webui.chat.confirm', 'Confirm');
    sessionModal.classList.add('open');
    sessionModal.setAttribute('aria-hidden', 'false');
    if (withInput) {
      setTimeout(() => {
        sessionModalInput.focus();
        sessionModalInput.select();
      }, 0);
    }
    return new Promise((resolve) => {
      modalResolver = resolve;
    });
  }

  function editMessageAt(messageIndex, content) {
    const session = getCurrentSession();
    if (!session || messageIndex < 0) return;
    if (sending) stopMessage();

    promptInput.value = activeEdit ? activeEdit.text : (extractTextContent(content) || (typeof content === 'string' ? content : ''));
    pendingFiles = activeEdit ? activeEdit.files.slice() : extractEditablePendingFiles(content);
    messages = messages.slice(0, messageIndex);
    session.messages = messages;
    session.model = modelSelect.value || PREFERRED_MODEL;
    session.system = currentSystemPrompt();
    if (!session.titleLocked) session.title = createSessionTitle(session.messages);
    session.updatedAt = Date.now();
    activeEdit = null;
    moveSessionToTop(session);
    renderUploadMeta();
    renderSessionList();
    renderThread();
    resizePromptInput();
    setStatus(text('webui.chat.statusReady', 'Ready'));
    persistStore();
    promptInput.focus();
  }

  function createMessage(role, initialText = '', initialReasoning = '', messageIndex = -1) {
    hideEmpty();
    const hasReasoning = role === 'assistant' && hasVisibleReasoning(initialReasoning);
    const isAssistantWaiting = role === 'assistant' && messageIndex < 0 && !hasReasoning && !hasMessageContent(initialText);

    const wrap = document.createElement('div');
    wrap.className = `msg ${role}`;

    const reasoning = document.createElement('div');
    reasoning.className = 'msg-reasoning';
    reasoning.hidden = !hasReasoning;

    const reasoningToggle = document.createElement('button');
    reasoningToggle.type = 'button';
    reasoningToggle.className = 'msg-reasoning-toggle';
    reasoningToggle.setAttribute('aria-expanded', 'true');
    reasoningToggle.innerHTML = `<span class="msg-reasoning-label">${escapeHtml(text('webui.chat.reasoning', 'Reasoning'))}</span><svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M4 6.5 8 10l4-3.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`;

    const reasoningBody = document.createElement('div');
    reasoningBody.className = 'msg-reasoning-body';
    reasoningBody.textContent = hasReasoning ? initialReasoning : '';

    reasoningToggle.addEventListener('click', () => {
      const collapsed = reasoning.classList.toggle('is-collapsed');
      reasoningToggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    });

    reasoning.appendChild(reasoningToggle);
    reasoning.appendChild(reasoningBody);

    const card = document.createElement('div');
    card.className = `msg-card msg-card-${role}`;
    const isEditing = role === 'user' && activeEdit && activeEdit.messageIndex === messageIndex;
    if (isEditing) {
      card.classList.add('msg-card-editing');

      const editor = document.createElement('textarea');
      editor.className = 'msg-edit-textarea';
      editor.value = activeEdit.text;
      editor.placeholder = text('webui.chat.editPlaceholder', 'Edit message');
      editor.addEventListener('input', () => {
        if (!activeEdit || activeEdit.messageIndex !== messageIndex) return;
        activeEdit.text = editor.value;
        editor.style.height = 'auto';
        editor.style.height = `${Math.max(editor.scrollHeight, 52)}px`;
      });
      editor.style.height = 'auto';
      editor.style.height = `${Math.max(editor.scrollHeight, 52)}px`;

      const footer = document.createElement('div');
      footer.className = 'msg-edit-footer';

      const cancelBtn = document.createElement('button');
      cancelBtn.type = 'button';
      cancelBtn.className = 'msg-edit-cancel';
      cancelBtn.textContent = text('webui.chat.cancel', 'Cancel');
      cancelBtn.addEventListener('click', () => {
        activeEdit = null;
        renderThread();
      });

      const saveBtn = document.createElement('button');
      saveBtn.type = 'button';
      saveBtn.className = 'msg-edit-save';
      saveBtn.textContent = text('webui.chat.save', 'Save');
      saveBtn.addEventListener('click', () => {
        editMessageAt(messageIndex, initialText);
      });

      footer.appendChild(cancelBtn);
      footer.appendChild(saveBtn);
      card.appendChild(editor);
      card.appendChild(footer);

      setTimeout(() => {
        editor.focus();
        editor.setSelectionRange(editor.value.length, editor.value.length);
      }, 0);
    } else if (isAssistantWaiting) {
      renderAssistantWaiting(card);
    } else {
      renderMessageContent(card, role, initialText);
    }

    const entry = {
      wrap,
      reasoning,
      reasoningBody,
      card,
      text: initialText,
      reasoningText: initialReasoning,
      waiting: isAssistantWaiting,
      messageIndex,
      actions: null,
      likeBtn: null,
      dislikeBtn: null,
      renderFrame: 0,
    };

    if (role === 'assistant') {
      wrap.appendChild(reasoning);
    }
    wrap.appendChild(card);

    if (role === 'user') {
      const actions = document.createElement('div');
      actions.className = 'msg-actions';

      const editBtn = document.createElement('button');
      editBtn.type = 'button';
      editBtn.className = 'msg-action-btn';
      editBtn.setAttribute('aria-label', text('webui.chat.edit', 'Edit'));
      editBtn.setAttribute('title', text('webui.chat.edit', 'Edit'));
      editBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 20h4l10-10-4-4L4 16v4Z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="m12.5 7.5 4 4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';
      editBtn.addEventListener('click', () => {
        beginEditMessage(messageIndex, initialText);
      });

      const copyBtn = document.createElement('button');
      copyBtn.type = 'button';
      copyBtn.className = 'msg-action-btn';
      copyBtn.setAttribute('aria-label', text('webui.chat.copy', 'Copy'));
      copyBtn.setAttribute('title', text('webui.chat.copy', 'Copy'));
      copyBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="9" y="9" width="10" height="10" rx="3" stroke="currentColor" stroke-width="1.8"/><path d="M15 9V8a3 3 0 0 0-3-3H8a3 3 0 0 0-3 3v4a3 3 0 0 0 3 3h1" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';
      copyBtn.addEventListener('click', async () => {
        try {
          await copyToClipboard(extractTextContent(initialText) || (typeof initialText === 'string' ? initialText : ''));
          toast(text('webui.chat.copySuccess', 'Copied'), 'info');
        } catch (error) {
          toast(error.message || String(error), 'error');
        }
      });

      if (!isEditing) {
        actions.appendChild(editBtn);
        actions.appendChild(copyBtn);
        wrap.appendChild(actions);
      }
    }

    if (role === 'assistant') {
      const actions = document.createElement('div');
      actions.className = 'msg-actions msg-actions-assistant';
      actions.hidden = messageIndex < 0;
      const message = messageIndex >= 0 ? messages[messageIndex] : null;

      const right = document.createElement('div');
      right.className = 'msg-action-group';

      const regenBtn = document.createElement('button');
      regenBtn.type = 'button';
      regenBtn.className = 'msg-action-btn msg-action-btn-regen';
      regenBtn.setAttribute('aria-label', text('webui.chat.regenerate', 'Regenerate'));
      regenBtn.setAttribute('title', text('webui.chat.regenerate', 'Regenerate'));
      regenBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M21 2v6h-6" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/><path d="M3 11a9 9 0 0 1 15.3-6.3L21 8" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/><path d="M3 22v-6h6" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/><path d="M21 13a9 9 0 0 1-15.3 6.3L3 16" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';
      regenBtn.addEventListener('click', () => {
        regenerateAssistantAt(entry.messageIndex);
      });

      const copyBtn = document.createElement('button');
      copyBtn.type = 'button';
      copyBtn.className = 'msg-action-btn';
      copyBtn.setAttribute('aria-label', text('webui.chat.copy', 'Copy'));
      copyBtn.setAttribute('title', text('webui.chat.copy', 'Copy'));
      copyBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="9" y="9" width="10" height="10" rx="3" stroke="currentColor" stroke-width="1.7"/><path d="M15 9V8a3 3 0 0 0-3-3H8a3 3 0 0 0-3 3v4a3 3 0 0 0 3 3h1" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>';
      copyBtn.addEventListener('click', async () => {
        try {
          await copyToClipboard(typeof entry.text === 'string' ? entry.text : extractTextContent(entry.text));
          toast(text('webui.chat.copySuccess', 'Copied'), 'info');
        } catch (error) {
          toast(error.message || String(error), 'error');
        }
      });

      const likeBtn = document.createElement('button');
      likeBtn.type = 'button';
      likeBtn.className = `msg-action-btn${message && message.feedback === 'up' ? ' active' : ''}`;
      likeBtn.setAttribute('aria-label', text('webui.chat.like', 'Like'));
      likeBtn.setAttribute('title', text('webui.chat.like', 'Like'));
      likeBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M7 11.5v7.5M10.5 19h6.1a1.8 1.8 0 0 0 1.76-1.44l1.12-5.6A1.8 1.8 0 0 0 17.72 10H14V6.9a1.7 1.7 0 0 0-3.12-.93L7 11.5v7.5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';
      likeBtn.addEventListener('click', () => {
        setAssistantFeedback(entry.messageIndex, 'up');
      });

      const dislikeBtn = document.createElement('button');
      dislikeBtn.type = 'button';
      dislikeBtn.className = `msg-action-btn${message && message.feedback === 'down' ? ' active' : ''}`;
      dislikeBtn.setAttribute('aria-label', text('webui.chat.dislike', 'Dislike'));
      dislikeBtn.setAttribute('title', text('webui.chat.dislike', 'Dislike'));
      dislikeBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M7 12.5V5M10.5 5h6.1a1.8 1.8 0 0 1 1.76 1.44l1.12 5.6A1.8 1.8 0 0 1 17.72 14H14v3.1a1.7 1.7 0 0 1-3.12.93L7 12.5V5" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';
      dislikeBtn.addEventListener('click', () => {
        setAssistantFeedback(entry.messageIndex, 'down');
      });

      right.appendChild(regenBtn);
      right.appendChild(copyBtn);
      right.appendChild(likeBtn);
      right.appendChild(dislikeBtn);
      actions.appendChild(right);
      wrap.appendChild(actions);
      entry.actions = actions;
      entry.likeBtn = likeBtn;
      entry.dislikeBtn = dislikeBtn;
    }

    thread.appendChild(wrap);

    syncAssistantActions(entry);
    return entry;
  }

  function syncAssistantActions(entry) {
    if (!entry || !entry.actions) return;
    entry.actions.hidden = entry.messageIndex < 0;
    const message = entry.messageIndex >= 0 ? messages[entry.messageIndex] : null;
    if (entry.likeBtn) entry.likeBtn.classList.toggle('active', Boolean(message && message.feedback === 'up'));
    if (entry.dislikeBtn) entry.dislikeBtn.classList.toggle('active', Boolean(message && message.feedback === 'down'));
  }

  function renderAssistantEntry(entry) {
    if (!entry) return;
    entry.renderFrame = 0;
    if (entry.waiting) return;
    if (hasMessageContent(entry.text)) {
      renderMessageContent(entry.card, 'assistant', entry.text);
    } else {
      entry.card.innerHTML = '';
    }
    const hasReasoning = hasVisibleReasoning(entry.reasoningText);
    entry.reasoning.hidden = !hasReasoning;
    entry.reasoningBody.textContent = hasReasoning ? entry.reasoningText : '';
  }

  function scheduleAssistantEntryRender(entry) {
    if (!entry) return;
    if (!entry.renderFrame) {
      entry.renderFrame = window.requestAnimationFrame(() => {
        renderAssistantEntry(entry);
        scrollThread();
      });
    } else {
      scrollThread();
    }
  }

  function flushAssistantEntry(entry) {
    if (!entry) return;
    if (entry.renderFrame) {
      window.cancelAnimationFrame(entry.renderFrame);
      entry.renderFrame = 0;
    }
    renderAssistantEntry(entry);
  }

  function finalizeAssistantEntry(entry, messageIndex) {
    if (!entry) return;
    entry.waiting = false;
    flushAssistantEntry(entry);
    entry.messageIndex = messageIndex;
    syncAssistantActions(entry);
    scrollThread();
  }

  function updateAssistant(entry, delta) {
    if (entry.waiting) entry.waiting = false;
    entry.text += delta;
    scheduleAssistantEntryRender(entry);
  }

  function updateMcpStatus(entry, payload) {
    if (!entry) return;
    const label = formatMcpStatus(payload);
    if (!label) return;
    setStatus(label);
    if (hasMessageContent(entry.text)) return;
    entry.waiting = false;
    entry.card.innerHTML = `<div class="msg-mcp-status"><span class="msg-loading-spinner" aria-hidden="true"></span><span>${escapeHtml(label)}</span></div>`;
    scrollThread();
  }

  function updateReasoning(entry, delta) {
    if (entry.waiting) entry.waiting = false;
    entry.reasoningText += delta;
    scheduleAssistantEntryRender(entry);
  }

  function renderThread() {
    thread.innerHTML = '';
    if (emptyState) thread.appendChild(emptyState);
    if (!messages.length) {
      showEmpty();
      return;
    }
    hideEmpty();
    messages.forEach((message, index) => {
      createMessage(
        message.role,
        message.content,
        message.role === 'assistant' ? (message.reasoning_content || '') : '',
        index,
      );
    });
    scrollThread();
  }

  function renderSessionList() {
    if (!sessionList) return;
    sessionList.dataset.empty = text('webui.chat.noSessions', 'No chats yet');
    const nextSignature = `${currentSessionId}|${sessions.map((session) => `${session.id}:${session.title || ''}`).join('|')}`;
    if (nextSignature === sessionListRenderSignature) return;
    sessionListRenderSignature = nextSignature;
    const fragment = document.createDocumentFragment();

    sessions.forEach((session) => {
      const item = document.createElement('button');
      item.type = 'button';
      item.className = `webui-session-item${session.id === currentSessionId ? ' active' : ''}`;

      const title = document.createElement('div');
      title.className = 'webui-session-title';
      title.textContent = session.title || text('webui.chat.untitled', 'New Chat');
      const actions = document.createElement('div');
      actions.className = 'webui-session-actions';

      const renameBtn = document.createElement('button');
      renameBtn.type = 'button';
      renameBtn.className = 'webui-session-action';
      renameBtn.title = text('webui.chat.rename', 'Rename');
      renameBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none"><path d="M4 20h4l10-10-4-4L4 16v4Z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="m12.5 7.5 4 4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';
      renameBtn.addEventListener('click', (event) => {
        event.stopPropagation();
        renameSession(session.id);
      });

      const deleteBtn = document.createElement('button');
      deleteBtn.type = 'button';
      deleteBtn.className = 'webui-session-action';
      deleteBtn.title = text('webui.chat.delete', 'Delete');
      deleteBtn.innerHTML = '<svg viewBox="0 0 24 24" fill="none"><path d="M5 7h14M9 7V5h6v2M8 7l1 12h6l1-12" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
      deleteBtn.addEventListener('click', (event) => {
        event.stopPropagation();
        deleteSession(session.id);
      });

      actions.appendChild(renameBtn);
      actions.appendChild(deleteBtn);

      item.appendChild(title);
      item.appendChild(actions);
      item.addEventListener('click', () => switchSession(session.id));
      fragment.appendChild(item);
    });
    sessionList.replaceChildren(fragment);
  }

  function syncCurrentSession() {
    const session = getCurrentSession();
    if (!session) return;
    session.model = modelSelect.value || PREFERRED_MODEL;
    session.system = currentSystemPrompt();
    if (!session.titleLocked) session.title = createSessionTitle(session.messages);
    session.updatedAt = Date.now();
    moveSessionToTop(session);
    persistStore();
    renderSessionList();
  }

  function switchSession(id) {
    const session = sessions.find((item) => item.id === id);
    if (!session) return;
    currentSessionId = session.id;
    messages = session.messages;
    pendingFiles = [];
    activeEdit = null;
    if (modelSelect.options.length) {
      modelSelect.value = Array.from(modelSelect.options).some((option) => option.value === session.model)
        ? session.model
        : (modelSelect.value || PREFERRED_MODEL);
    }
    renderUploadMeta();
    renderSessionList();
    renderThread();
    resizePromptInput();
    setStatus(text('webui.chat.statusReady', 'Ready'));
    persistStore();
    collapseSidebarOnCompact();
  }

  function startNewSession() {
    const session = createSession();
    sessions.unshift(session);
    currentSessionId = session.id;
    messages = session.messages;
    pendingFiles = [];
    activeEdit = null;
    renderUploadMeta();
    renderSessionList();
    renderThread();
    resizePromptInput();
    setStatus(text('webui.chat.statusReady', 'Ready'));
    persistStore();
    collapseSidebarOnCompact();
    promptInput.focus();
  }

  function renameSession(id) {
    const session = sessions.find((item) => item.id === id);
    if (!session) return;
    openSessionModal({
      title: text('webui.chat.rename', 'Rename'),
      description: text('webui.chat.renamePrompt', 'Rename session'),
      confirmLabel: text('webui.chat.confirm', 'Confirm'),
      cancelLabel: text('webui.chat.cancel', 'Cancel'),
      inputValue: session.title || text('webui.chat.untitled', 'New Chat'),
      withInput: true,
    }).then((nextTitle) => {
      if (typeof nextTitle !== 'string') return;
      const trimmed = nextTitle.trim();
      if (!trimmed) return;
      session.title = trimmed;
      session.titleLocked = true;
      session.updatedAt = Date.now();
      moveSessionToTop(session);
      persistStore();
      renderSessionList();
    });
  }

  function deleteSession(id) {
    const session = sessions.find((item) => item.id === id);
    if (!session) return;
    openSessionModal({
      title: text('webui.chat.delete', 'Delete'),
      description: text('webui.chat.deleteConfirm', 'Delete this session?'),
      confirmLabel: text('webui.chat.delete', 'Delete'),
      cancelLabel: text('webui.chat.cancel', 'Cancel'),
    }).then((confirmed) => {
      if (!confirmed) return;
      sessions = sessions.filter((item) => item.id !== id);
      if (!sessions.length) {
        startNewSession();
        return;
      }

      const next = sessions[0];
      currentSessionId = next.id;
      persistStore();
      switchSession(next.id);
    });
  }

  function buildPayload() {
    const outgoing = [];
    const system = currentSystemPrompt();
    if (system) outgoing.push({ role: 'system', content: system });
    messages
      .filter((message) => message && (message.role === 'user' || message.role === 'assistant'))
      .forEach((message) => outgoing.push(message));
    const payload = {
      model: modelSelect.value || PREFERRED_MODEL,
      messages: outgoing,
      stream: true,
      temperature: 0.8,
      top_p: 0.95,
    };
    if (searchSettings.enabled && currentModelCapability() === 'chat') {
      payload.deepsearch = searchSettings.preset === 'deeper' ? 'deeper' : 'default';
    }
    const mcpPayload = buildMcpPayload();
    if (mcpPayload && currentModelCapability() === 'chat' && mcpPayload.tool_choice !== 'none') {
      payload.mcp = mcpPayload;
    }
    return payload;
  }

  async function loadModels() {
    const headers = await getAuthHeaders();
    const res = await fetch(MODELS_ENDPOINT, { headers, cache: 'no-store' });
    if (!res.ok) throw new Error(`models ${res.status}`);

    const data = await res.json();
    const items = Array.isArray(data && data.data) ? data.data : [];
    availableModels = items.filter((item) => item && item.id);
    const ids = items.map((item) => item && item.id).filter(Boolean);

    modelSelect.innerHTML = '';
    availableModels.forEach((item) => {
      const opt = document.createElement('option');
      opt.value = item.id;
      opt.textContent = formatModelOptionLabel(item.id, item.name || item.id);
      modelSelect.appendChild(opt);
    });
    modelSelect.value = ids.includes(PREFERRED_MODEL) ? PREFERRED_MODEL : (ids[0] || PREFERRED_MODEL);
  }

  async function sendMessage() {
    if (sending) return;

    const prompt = (promptInput.value || '').trim();
    const capability = currentModelCapability();
    if (!prompt) {
      toast(text('webui.chat.errors.enterPrompt', 'Please enter a message'), 'error');
      return;
    }

    const session = getCurrentSession();
    if (!session) return;
    activeEdit = null;

    let userMessage;
    try {
      userMessage = buildUserMessage(prompt, capability);
    } catch (error) {
      toast(error.message || String(error), 'error');
      return;
    }

    session.model = modelSelect.value || PREFERRED_MODEL;
    session.system = currentSystemPrompt();
    messages.push(userMessage);
    if (!session.titleLocked) session.title = createSessionTitle(messages);
    session.updatedAt = Date.now();
    moveSessionToTop(session);
    persistStore();
    renderSessionList();

    messages[messages.length - 1].createdAt = Date.now();
    messages[messages.length - 1].feedback = '';
    const userEntry = createMessage('user', userMessage.content, '', messages.length - 1);
    void userEntry;
    const assistantCreatedAt = Date.now();
    const assistantEntry = createMessage('assistant', '', '', -1);

    promptInput.value = '';
    pendingFiles = [];
    if (fileInput) fileInput.value = '';
    renderUploadMeta();
    resizePromptInput();
    abortController = new AbortController();
    setSending(true);
    setStatus(text('webui.chat.statusConnecting', 'Connecting...'));

    try {
      const headers = {
        'Content-Type': 'application/json',
        ...(await getAuthHeaders()),
      };
      const res = await fetch(CHAT_ENDPOINT, {
        method: 'POST',
        headers,
        body: JSON.stringify(buildPayload()),
        signal: abortController.signal,
      });
      if (!res.ok) {
        const detail = await res.text().catch(() => '');
        throw new Error(detail || `HTTP ${res.status}`);
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';

      function handleStreamChunk(chunk) {
        const messageEvent = parseSseEvent(chunk);
        const payload = messageEvent.data.trim();
        if (!payload) return false;
        if (messageEvent.event === 'mcp') {
          try {
            updateMcpStatus(assistantEntry, JSON.parse(payload));
          } catch {
            updateMcpStatus(assistantEntry, { message: payload });
          }
          return false;
        }
        if (payload === '[DONE]') {
          const finalReasoning = hasVisibleReasoning(assistantEntry.reasoningText) ? assistantEntry.reasoningText : '';
          messages.push({
            role: 'assistant',
            content: assistantEntry.text,
            reasoning_content: finalReasoning,
            createdAt: assistantCreatedAt,
            feedback: '',
          });
          syncCurrentSession();
          finalizeAssistantEntry(assistantEntry, messages.length - 1);
          setStatus(text('webui.chat.statusDone', 'Completed'));
          return true;
        }

        let json;
        try {
          json = JSON.parse(payload);
        } catch {
          return false;
        }

        if (messageEvent.event === 'error' || json.error) {
          const errorMessage = json.error && json.error.message
            ? json.error.message
            : text('webui.chat.errors.requestFailed', 'Request failed');
          throw new Error(errorMessage);
        }

        const choice = json && json.choices && json.choices[0];
        const delta = choice && choice.delta ? choice.delta : {};
        if (typeof delta.reasoning_content === 'string') {
          updateReasoning(assistantEntry, delta.reasoning_content);
          if (hasVisibleReasoning(assistantEntry.reasoningText)) {
            setStatus(text('webui.chat.statusThinking', 'Thinking...'));
          }
        }
        if (delta.content) {
          updateAssistant(assistantEntry, delta.content);
          setStatus(text('webui.chat.statusGenerating', 'Generating...'));
        }
        return false;
      }

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n');
        const chunks = buffer.split('\n\n');
        buffer = chunks.pop() || '';

        for (const chunk of chunks) {
          if (handleStreamChunk(chunk)) return;
        }
      }

      if (buffer.trim() && handleStreamChunk(buffer)) return;

      const finalReasoning = hasVisibleReasoning(assistantEntry.reasoningText) ? assistantEntry.reasoningText : '';
      messages.push({
        role: 'assistant',
        content: assistantEntry.text,
        reasoning_content: finalReasoning,
        createdAt: assistantCreatedAt,
        feedback: '',
      });
      syncCurrentSession();
      finalizeAssistantEntry(assistantEntry, messages.length - 1);
      setStatus(text('webui.chat.statusDone', 'Completed'));
    } catch (error) {
      if (error && error.name === 'AbortError') {
        setStatus(text('webui.chat.statusStopped', 'Stopped'));
      } else {
        messages.push({
          role: 'error',
          content: `${text('webui.chat.errors.requestFailed', 'Request failed')}: ${error.message || error}`,
          createdAt: Date.now(),
          feedback: '',
        });
        syncCurrentSession();
        renderThread();
        toast(text('webui.chat.errors.requestFailed', 'Request failed'), 'error');
        setStatus(text('webui.chat.statusFailed', 'Failed'));
      }
    } finally {
      abortController = null;
      setSending(false);
      scrollThread();
    }
  }

  function stopMessage() {
    if (abortController) abortController.abort();
  }

  function restoreSessions() {
    const stored = loadStore();
    sessions = stored.sessions.map(normalizeSession);
    currentSessionId = stored.currentSessionId;

    if (!sessions.length) {
      startNewSession();
      return;
    }

    const existing = sessions.find((item) => item.id === currentSessionId) || sessions[0];
    switchSession(existing.id);
  }

  async function boot() {
    if (typeof renderWebuiHeader === 'function') await renderWebuiHeader();
    if (typeof renderSiteFooter === 'function') await renderSiteFooter();
    if (window.I18n && typeof window.I18n.apply === 'function') I18n.apply(document);
    renderSendButton();
    if (window.I18n && typeof window.I18n.onReady === 'function') {
      window.I18n.onReady(() => {
        renderSendButton();
        syncSearchControls();
        syncMcpControls();
      });
    }
    if (!await ensureAccess()) return;
    await initStorageScope();
    loadMcpSettings();
    loadSearchSettings();
    loadSidebarState();
    syncSearchControls();
    syncMcpControls();
    loadMcpServers().catch((error) => {
      console.warn('webui mcp load failed', error);
    });
    await loadModels();
    syncSearchControls();
    syncMcpControls();
    restoreSessions();
    resizePromptInput();
    promptInput.focus();
  }

  if (newChatBtn) newChatBtn.addEventListener('click', startNewSession);
  sidebarToggleBtn.addEventListener('click', toggleSidebar);
  if (sidebarCompactMedia) {
    if (typeof sidebarCompactMedia.addEventListener === 'function') {
      sidebarCompactMedia.addEventListener('change', collapseSidebarOnCompact);
    } else if (typeof sidebarCompactMedia.addListener === 'function') {
      sidebarCompactMedia.addListener(collapseSidebarOnCompact);
    }
  }
  sendBtn.addEventListener('click', () => {
    if (sending) {
      stopMessage();
      return;
    }
    sendMessage();
  });
  modelSelect.addEventListener('change', () => {
    syncCurrentSession();
    syncSearchControls();
  });
  if (systemInput) systemInput.addEventListener('change', syncCurrentSession);
  uploadBtn.addEventListener('click', () => fileInput.click());
  if (webSearchBtn) {
    webSearchBtn.addEventListener('click', () => {
      if (currentModelCapability() !== 'chat') return;
      searchSettings.enabled = !searchSettings.enabled;
      persistSearchSettings();
      syncSearchControls();
    });
  }
  if (webSearchPreset) {
    webSearchPreset.addEventListener('change', () => {
      searchSettings.preset = webSearchPreset.value === 'deeper' ? 'deeper' : 'default';
      persistSearchSettings();
      syncSearchControls();
    });
  }
  if (mcpBtn) mcpBtn.addEventListener('click', openMcpModal);
  if (mcpCloseBtn) mcpCloseBtn.addEventListener('click', closeMcpModal);
  if (mcpModal) {
    mcpModal.addEventListener('click', (event) => {
      if (event.target === mcpModal) closeMcpModal();
    });
  }
  if (mcpEnabled) {
    mcpEnabled.addEventListener('change', () => {
      mcpSettings.enabled = Boolean(mcpEnabled.checked);
      persistMcpSettings();
      syncMcpControls();
    });
  }
  if (mcpAuto) {
    mcpAuto.addEventListener('change', () => {
      mcpSettings.auto = Boolean(mcpAuto.checked);
      persistMcpSettings();
      renderMcpServers();
      syncMcpControls();
    });
  }
  if (mcpToolChoice) {
    mcpToolChoice.addEventListener('change', () => {
      mcpSettings.toolChoice = mcpToolChoice.value || 'auto';
      persistMcpSettings();
      syncMcpControls();
    });
  }
  if (mcpRefreshBtn) {
    mcpRefreshBtn.addEventListener('click', () => {
      loadMcpServers().catch((error) => toast(error.message || String(error), 'error'));
    });
  }
  if (mcpDiscoverToolsBtn) {
    mcpDiscoverToolsBtn.addEventListener('click', () => {
      loadMcpTools().catch((error) => toast(error.message || String(error), 'error'));
    });
  }
  if (mcpJsonImportBtn) mcpJsonImportBtn.addEventListener('click', importMcpServersFromJson);
  if (mcpResetFormBtn) mcpResetFormBtn.addEventListener('click', () => resetMcpForm(null));
  if (mcpSaveBtn) mcpSaveBtn.addEventListener('click', saveMcpServer);
  if (mcpDeleteBtn) mcpDeleteBtn.addEventListener('click', deleteMcpServer);
  fileInput.addEventListener('change', async () => {
    try {
      pendingFiles = await preparePendingFiles(fileInput.files || []);
      renderUploadMeta();
    } catch (error) {
      pendingFiles = [];
      if (fileInput) fileInput.value = '';
      renderUploadMeta();
      toast(error.message || String(error), 'error');
    }
  });
  sessionModalCancel.addEventListener('click', () => closeSessionModal(false));
  sessionModalConfirm.addEventListener('click', () => {
    const result = sessionModalInputWrap.hidden ? true : sessionModalInput.value;
    closeSessionModal(result);
  });
  sessionModal.addEventListener('click', (event) => {
    if (event.target === sessionModal) closeSessionModal(false);
  });
  sessionModalInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      closeSessionModal(sessionModalInput.value);
    }
  });
  promptInput.addEventListener('input', resizePromptInput);
  promptInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      sendMessage();
    }
  });

  boot().catch((error) => {
    console.error('webui chat boot failed', error);
    toast(text('webui.chat.errors.initFailed', 'Chat page initialization failed'), 'error');
    setStatus(text('webui.chat.statusInitFailed', 'Initialization failed'));
  });
})();
