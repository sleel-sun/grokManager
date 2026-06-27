(() => {
  const VERIFY_ENDPOINT = '/webui/api/verify';
  const MODELS_ENDPOINT = '/webui/api/images/models';
  const GENERATE_ENDPOINT = '/webui/api/images/generations';
  const EDIT_ENDPOINT = '/webui/api/images/edits';
  const HISTORY_KEY = 'grokmanager.image_studio.history.v1';
  const HISTORY_LIMIT = 24;

  const form = document.getElementById('studioForm');
  const modeToggle = document.getElementById('studioModeToggle');
  const promptInput = document.getElementById('studioPrompt');
  const promptCount = document.getElementById('studioPromptCount');
  const modelSelect = document.getElementById('studioModel');
  const editModelSelect = document.getElementById('studioEditModel');
  const generateModelField = document.getElementById('studioGenerateModelField');
  const editModelField = document.getElementById('studioEditModelField');
  const imageField = document.getElementById('studioImageField');
  const imageInput = document.getElementById('studioImages');
  const imageCount = document.getElementById('studioImageCount');
  const sizeSelect = document.getElementById('studioSize');
  const countSelect = document.getElementById('studioCount');
  const submitBtn = document.getElementById('studioSubmit');
  const statusEl = document.getElementById('studioStatus');
  const output = document.getElementById('studioOutput');
  const empty = document.getElementById('studioEmpty');
  const clearHistoryBtn = document.getElementById('studioClearHistory');
  const modelCount = document.getElementById('studioModelCount');
  const modelHint = document.getElementById('studioModelHint');

  let mode = 'generate';
  let running = false;
  let history = readHistory();

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

  function addHistory({ images, prompt, model, mode, size }) {
    const now = Date.now();
    const items = images.map((url, index) => ({
      id: `${now}-${index}`,
      url,
      prompt,
      model,
      mode,
      size,
      created_at: now,
    }));
    history = [...items, ...history].slice(0, HISTORY_LIMIT);
    writeHistory();
    renderHistory();
  }

  function formatDate(value) {
    const date = new Date(value || Date.now());
    if (Number.isNaN(date.getTime())) return '';
    return date.toLocaleString(undefined, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
  }

  function renderHistory() {
    if (!output || !empty) return;
    empty.hidden = history.length > 0;
    empty.style.display = history.length > 0 ? 'none' : '';
    output.innerHTML = history.map((item) => {
      const title = item.prompt || 'Untitled';
      const meta = `${item.model || 'model'} · ${item.size || 'size'} · ${formatDate(item.created_at)}`;
      return `<article class="studio-output">
        <a href="${escapeHtml(item.url)}" target="_blank" rel="noopener">
          <img src="${escapeHtml(item.url)}" alt="${escapeHtml(title)}" loading="lazy">
        </a>
        <div class="studio-output-body">
          <div class="studio-output-title" title="${escapeHtml(title)}">${escapeHtml(title)}</div>
          <div class="studio-output-meta" title="${escapeHtml(meta)}">${escapeHtml(meta)}</div>
        </div>
      </article>`;
    }).join('');
  }

  function setRunning(next) {
    running = next;
    if (submitBtn) {
      submitBtn.disabled = next;
      submitBtn.textContent = next
        ? (mode === 'edit' ? '正在编辑...' : '正在生成...')
        : (mode === 'edit' ? '开始编辑' : '开始生成');
    }
    [
      promptInput,
      modelSelect,
      editModelSelect,
      imageInput,
      sizeSelect,
      countSelect,
    ].forEach((el) => {
      if (el) el.disabled = next;
    });
    if (modeToggle) {
      modeToggle.querySelectorAll('button').forEach((button) => {
        button.disabled = next;
      });
    }
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
    if (imageField) imageField.classList.toggle('studio-hidden', mode !== 'edit');
    if (countSelect) {
      Array.from(countSelect.options).forEach((option) => {
        option.disabled = mode === 'edit' && Number(option.value) > 2;
      });
      if (mode === 'edit' && Number(countSelect.value) > 2) countSelect.value = '2';
    }
    setRunning(false);
  }

  function syncPromptCount() {
    if (!promptCount) return;
    promptCount.textContent = `${String((promptInput && promptInput.value) || '').trim().length} 字`;
  }

  function syncImageCount() {
    if (!imageCount) return;
    const count = imageInput && imageInput.files ? imageInput.files.length : 0;
    imageCount.textContent = count ? `${count} 张` : '未选择';
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
    const edits = Array.isArray(payload.edits) ? payload.edits : [];

    const gptFirst = [...generation].sort((a, b) => {
      const ag = String(a.id || '').startsWith('gpt-image') ? 0 : 1;
      const bg = String(b.id || '').startsWith('gpt-image') ? 0 : 1;
      return ag - bg;
    });
    if (modelSelect) {
      modelSelect.innerHTML = gptFirst.map(optionHtml).join('');
      const preferred = gptFirst.find((item) => item.id === 'gpt-image-2') || gptFirst[0];
      if (preferred) modelSelect.value = preferred.id;
    }
    if (editModelSelect) {
      editModelSelect.innerHTML = edits.map(optionHtml).join('');
      if (edits[0]) editModelSelect.value = edits[0].id;
    }
    const total = generation.length + edits.length;
    if (modelCount) modelCount.textContent = String(total);
    if (modelHint) modelHint.textContent = `可用文生图 ${generation.length} 个，编辑 ${edits.length} 个`;
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
    const res = await fetch(GENERATE_ENDPOINT, {
      method: 'POST',
      headers: await webuiAuthHeaders(true),
      body: JSON.stringify({
        model,
        prompt,
        n,
        size,
        response_format: 'url',
      }),
    });
    if (!res.ok) throw new Error(await responseError(res));
    const payload = await res.json();
    return { images: normalizeImages(payload), model, size };
  }

  async function editImages(prompt) {
    const files = Array.from((imageInput && imageInput.files) || []);
    if (!files.length) throw new Error('图像编辑需要至少上传一张参考图');
    const model = (editModelSelect && editModelSelect.value) || 'grok-imagine-image-edit';
    const n = Math.min(Number((countSelect && countSelect.value) || 1), 2);
    const size = (sizeSelect && sizeSelect.value) || '1024x1024';
    const body = new FormData();
    body.set('model', model);
    body.set('prompt', prompt);
    body.set('n', String(n));
    body.set('size', size);
    body.set('response_format', 'url');
    files.slice(0, 5).forEach((file) => body.append('image', file));

    const res = await fetch(EDIT_ENDPOINT, {
      method: 'POST',
      headers: await webuiAuthHeaders(false),
      body,
    });
    if (!res.ok) throw new Error(await responseError(res));
    const payload = await res.json();
    return { images: normalizeImages(payload), model, size };
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
    setStatus(mode === 'edit' ? '正在提交图像编辑任务...' : '正在提交文生图任务...', 'running');
    try {
      const result = mode === 'edit'
        ? await editImages(prompt)
        : await generateImages(prompt);
      if (!result.images.length) throw new Error('接口没有返回可显示图片');
      addHistory({
        images: result.images,
        prompt,
        model: result.model,
        mode,
        size: result.size,
      });
      setStatus(`完成，返回 ${result.images.length} 张图片`, 'completed');
      toast('图片任务完成', 'success');
    } catch (error) {
      const message = (error && error.message) || String(error);
      setStatus(message, 'failed');
      toast(message, 'error');
    } finally {
      setRunning(false);
    }
  }

  async function boot() {
    if (typeof renderWebuiHeader === 'function') await renderWebuiHeader();
    if (typeof renderSiteFooter === 'function') await renderSiteFooter();
    if (!await ensureAccess()) return;
    renderHistory();
    syncPromptCount();
    syncImageCount();
    setMode('generate');
    try {
      await loadModels();
      setStatus('就绪', 'idle');
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

  if (promptInput) promptInput.addEventListener('input', syncPromptCount);
  if (imageInput) imageInput.addEventListener('change', syncImageCount);
  if (clearHistoryBtn) {
    clearHistoryBtn.addEventListener('click', () => {
      history = [];
      writeHistory();
      renderHistory();
      setStatus('历史已清空', 'idle');
    });
  }

  boot().catch((error) => {
    console.error('image studio boot failed', error);
    toast('Image Studio 初始化失败', 'error');
  });
})();
