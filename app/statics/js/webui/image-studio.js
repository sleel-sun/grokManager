(() => {
  const VERIFY_ENDPOINT = '/webui/api/verify';
  const MODELS_ENDPOINT = '/webui/api/images/models';
  const GENERATE_ENDPOINT = '/webui/api/images/generations';
  const EDIT_ENDPOINT = '/webui/api/images/edits';
  const HISTORY_ENDPOINT = '/webui/api/images/history';
  const HISTORY_KEY = 'grokmanager.image_studio.history.v1';
  const PENDING_REFERENCE_KEY = 'grokmanager.image_studio.pending_reference.v1';
  const HISTORY_LIMIT = 24;
  const GPT_MODELS = new Set(['gpt-image-1', 'gpt-image-2', 'codex-gpt-image-2']);
  const EDIT_SIZE = '1024x1024';
  const QUALITY_RANK = { '1k': 1, '2k': 2, '4k': 3 };

  const form = document.getElementById('studioForm');
  const composer = document.getElementById('studioComposer');
  const modeToggle = document.getElementById('studioModeToggle');
  const promptInput = document.getElementById('studioPrompt');
  const promptCount = document.getElementById('studioPromptCount');
  const modelSelect = document.getElementById('studioModel');
  const editModelSelect = document.getElementById('studioEditModel');
  const generateModelField = document.getElementById('studioGenerateModelField');
  const editModelField = document.getElementById('studioEditModelField');
  const imageInput = document.getElementById('studioImages');
  const pickImagesBtn = document.getElementById('studioPickImages');
  const imageCount = document.getElementById('studioImageCount');
  const referencePreview = document.getElementById('studioReferencePreview');
  const sizeSelect = document.getElementById('studioSize');
  const qualitySelect = document.getElementById('studioQuality');
  const qualityHint = document.getElementById('studioQualityHint');
  const countSelect = document.getElementById('studioCount');
  const submitBtn = document.getElementById('studioSubmit');
  const statusEl = document.getElementById('studioStatus');
  const output = document.getElementById('studioOutput');
  const empty = document.getElementById('studioEmpty');
  const clearHistoryBtn = document.getElementById('studioClearHistory');
  const newSessionBtn = document.getElementById('studioNewSession');
  const sessionList = document.getElementById('studioSessionList');
  const modelCount = document.getElementById('studioModelCount');
  const modelHint = document.getElementById('studioModelHint');
  const historyHint = document.getElementById('studioHistoryHint');
  const resultsViewport = document.getElementById('studioResultsViewport');

  let mode = 'generate';
  let running = false;
  let history = readHistory();
  let selectedSessionId = '';
  let draftMode = false;
  let dragDepth = 0;
  let referencePreviewUrls = [];
  let referenceUrls = [];
  let lastGenerateSize = EDIT_SIZE;
  let qualityConfig = { premium: false, default: '1k', max: '1k', options: [{ id: '1k', label: '1K', enabled: true }] };

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function toast(message, type = 'info') {
    if (typeof showToast === 'function') showToast(message, type);
  }

  function setStatus(message, state = 'idle') {
    if (!statusEl) return;
    statusEl.textContent = message;
    statusEl.dataset.state = state;
  }

  function readHistory() {
    try {
      const parsed = JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]');
      return Array.isArray(parsed) ? parsed.slice(0, HISTORY_LIMIT) : [];
    } catch {
      return [];
    }
  }

  function writeHistory() {
    try {
      localStorage.setItem(HISTORY_KEY, JSON.stringify(history.slice(0, HISTORY_LIMIT)));
    } catch {}
  }

  function normalizeImageItems(items, fallbackUrl = '') {
    const images = Array.isArray(items)
      ? items
        .map((item) => (typeof item === 'string' ? { url: item } : item))
        .filter((item) => item && item.url)
      : [];
    if (!images.length && fallbackUrl) images.push({ url: fallbackUrl });
    return images;
  }

  function normalizeMode(value) {
    const raw = String(value || '').toLowerCase();
    if (raw === 'edit' || raw === 'cache') return raw;
    return 'generate';
  }

  function normalizeTurn(turn, fallbackCreatedAt) {
    if (!turn || typeof turn !== 'object') return null;
    const images = normalizeImageItems(turn.images, turn.url);
    if (!images.length) return null;
    const createdAt = Number(turn.created_at || turn.createdAt || fallbackCreatedAt || Date.now());
    return {
      id: String(turn.id || `${createdAt}-${Math.random()}`),
      prompt: String(turn.prompt || ''),
      model: String(turn.model || 'model'),
      mode: normalizeMode(turn.mode),
      size: String(turn.size || '1024x1024'),
      quality: String(turn.quality || '1k'),
      created_at: createdAt,
      reference_names: Array.isArray(turn.reference_names) ? turn.reference_names : [],
      images,
    };
  }

  function normalizeSession(session) {
    if (!session || typeof session !== 'object') return null;
    const createdAt = Number(session.created_at || session.createdAt || Date.now());
    let turns = Array.isArray(session.turns)
      ? session.turns.map((turn) => normalizeTurn(turn, createdAt)).filter(Boolean)
      : [];
    if (!turns.length) {
      const legacyTurn = normalizeTurn(session, createdAt);
      if (legacyTurn) turns = [legacyTurn];
    }
    if (!turns.length) return null;

    const latest = turns[turns.length - 1];
    const title = String(session.title || turns[0].prompt || session.prompt || '');
    return {
      id: String(session.id || `${createdAt}-${Math.random()}`),
      title,
      prompt: String(session.prompt || latest.prompt || title),
      model: String(session.model || latest.model || 'model'),
      mode: normalizeMode(session.mode || latest.mode),
      size: String(session.size || latest.size || '1024x1024'),
      quality: String(session.quality || latest.quality || '1k'),
      created_at: createdAt,
      updated_at: Number(session.updated_at || session.updatedAt || latest.created_at || createdAt),
      reference_names: Array.isArray(session.reference_names) ? session.reference_names : latest.reference_names,
      images: turns.flatMap((turn) => turn.images),
      turns,
    };
  }

  function sessionsFromLocalItems(items) {
    return (Array.isArray(items) ? items : []).map((item) => normalizeSession({
      id: item.id,
      prompt: item.prompt,
      model: item.model,
      mode: item.mode,
      size: item.size,
      quality: item.quality || '1k',
      created_at: item.created_at,
      images: item.images || [{ url: item.url }],
    })).filter(Boolean);
  }

  function historySessions() {
    const normalized = (Array.isArray(history) ? history : [])
      .map((item) => normalizeSession(item))
      .filter(Boolean);
    if (normalized.length) return normalized;
    return sessionsFromLocalItems(history);
  }

  function imageUrlFromItem(item) {
    if (!item || typeof item !== 'object') return '';
    if (item.url) return String(item.url);
    if (item.b64_json) return `data:image/png;base64,${item.b64_json}`;
    return '';
  }

  function normalizeImages(payload) {
    const data = payload && Array.isArray(payload.data) ? payload.data : [];
    return data.map(imageUrlFromItem).filter(Boolean);
  }

  function activeAppendSessionId() {
    return !draftMode && selectedSessionId ? selectedSessionId : '';
  }

  function addHistory({ images, prompt, model, mode: sessionMode, size, quality }) {
    const now = Date.now();
    const turn = normalizeTurn({
      id: `${now}-${Math.random().toString(36).slice(2, 8)}`,
      prompt,
      model,
      mode: sessionMode,
      size,
      quality,
      created_at: now,
      images: images.map((url) => ({ url })),
    }, now);
    if (!turn) return;

    const existingSessions = historySessions();
    const existing = activeAppendSessionId()
      ? existingSessions.find((session) => session.id === selectedSessionId)
      : null;
    if (existing) {
      const turns = Array.isArray(existing.turns) && existing.turns.length
        ? existing.turns.slice()
        : [normalizeTurn(existing, existing.created_at)].filter(Boolean);
      existing.turns = [...turns, turn];
      existing.updated_at = now;
      existing.prompt = turn.prompt;
      existing.model = turn.model;
      existing.mode = turn.mode;
      existing.size = turn.size;
      existing.quality = turn.quality;
      existing.images = existing.turns.flatMap((item) => item.images);
      history = [existing, ...existingSessions.filter((session) => session.id !== existing.id)].slice(0, HISTORY_LIMIT);
      draftMode = false;
      writeHistory();
      renderHistory();
      return;
    }

    const session = normalizeSession({
      id: `${now}`,
      title: prompt,
      prompt,
      model,
      mode: sessionMode,
      size,
      quality,
      created_at: now,
      updated_at: now,
      images: images.map((url) => ({ url })),
      turns: [turn],
    });
    if (!session) return;
    history = [session, ...historySessions()].slice(0, HISTORY_LIMIT);
    selectedSessionId = session.id;
    draftMode = false;
    writeHistory();
    renderHistory();
  }

  function formatDate(value) {
    const date = new Date(value || Date.now());
    if (Number.isNaN(date.getTime())) return '';
    return date.toLocaleString(undefined, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
  }

  function modeLabel(value) {
    if (value === 'edit') return '图像编辑';
    if (value === 'cache') return '引用图';
    return '文生图';
  }

  function shortPrompt(value) {
    const text = String(value || '').trim();
    if (!text) return '未命名画图';
    return text.length > 42 ? `${text.slice(0, 42)}...` : text;
  }

  function renderSidebar(sessions, selected) {
    if (!sessionList) return;
    sessionList.innerHTML = sessions.map((session) => {
      const active = selected && selected.id === session.id ? ' is-active' : '';
      const turnCount = Array.isArray(session.turns) ? session.turns.length : 1;
      const imageCount = Array.isArray(session.images) ? session.images.length : 0;
      const meta = `${turnCount} 轮 · ${imageCount} 张 · ${formatDate(session.updated_at || session.created_at)}`;
      return `<button class="studio-side-session${active}" type="button" data-select-session="${escapeHtml(session.id)}">
        <span class="studio-side-session-title" title="${escapeHtml(session.title || session.prompt)}">${escapeHtml(shortPrompt(session.title || session.prompt))}</span>
        <span class="studio-side-session-meta" title="${escapeHtml(meta)}">${escapeHtml(meta)}</span>
      </button>`;
    }).join('');
  }

  function renderTurn(session, turn, index) {
    const prompt = turn.prompt || 'Untitled';
    const meta = `${modeLabel(turn.mode)} · ${turn.model} · ${String(turn.quality || '1k').toUpperCase()} · ${turn.size} · ${formatDate(turn.created_at)}`;
    const refs = turn.reference_names.length
      ? `<span class="studio-tag">参考图 ${escapeHtml(turn.reference_names.join(', '))}</span>`
      : '';
    const deleteButton = index === 0
      ? `<button class="studio-delete-btn" type="button" data-delete-session="${escapeHtml(session.id)}">删除会话</button>`
      : '';
    const images = turn.images.map((image, imageIndex) => {
      const url = escapeHtml(image.url);
      const label = `结果 ${imageIndex + 1}`;
      return `<article class="studio-output">
        <a class="studio-image-link" href="${url}" target="_blank" rel="noopener">
          <img src="${url}" alt="${escapeHtml(prompt)}" loading="lazy">
        </a>
        <div class="studio-output-body">
          <span class="studio-output-title">${escapeHtml(label)}</span>
          <span class="studio-output-actions">
            <button class="studio-reference-link" type="button" data-reference-image="${url}" data-reference-name="${escapeHtml(label)}">引用编辑</button>
            <a class="studio-output-meta studio-download-link" href="${url}" download>下载</a>
          </span>
        </div>
      </article>`;
    }).join('');

    return `<article class="studio-turn" data-session-id="${escapeHtml(session.id)}" data-turn-id="${escapeHtml(turn.id)}">
      <div class="studio-turn-user">
        <div class="studio-turn-meta">
          <span>${escapeHtml(modeLabel(turn.mode))}</span>
          <span>${escapeHtml(formatDate(turn.created_at))}</span>
        </div>
        <div class="studio-user-bubble">${escapeHtml(prompt)}</div>
      </div>
      <div class="studio-result-wrap">
        <div class="studio-result-toolbar">
          <div class="studio-result-tags">
            <span class="studio-tag">${escapeHtml(turn.images.length)} 张</span>
            <span class="studio-tag">${escapeHtml(turn.model)}</span>
            <span class="studio-tag">${escapeHtml(String(turn.quality || '1k').toUpperCase())}</span>
            <span class="studio-tag">${escapeHtml(turn.size)}</span>
            ${refs}
          </div>
          ${deleteButton}
        </div>
        <div class="studio-session-images">${images}</div>
        <div class="studio-output-meta" title="${escapeHtml(meta)}">${escapeHtml(meta)}</div>
      </div>
    </article>`;
  }

  function renderSelectedSession(session) {
    const turns = Array.isArray(session.turns) && session.turns.length ? session.turns : [session];
    return turns.map((turn, index) => renderTurn(session, turn, index)).join('');
  }

  function renderHistory() {
    if (!output || !empty) return;
    const sessions = historySessions();
    let selected = selectedSessionId ? sessions.find((session) => session.id === selectedSessionId) : null;
    if (!draftMode && !selected && sessions[0]) {
      selected = sessions[0];
      selectedSessionId = selected.id;
    }
    if (draftMode) selected = null;

    renderSidebar(sessions, selected);
    empty.hidden = Boolean(selected);
    empty.style.display = selected ? 'none' : '';
    output.innerHTML = selected ? renderSelectedSession(selected) : '';
    if (historyHint) historyHint.textContent = sessions.length ? `${sessions.length} 个` : '暂无';
  }

  async function loadHistory(preferredSessionId = '') {
    const res = await fetch(HISTORY_ENDPOINT, {
      headers: await webuiAuthHeaders(),
      cache: 'no-store',
    });
    if (!res.ok) throw new Error(await responseError(res));
    const payload = await res.json();
    const sessions = Array.isArray(payload.data) ? payload.data : [];
    history = sessions.map(normalizeSession).filter(Boolean);
    if (preferredSessionId) selectedSessionId = preferredSessionId;
    draftMode = false;
    writeHistory();
    renderHistory();
  }

  function syncSubmitState() {
    if (!submitBtn) return;
    const hasPrompt = Boolean(String((promptInput && promptInput.value) || '').trim());
    submitBtn.disabled = running || !hasPrompt;
  }

  function setRunning(next) {
    running = next;
    if (composer) {
      composer.classList.toggle('is-running', next);
    }
    if (submitBtn) {
      submitBtn.title = next
        ? (mode === 'edit' ? '正在编辑...' : '正在生成...')
        : (mode === 'edit' ? '开始编辑' : '开始生成');
      submitBtn.setAttribute('aria-label', submitBtn.title);
    }
    [
      promptInput,
      modelSelect,
      editModelSelect,
      imageInput,
      sizeSelect,
      qualitySelect,
      countSelect,
      pickImagesBtn,
    ].forEach((el) => {
      if (el) el.disabled = next;
    });
    syncSizeAccess();
    if (modeToggle) {
      modeToggle.querySelectorAll('button').forEach((button) => {
        button.disabled = next;
      });
    }
    syncSubmitState();
  }

  function setMode(nextMode) {
    mode = nextMode === 'edit' ? 'edit' : 'generate';
    if (modeToggle) {
      modeToggle.querySelectorAll('button').forEach((button) => {
        const active = button.dataset.mode === mode;
        button.classList.toggle('is-active', active);
        button.setAttribute('aria-pressed', active ? 'true' : 'false');
      });
    }
    if (generateModelField) generateModelField.classList.toggle('studio-hidden', mode !== 'generate');
    if (editModelField) editModelField.classList.toggle('studio-hidden', mode !== 'edit');
    if (countSelect) {
      Array.from(countSelect.options).forEach((option) => {
        option.disabled = mode === 'edit' && Number(option.value) > 2;
      });
      if (mode === 'edit' && Number(countSelect.value) > 2) countSelect.value = '2';
    }
    if (promptInput) {
      promptInput.placeholder = mode === 'edit'
        ? '描述你希望如何修改参考图'
        : '输入你想要生成的画面，也可直接粘贴图片';
    }
    syncSizeAccess();
    syncQualityAccess();
    setRunning(false);
  }

  function syncPromptCount() {
    const value = String((promptInput && promptInput.value) || '');
    if (promptCount) promptCount.textContent = `${value.trim().length} 字`;
    if (promptInput) {
      promptInput.style.height = 'auto';
      const nextHeight = Math.min(Math.max(promptInput.scrollHeight, 96), 300);
      promptInput.style.height = `${nextHeight}px`;
    }
    syncSubmitState();
  }

  function clearPromptInput() {
    if (!promptInput) return;
    promptInput.value = '';
    syncPromptCount();
  }

  function restorePromptInput(prompt) {
    if (!promptInput || String(promptInput.value || '').trim()) return;
    promptInput.value = prompt;
    syncPromptCount();
  }

  function imageFiles() {
    return Array.from((imageInput && imageInput.files) || []).filter((file) => file.type.startsWith('image/'));
  }

  function referenceCount() {
    return imageFiles().length + referenceUrls.length;
  }

  function syncImageCount() {
    if (!imageCount) return;
    const count = referenceCount();
    imageCount.textContent = count ? `参考图 ${count}` : '上传参考图';
  }

  function revokeReferencePreviewUrls() {
    referencePreviewUrls.forEach((url) => URL.revokeObjectURL(url));
    referencePreviewUrls = [];
  }

  function syncReferencePreview() {
    if (!referencePreview) return;
    revokeReferencePreviewUrls();
    const files = imageFiles();
    const fileItems = files.map((file) => {
      const url = URL.createObjectURL(file);
      referencePreviewUrls.push(url);
      return `<div class="studio-reference-item">
        <img src="${escapeHtml(url)}" alt="${escapeHtml(file.name || 'reference')}" title="${escapeHtml(file.name || '')}">
      </div>`;
    });
    const urlItems = referenceUrls.map((item, index) => {
      const url = escapeHtml(item.url);
      const name = escapeHtml(item.name || `历史图 ${index + 1}`);
      return `<div class="studio-reference-item">
        <img src="${url}" alt="${name}" title="${name}">
        <button class="studio-reference-remove" type="button" data-remove-reference="${index}" aria-label="移除引用">&times;</button>
      </div>`;
    });
    const items = [...fileItems, ...urlItems];
    const visible = items.slice(0, 8);
    const remaining = items.length > 8 ? `<div class="studio-reference-more">+${items.length - 8}</div>` : '';
    referencePreview.innerHTML = `${visible.join('')}${remaining}`;
  }

  function setImageFiles(files) {
    if (!imageInput) return;
    const limit = Math.max(0, 5 - referenceUrls.length);
    const images = files.filter((file) => file && file.type && file.type.startsWith('image/')).slice(0, limit);
    try {
      const transfer = new DataTransfer();
      images.forEach((file) => transfer.items.add(file));
      imageInput.files = transfer.files;
      syncImageCount();
      syncReferencePreview();
      if (images.length) setMode('edit');
    } catch {
      toast('当前浏览器不支持直接粘贴图片，请使用上传按钮', 'error');
    }
  }

  function appendImageFiles(files) {
    setImageFiles([...imageFiles(), ...files]);
  }

  function addReferenceUrl(url, name) {
    const cleanUrl = String(url || '').trim();
    if (!cleanUrl) return;
    if (referenceCount() >= 5) {
      toast('图像编辑最多支持 5 张参考图', 'error');
      return;
    }
    if (referenceUrls.some((item) => item.url === cleanUrl)) {
      toast('这张图片已经在参考图中', 'info');
      setMode('edit');
      return;
    }
    referenceUrls.push({
      url: cleanUrl,
      name: String(name || `历史图 ${referenceUrls.length + 1}`),
    });
    syncImageCount();
    syncReferencePreview();
    setMode('edit');
    setStatus('已引用历史图片，可输入编辑要求', 'idle');
    if (promptInput) promptInput.focus();
  }

  function consumePendingReference() {
    let pending = null;
    try {
      pending = JSON.parse(sessionStorage.getItem(PENDING_REFERENCE_KEY) || 'null');
      sessionStorage.removeItem(PENDING_REFERENCE_KEY);
    } catch {
      pending = null;
    }
    if (!pending || typeof pending !== 'object') return;
    const createdAt = Number(pending.created_at || pending.createdAt || 0);
    if (createdAt && Date.now() - createdAt > 10 * 60 * 1000) return;
    addReferenceUrl(pending.url, pending.name);
  }

  function syncQualityAccess() {
    if (!qualitySelect) return;
    const max = String((qualityConfig && qualityConfig.max) || '1k').toLowerCase();
    const enabledById = new Map(
      (Array.isArray(qualityConfig.options) ? qualityConfig.options : [])
        .map((item) => [String(item.id || '').toLowerCase(), item.enabled !== false])
    );
    Array.from(qualitySelect.options).forEach((option) => {
      const value = String(option.value || '').toLowerCase();
      const enabled = enabledById.has(value)
        ? enabledById.get(value)
        : (QUALITY_RANK[value] || 1) <= (QUALITY_RANK[max] || 1);
      option.disabled = !enabled;
    });
    if (qualitySelect.selectedOptions[0] && qualitySelect.selectedOptions[0].disabled) {
      const fallback = Array.from(qualitySelect.options).find((option) => !option.disabled);
      if (fallback) qualitySelect.value = fallback.value;
    }
    if (qualityHint) {
      qualityHint.textContent = (QUALITY_RANK[max] || 1) > 1 ? `最高 ${max.toUpperCase()}` : '锁定 1K';
    }
  }

  function syncSizeAccess() {
    if (!sizeSelect) return;
    if (mode === 'edit') {
      const current = String(sizeSelect.value || EDIT_SIZE);
      if (current !== EDIT_SIZE) lastGenerateSize = current;
      sizeSelect.value = EDIT_SIZE;
    } else {
      const fallback = Array.from(sizeSelect.options).some((option) => option.value === lastGenerateSize)
        ? lastGenerateSize
        : EDIT_SIZE;
      sizeSelect.value = fallback;
    }
    Array.from(sizeSelect.options).forEach((option) => {
      option.disabled = mode === 'edit' && option.value !== EDIT_SIZE;
    });
    sizeSelect.disabled = running || mode === 'edit';
    sizeSelect.title = mode === 'edit' ? '图像编辑当前仅支持 1:1' : '';
  }

  function optionHtml(model) {
    const label = model.name && model.name !== model.id ? `${model.name} (${model.id})` : model.id;
    return `<option value="${escapeHtml(model.id)}">${escapeHtml(label)}</option>`;
  }

  async function loadModels() {
    const res = await fetch(MODELS_ENDPOINT, {
      headers: await webuiAuthHeaders(),
      cache: 'no-store',
    });
    if (!res.ok) throw new Error(await responseError(res));
    const payload = await res.json();
    const generation = Array.isArray(payload.generation) ? payload.generation : [];
    const edits = Array.isArray(payload.edits)
      ? payload.edits.filter((item) => GPT_MODELS.has(String(item.id || '')))
      : [];

    const workspace = payload.workspace && typeof payload.workspace === 'object' ? payload.workspace : {};
    qualityConfig = workspace.quality || qualityConfig;
    const gptModels = generation.filter((item) => GPT_MODELS.has(String(item.id || '')));
    const gptFirst = (gptModels.length ? gptModels : generation).sort((a, b) => {
      const order = ['gpt-image-2', 'gpt-image-1', 'codex-gpt-image-2'];
      const ai = order.indexOf(String(a.id || ''));
      const bi = order.indexOf(String(b.id || ''));
      return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
    });
    if (modelSelect) {
      modelSelect.innerHTML = gptFirst.length
        ? gptFirst.map(optionHtml).join('')
        : '<option value="">无可用模型</option>';
      const preferred = gptFirst.find((item) => item.id === 'gpt-image-2') || gptFirst[0];
      if (preferred) modelSelect.value = preferred.id;
    }
    if (editModelSelect) {
      editModelSelect.innerHTML = edits.length
        ? edits.map(optionHtml).join('')
        : '<option value="">无可用编辑模型</option>';
      const preferredEdit = edits.find((item) => item.id === 'gpt-image-2') || edits[0];
      if (preferredEdit) editModelSelect.value = preferredEdit.id;
    }
    const total = generation.length + edits.length;
    if (modelCount) modelCount.textContent = String(total);
    if (modelHint) modelHint.textContent = `GPT 工作台 ${gptFirst.length} 个模型，编辑 ${edits.length} 个`;
    if (qualitySelect && qualityConfig.options) {
      qualitySelect.innerHTML = qualityConfig.options.map((item) => (
        `<option value="${escapeHtml(item.id)}">${escapeHtml(item.label || String(item.id || '').toUpperCase())}</option>`
      )).join('');
    }
    syncQualityAccess();
  }

  async function responseError(res) {
    const payload = await res.json().catch(() => null);
    return (
      (payload && payload.error && payload.error.message)
      || (payload && payload.detail)
      || `${res.status} ${res.statusText}`
    );
  }

  async function ensureAccess() {
    if (await verifyStoredWebuiAccess(VERIFY_ENDPOINT)) return true;
    location.href = '/webui/login';
    return false;
  }

  async function generateImages(prompt) {
    const model = (modelSelect && modelSelect.value) || 'gpt-image-2';
    const n = Number((countSelect && countSelect.value) || 1);
    const size = (sizeSelect && sizeSelect.value) || '1024x1024';
    const quality = (qualitySelect && qualitySelect.value) || '1k';
    const sessionId = activeAppendSessionId();
    const res = await fetch(GENERATE_ENDPOINT, {
      method: 'POST',
      headers: await webuiAuthHeaders(true),
      body: JSON.stringify({
        model,
        prompt,
        n,
        size,
        quality,
        response_format: 'url',
        session_id: sessionId || undefined,
      }),
    });
    if (!res.ok) throw new Error(await responseError(res));
    const payload = await res.json();
    return { images: normalizeImages(payload), model, size, quality: payload.quality || quality, session: payload.studio_session };
  }

  async function editImages(prompt) {
    const files = imageFiles();
    const referenced = referenceUrls.slice(0, 5);
    if (!files.length && !referenced.length) throw new Error('图像编辑需要至少上传或引用一张参考图');
    const model = (editModelSelect && editModelSelect.value) || 'gpt-image-2';
    const n = Math.min(Number((countSelect && countSelect.value) || 1), 2);
    const size = EDIT_SIZE;
    const quality = (qualitySelect && qualitySelect.value) || '1k';
    const sessionId = activeAppendSessionId();
    const body = new FormData();
    body.set('model', model);
    body.set('prompt', prompt);
    body.set('n', String(n));
    body.set('size', size);
    body.set('quality', quality);
    body.set('response_format', 'url');
    if (sessionId) body.set('session_id', sessionId);
    referenced.forEach((item) => body.append('reference_url', item.url));
    files.slice(0, Math.max(0, 5 - referenced.length)).forEach((file) => body.append('image', file));

    const res = await fetch(EDIT_ENDPOINT, {
      method: 'POST',
      headers: await webuiAuthHeaders(false),
      body,
    });
    if (!res.ok) throw new Error(await responseError(res));
    const payload = await res.json();
    return { images: normalizeImages(payload), model, size, quality: payload.quality || quality, session: payload.studio_session };
  }

  async function submit() {
    if (running) return;
    const prompt = String((promptInput && promptInput.value) || '').trim();
    if (!prompt) {
      toast('请输入提示词', 'error');
      if (promptInput) promptInput.focus();
      return;
    }
    setRunning(true);
    clearPromptInput();
    setStatus(mode === 'edit' ? '正在提交图像编辑任务...' : '正在提交文生图任务...', 'running');
    try {
      const result = mode === 'edit'
        ? await editImages(prompt)
        : await generateImages(prompt);
      if (!result.images.length) throw new Error('接口没有返回可显示图片');
      if (result.session) {
        selectedSessionId = String(result.session.id || '');
        await loadHistory(selectedSessionId);
      } else {
        addHistory({
          images: result.images,
          prompt,
          model: result.model,
          mode,
          size: result.size,
          quality: result.quality,
        });
      }
      setStatus(`完成，返回 ${result.images.length} 张图片`, 'completed');
      toast('图片任务完成', 'success');
      if (resultsViewport) resultsViewport.scrollTo({ top: resultsViewport.scrollHeight, behavior: 'smooth' });
    } catch (error) {
      restorePromptInput(prompt);
      const message = (error && error.message) || String(error);
      setStatus(message, 'failed');
      toast(message, 'error');
    } finally {
      setRunning(false);
    }
  }

  function startDraft() {
    draftMode = true;
    selectedSessionId = '';
    if (promptInput) {
      promptInput.value = '';
      promptInput.focus();
    }
    if (imageInput) imageInput.value = '';
    referenceUrls = [];
    setMode('generate');
    syncPromptCount();
    syncImageCount();
    syncReferencePreview();
    renderHistory();
    setStatus('新画图已就绪', 'idle');
  }

  async function boot() {
    if (typeof renderWebuiHeader === 'function') await renderWebuiHeader();
    if (typeof renderSiteFooter === 'function') await renderSiteFooter();
    if (!await ensureAccess()) return;
    renderHistory();
    syncPromptCount();
    syncImageCount();
    syncReferencePreview();
    setMode('generate');
    try {
      await loadModels();
      await loadHistory();
      consumePendingReference();
      if (!referenceCount()) setStatus('就绪', 'idle');
    } catch (error) {
      const message = (error && error.message) || String(error);
      setStatus(`模型加载失败: ${message}`, 'failed');
      toast(`模型加载失败: ${message}`, 'error');
    }
  }

  if (form) {
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      void submit();
    });
  }

  if (modeToggle) {
    modeToggle.addEventListener('click', (event) => {
      const button = event.target instanceof Element ? event.target.closest('button[data-mode]') : null;
      if (!(button instanceof HTMLButtonElement) || button.disabled) return;
      setMode(button.dataset.mode || 'generate');
    });
  }

  if (promptInput) {
    promptInput.addEventListener('input', syncPromptCount);
    promptInput.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' || event.shiftKey) return;
      event.preventDefault();
      void submit();
    });
    promptInput.addEventListener('paste', (event) => {
      const files = Array.from((event.clipboardData && event.clipboardData.files) || [])
        .filter((file) => file.type.startsWith('image/'));
      if (!files.length) return;
      event.preventDefault();
      appendImageFiles(files);
    });
  }

  if (pickImagesBtn) {
    pickImagesBtn.addEventListener('click', () => {
      if (!running && imageInput) imageInput.click();
    });
  }

  if (imageInput) {
    imageInput.addEventListener('change', () => {
      syncImageCount();
      syncReferencePreview();
      if (imageFiles().length) setMode('edit');
    });
  }

  if (qualitySelect) qualitySelect.addEventListener('change', syncQualityAccess);
  if (sizeSelect) {
    sizeSelect.addEventListener('change', () => {
      if (mode === 'generate') lastGenerateSize = sizeSelect.value || EDIT_SIZE;
      syncSizeAccess();
    });
  }

  if (referencePreview) {
    referencePreview.addEventListener('click', (event) => {
      const button = event.target instanceof Element ? event.target.closest('[data-remove-reference]') : null;
      if (!(button instanceof HTMLButtonElement)) return;
      const index = Number(button.dataset.removeReference);
      if (!Number.isInteger(index) || index < 0) return;
      referenceUrls.splice(index, 1);
      syncImageCount();
      syncReferencePreview();
      if (!referenceCount()) setMode('generate');
    });
  }

  if (newSessionBtn) {
    newSessionBtn.addEventListener('click', startDraft);
  }

  if (clearHistoryBtn) {
    clearHistoryBtn.addEventListener('click', async () => {
      try {
        const res = await fetch(HISTORY_ENDPOINT, {
          method: 'DELETE',
          headers: await webuiAuthHeaders(),
        });
        if (!res.ok) throw new Error(await responseError(res));
        history = [];
        selectedSessionId = '';
        draftMode = true;
        writeHistory();
        renderHistory();
        setStatus('历史已清空', 'idle');
      } catch (error) {
        const message = (error && error.message) || String(error);
        toast(message, 'error');
      }
    });
  }

  if (sessionList) {
    sessionList.addEventListener('click', (event) => {
      const button = event.target instanceof Element ? event.target.closest('[data-select-session]') : null;
      if (!(button instanceof HTMLButtonElement)) return;
      selectedSessionId = button.dataset.selectSession || '';
      draftMode = false;
      renderHistory();
      if (resultsViewport) resultsViewport.scrollTo({ top: 0 });
    });
  }

  if (output) {
    output.addEventListener('click', async (event) => {
      const referenceButton = event.target instanceof Element ? event.target.closest('[data-reference-image]') : null;
      if (referenceButton instanceof HTMLButtonElement) {
        addReferenceUrl(referenceButton.dataset.referenceImage || '', referenceButton.dataset.referenceName || '');
        return;
      }
      const button = event.target instanceof Element ? event.target.closest('[data-delete-session]') : null;
      if (!(button instanceof HTMLButtonElement)) return;
      const id = button.dataset.deleteSession || '';
      if (!id) return;
      try {
        const res = await fetch(`${HISTORY_ENDPOINT}/${encodeURIComponent(id)}`, {
          method: 'DELETE',
          headers: await webuiAuthHeaders(),
        });
        if (!res.ok) throw new Error(await responseError(res));
        selectedSessionId = '';
        await loadHistory();
        setStatus('历史会话已删除', 'idle');
      } catch (error) {
        const message = (error && error.message) || String(error);
        toast(message, 'error');
      }
    });
  }

  if (composer) {
    composer.addEventListener('dragenter', (event) => {
      const hasImage = Array.from((event.dataTransfer && event.dataTransfer.items) || [])
        .some((item) => item.kind === 'file' && item.type.startsWith('image/'));
      if (!hasImage) return;
      event.preventDefault();
      dragDepth += 1;
      composer.classList.add('is-dragging');
    });
    composer.addEventListener('dragover', (event) => {
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
    });
    composer.addEventListener('dragleave', (event) => {
      event.preventDefault();
      dragDepth = Math.max(0, dragDepth - 1);
      if (!dragDepth) composer.classList.remove('is-dragging');
    });
    composer.addEventListener('drop', (event) => {
      event.preventDefault();
      dragDepth = 0;
      composer.classList.remove('is-dragging');
      const files = Array.from((event.dataTransfer && event.dataTransfer.files) || [])
        .filter((file) => file.type.startsWith('image/'));
      if (files.length) appendImageFiles(files);
    });
  }

  window.addEventListener('beforeunload', revokeReferencePreviewUrls);

  boot().catch((error) => {
    console.error('image studio boot failed', error);
    toast('Image Studio 初始化失败', 'error');
  });
})();
