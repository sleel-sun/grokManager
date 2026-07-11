(function () {
  const API = '/admin/api/webui/users';
  const GPT_MODELS = ['gpt-image-1', 'gpt-image-2', 'codex-gpt-image-2'];
  const state = {
    users: [],
    selected: new Set(),
    search: '',
    filter: 'all',
    meta: { webui_enabled: false, legacy_key_configured: false, summary: {} },
    dirty: false,
  };

  const $ = (id) => document.getElementById(id);

  function tx(key, fallback, params) {
    if (typeof window.t !== 'function') return fallback;
    const value = t(key, params || null);
    return value === key ? fallback : value;
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function randomSecret() {
    const cryptoObj = globalThis.crypto || window.crypto;
    if (cryptoObj && typeof cryptoObj.randomUUID === 'function') {
      return cryptoObj.randomUUID().replace(/-/g, '').slice(0, 28);
    }
    const alphabet = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
    let value = '';
    for (let i = 0; i < 28; i += 1) value += alphabet[Math.floor(Math.random() * alphabet.length)];
    return value;
  }

  function makeId() {
    return `u-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  }

  function normalizeQuality(value) {
    const text = String(value || '1k').trim().toLowerCase();
    if (['4', '4k', '4096', 'premium', 'pro'].includes(text)) return '4k';
    if (['2', '2k', '2048'].includes(text)) return '2k';
    return '1k';
  }

  function normalizeBool(value, fallback) {
    if (value == null) return fallback;
    if (typeof value === 'boolean') return value;
    const text = String(value).trim().toLowerCase();
    if (!text) return fallback;
    if (['1', 'true', 'yes', 'on', 'enabled', 'allow'].includes(text)) return true;
    if (['0', 'false', 'no', 'off', 'disabled', 'deny', 'blocked'].includes(text)) return false;
    return Boolean(value);
  }

  function normalizeModels(value) {
    if (Array.isArray(value)) return value.map((item) => String(item || '').trim()).filter(Boolean);
    if (typeof value === 'string') return value.split(/[\n,]/).map((item) => item.trim()).filter(Boolean);
    return [];
  }

  function normalizeQuota(value) {
    const parsed = Number.parseInt(value, 10);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
  }

  function firstDefined() {
    for (let i = 0; i < arguments.length; i += 1) {
      if (arguments[i] !== undefined && arguments[i] !== null) return arguments[i];
    }
    return undefined;
  }

  function normalizeUser(entry, index) {
    const raw = entry && typeof entry === 'object' ? entry : {};
    const username = String(raw.username || raw.name || raw.id || '').trim();
    const key = String(raw.key || raw.password || raw.token || '').trim();
    if (!username || !key) return null;
    const apiKey = String(firstDefined(raw.api_key, raw.apiKey, raw.openai_api_key, raw.openaiApiKey, raw.api_call_key, raw.apiCallKey) || '').trim();
    const gptEnabledRaw = firstDefined(raw.gpt_enabled, raw.gptEnabled, raw.allow_gpt, raw.allowGpt);
    const models = normalizeModels(firstDefined(raw.gpt_models, raw.gptModels, raw.gpt_image_models, raw.allowed_gpt_models));
    const gptEnabled = gptEnabledRaw == null ? models.length > 0 : normalizeBool(gptEnabledRaw, false);
    return {
      _id: raw._id || makeId(),
      username,
      key,
      api_key: apiKey,
      display_name: String(raw.display_name || raw.displayName || username).trim(),
      enabled: normalizeBool(raw.enabled, true),
      allow_nsfw: normalizeBool(firstDefined(raw.allow_nsfw, raw.allowNsfw, raw.nsfw, raw.enable_nsfw), true),
      gpt_enabled: Boolean(gptEnabled),
      gpt_models: gptEnabled ? GPT_MODELS.slice() : [],
      gpt_image_quality: normalizeQuality(firstDefined(raw.gpt_image_quality, raw.gptImageQuality, raw.gpt_quality)),
      grok_daily_quota: normalizeQuota(firstDefined(raw.grok_daily_quota, raw.grokDailyQuota, raw.grok_quota, raw.grokQuota)),
      gpt_daily_quota: normalizeQuota(firstDefined(raw.gpt_daily_quota, raw.gptDailyQuota, raw.gpt_quota, raw.gptQuota)),
      quota_usage: raw.quota_usage && typeof raw.quota_usage === 'object' ? raw.quota_usage : {},
      _order: Number.isFinite(index) ? index : 0,
    };
  }

  function payloadUsers() {
    return state.users.map((user) => ({
      username: user.username.trim(),
      key: user.key.trim(),
      api_key: String(user.api_key || '').trim(),
      display_name: (user.display_name || user.username).trim(),
      enabled: user.enabled !== false,
      allow_nsfw: user.allow_nsfw !== false,
      gpt_enabled: Boolean(user.gpt_enabled),
      gpt_models: user.gpt_enabled ? GPT_MODELS.slice() : [],
      gpt_image_quality: normalizeQuality(user.gpt_image_quality),
      grok_daily_quota: normalizeQuota(user.grok_daily_quota),
      gpt_daily_quota: normalizeQuota(user.gpt_daily_quota),
    }));
  }

  async function api(method, path, body) {
    const key = await adminKey.get();
    if (!key) {
      location.href = '/admin/login';
      throw new Error('missing admin key');
    }
    const res = await fetch(API + path, {
      method,
      headers: {
        Authorization: `Bearer ${key}`,
        ...(body == null ? {} : { 'Content-Type': 'application/json' }),
      },
      body: body == null ? undefined : JSON.stringify(body),
      cache: 'no-store',
    });
    if (res.status === 401 || res.status === 403) {
      adminLogout();
      throw new Error(tx('common.invalidKey', 'Invalid key'));
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || data.message || `HTTP ${res.status}`);
    return data;
  }

  function setUsersFromResponse(data) {
    state.meta = {
      webui_enabled: Boolean(data.webui_enabled),
      legacy_key_configured: Boolean(data.legacy_key_configured),
      summary: data.summary || {},
    };
    state.users = (Array.isArray(data.users) ? data.users : [])
      .map((user, index) => normalizeUser(user, index))
      .filter(Boolean);
    state.selected.clear();
    state.dirty = false;
    render();
  }

  async function loadUsers() {
    $('reloadBtn').disabled = true;
    try {
      setUsersFromResponse(await api('GET', '', null));
    } catch (err) {
      showToast(`${tx('users.loadFailed', 'Load failed')}: ${err.message}`, 'error');
    } finally {
      $('reloadBtn').disabled = false;
    }
  }

  function validateUsers(users) {
    const seen = new Set();
    const apiKeys = new Set();
    for (const user of users) {
      const username = String(user.username || '').trim();
      const key = String(user.key || '').trim();
      const apiKey = String(user.api_key || '').trim();
      if (!username) throw new Error(tx('users.validation.username', 'Username is required'));
      if (!key) throw new Error(tx('users.validation.key', 'Access key is required'));
      if (/[\s/:=]/.test(username) || username.length > 64) {
        throw new Error(tx('users.validation.usernameFormat', 'Username cannot contain whitespace, /, :, or ='));
      }
      const lowered = username.toLowerCase();
      if (seen.has(lowered)) throw new Error(tx('users.validation.duplicate', 'Duplicate username: {name}', { name: username }));
      seen.add(lowered);
      if (apiKey) {
        if (apiKeys.has(apiKey)) throw new Error(tx('users.validation.duplicateApiKey', 'Duplicate API key'));
        apiKeys.add(apiKey);
      }
    }
  }

  async function saveUsers() {
    try {
      const users = payloadUsers();
      validateUsers(users);
      $('saveBtn').disabled = true;
      const data = await api('PUT', '', { users });
      setUsersFromResponse(data);
      showToast(tx('users.saveDone', 'Saved'), 'success');
    } catch (err) {
      showToast(`${tx('users.saveFailed', 'Save failed')}: ${err.message}`, 'error');
    } finally {
      $('saveBtn').disabled = false;
    }
  }

  function summary() {
    const users = state.users;
    return {
      total: users.length,
      enabled: users.filter((user) => user.enabled !== false).length,
      disabled: users.filter((user) => user.enabled === false).length,
      nsfw_allowed: users.filter((user) => user.allow_nsfw !== false).length,
      gpt_enabled: users.filter((user) => user.gpt_enabled === true).length,
      grok_used: users.reduce((total, user) => total + Number((user.quota_usage.grok || {}).used || 0), 0),
      gpt_used: users.reduce((total, user) => total + Number((user.quota_usage.gpt || {}).used || 0), 0),
    };
  }

  function renderSummary() {
    const stats = summary();
    $('statTotal').textContent = stats.total;
    $('statEnabled').textContent = stats.enabled;
    $('statDisabled').textContent = stats.disabled;
    $('statNsfw').textContent = stats.nsfw_allowed;
    $('statGpt').textContent = stats.gpt_enabled;
    $('statGrokUsage').textContent = stats.grok_used;
    $('statGptUsage').textContent = stats.gpt_used;

    const mode = state.meta.webui_enabled
      ? tx('users.mode.enabled', 'WebUI enabled')
      : tx('users.mode.disabled', 'WebUI disabled');
    const loginMode = state.users.length
      ? tx('users.mode.multiUser', 'Multi-user mode')
      : state.meta.legacy_key_configured
        ? tx('users.mode.singleKey', 'Single-password mode')
        : tx('users.mode.anonymous', 'Anonymous mode');
    $('modeStatus').innerHTML = `
      <span>${escapeHtml(tx('users.mode.current', 'Current mode'))}: <strong>${escapeHtml(loginMode)}</strong></span>
      <span class="user-status-pill">${escapeHtml(mode)}</span>`;
  }

  function filteredUsers() {
    const needle = state.search.trim().toLowerCase();
    return state.users.filter((user) => {
      const text = `${user.username} ${user.display_name}`.toLowerCase();
      if (needle && !text.includes(needle)) return false;
      if (state.filter === 'enabled') return user.enabled !== false;
      if (state.filter === 'disabled') return user.enabled === false;
      if (state.filter === 'gpt') return user.gpt_enabled === true;
      if (state.filter === 'nsfwBlocked') return user.allow_nsfw === false;
      return true;
    });
  }

  function markDirty() {
    state.dirty = true;
    $('saveBtn').disabled = false;
    renderSummary();
  }

  function updateUser(id, patch) {
    const user = state.users.find((item) => item._id === id);
    if (!user) return;
    Object.assign(user, patch);
    if ('gpt_enabled' in patch) user.gpt_models = patch.gpt_enabled ? GPT_MODELS.slice() : [];
    markDirty();
  }

  function removeUsers(ids) {
    const targets = new Set(ids);
    state.users = state.users.filter((user) => !targets.has(user._id));
    targets.forEach((id) => state.selected.delete(id));
    markDirty();
    render();
  }

  function formatQuotaCell(user, bucket) {
    const quota = user.quota_usage && user.quota_usage[bucket] ? user.quota_usage[bucket] : {};
    const used = Number(quota.used || 0);
    const limit = Number(quota.limit || 0);
    if (limit <= 0) return `${used} / ${tx('users.unlimited', '不限')}`;
    const remaining = Math.max(0, limit - used);
    return `${used} / ${limit} (${tx('users.remaining', '剩余')} ${remaining})`;
  }

  function renderRows() {
    const body = $('usersBody');
    const users = filteredUsers();
    body.replaceChildren();
    $('emptyState').hidden = state.users.length > 0;
    if (!users.length) {
      $('selectAll').checked = false;
      $('selectAll').indeterminate = false;
      return;
    }

    for (const user of users) {
      const tr = document.createElement('tr');
      tr.dataset.id = user._id;
      tr.innerHTML = `
        <td><input type="checkbox" class="cb row-select" ${state.selected.has(user._id) ? 'checked' : ''} aria-label="Select ${escapeHtml(user.username)}"></td>
        <td><input class="cell-input user-username" value="${escapeHtml(user.username)}" autocomplete="off"></td>
        <td><input class="cell-input cell-key user-key" value="${escapeHtml(user.key)}" autocomplete="new-password"></td>
        <td>
          <div class="cell-input-action">
            <input class="cell-input cell-key user-api-key" value="${escapeHtml(user.api_key || '')}" autocomplete="new-password" placeholder="${escapeHtml(tx('users.apiKeyPlaceholder', '留空则禁用 API 调用'))}">
            <button type="button" class="icon-btn regen-api-key" title="${escapeHtml(tx('users.regenerateApiKey', '随机生成 API 调用密钥'))}"><svg viewBox="0 0 24 24"><path d="M21 12a9 9 0 0 1-15.5 6.2"/><path d="M3 12A9 9 0 0 1 18.5 5.8"/><path d="M3 17v5h5"/><path d="M21 7V2h-5"/></svg></button>
          </div>
        </td>
        <td><input class="cell-input user-display" value="${escapeHtml(user.display_name || '')}" autocomplete="off"></td>
        <td><label class="toggle-label"><input type="checkbox" class="user-enabled" ${user.enabled !== false ? 'checked' : ''}>${escapeHtml(tx('users.enabledShort', '启用'))}</label></td>
        <td><label class="toggle-label"><input type="checkbox" class="user-nsfw" ${user.allow_nsfw !== false ? 'checked' : ''}>${escapeHtml(tx('users.allowedShort', '允许'))}</label></td>
        <td><label class="toggle-label"><input type="checkbox" class="user-gpt" ${user.gpt_enabled ? 'checked' : ''}>${escapeHtml(tx('users.allowedShort', '允许'))}</label></td>
        <td>
          <select class="cell-select user-quality">
            <option value="1k" ${user.gpt_image_quality === '1k' ? 'selected' : ''}>1K</option>
            <option value="2k" ${user.gpt_image_quality === '2k' ? 'selected' : ''}>2K</option>
            <option value="4k" ${user.gpt_image_quality === '4k' ? 'selected' : ''}>4K</option>
          </select>
        </td>
        <td><input class="cell-input user-grok-quota" type="number" min="0" step="1" value="${escapeHtml(user.grok_daily_quota || 0)}"></td>
        <td><input class="cell-input user-gpt-quota" type="number" min="0" step="1" value="${escapeHtml(user.gpt_daily_quota || 0)}"></td>
        <td>
          <div class="text-xs text-muted">Grok ${escapeHtml(formatQuotaCell(user, 'grok'))}</div>
          <div class="text-xs text-muted">GPT ${escapeHtml(formatQuotaCell(user, 'gpt'))}</div>
        </td>
        <td>
          <div class="row-actions">
            <button type="button" class="icon-btn copy-key" title="${escapeHtml(tx('users.copyKey', '复制 Key'))}"><svg viewBox="0 0 24 24"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button>
            <button type="button" class="icon-btn regen-key" title="${escapeHtml(tx('users.regenerateKey', '重置 Key'))}"><svg viewBox="0 0 24 24"><path d="M21 12a9 9 0 0 1-15.5 6.2"/><path d="M3 12A9 9 0 0 1 18.5 5.8"/><path d="M3 17v5h5"/><path d="M21 7V2h-5"/></svg></button>
            <button type="button" class="icon-btn icon-btn-danger delete-user" title="${escapeHtml(tx('users.delete', '删除'))}"><svg viewBox="0 0 24 24"><path d="M5 7h14"/><path d="M9 7V5h6v2"/><path d="M8 7l1 12h6l1-12"/></svg></button>
          </div>
        </td>`;

      tr.querySelector('.row-select').addEventListener('change', (event) => {
        if (event.target.checked) state.selected.add(user._id);
        else state.selected.delete(user._id);
        renderSelectionState();
      });
      tr.querySelector('.user-username').addEventListener('input', (event) => updateUser(user._id, { username: event.target.value }));
      tr.querySelector('.user-key').addEventListener('input', (event) => updateUser(user._id, { key: event.target.value }));
      tr.querySelector('.user-api-key').addEventListener('input', (event) => updateUser(user._id, { api_key: event.target.value }));
      tr.querySelector('.user-display').addEventListener('input', (event) => updateUser(user._id, { display_name: event.target.value }));
      tr.querySelector('.user-enabled').addEventListener('change', (event) => updateUser(user._id, { enabled: event.target.checked }));
      tr.querySelector('.user-nsfw').addEventListener('change', (event) => updateUser(user._id, { allow_nsfw: event.target.checked }));
      tr.querySelector('.user-gpt').addEventListener('change', (event) => updateUser(user._id, { gpt_enabled: event.target.checked }));
      tr.querySelector('.user-quality').addEventListener('change', (event) => updateUser(user._id, { gpt_image_quality: event.target.value }));
      tr.querySelector('.user-grok-quota').addEventListener('input', (event) => updateUser(user._id, { grok_daily_quota: normalizeQuota(event.target.value) }));
      tr.querySelector('.user-gpt-quota').addEventListener('input', (event) => updateUser(user._id, { gpt_daily_quota: normalizeQuota(event.target.value) }));
      tr.querySelector('.delete-user').addEventListener('click', () => removeUsers([user._id]));
      tr.querySelector('.regen-key').addEventListener('click', () => {
        updateUser(user._id, { key: randomSecret() });
        renderRows();
      });
      tr.querySelector('.regen-api-key').addEventListener('click', () => {
        updateUser(user._id, { api_key: randomSecret() });
        renderRows();
      });
      tr.querySelector('.copy-key').addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(user.key);
          showToast(tx('users.copyDone', 'Copied'), 'success');
        } catch {
          showToast(tx('users.copyFailed', 'Copy failed'), 'error');
        }
      });
      body.appendChild(tr);
    }
    renderSelectionState();
  }

  function renderSelectionState() {
    const users = filteredUsers();
    const visibleIds = users.map((user) => user._id);
    const selectedVisible = visibleIds.filter((id) => state.selected.has(id));
    $('selectAll').checked = visibleIds.length > 0 && selectedVisible.length === visibleIds.length;
    $('selectAll').indeterminate = selectedVisible.length > 0 && selectedVisible.length < visibleIds.length;
    const hasSelection = state.selected.size > 0;
    $('enableSelectedBtn').disabled = !hasSelection;
    $('disableSelectedBtn').disabled = !hasSelection;
    $('deleteSelectedBtn').disabled = !hasSelection;
  }

  function render() {
    renderSummary();
    renderRows();
    $('saveBtn').disabled = !state.dirty;
  }

  function addUser() {
    const index = state.users.length + 1;
    state.users.push({
      _id: makeId(),
      username: `user${index}`,
      key: randomSecret(),
      api_key: randomSecret(),
      display_name: `User ${index}`,
      enabled: true,
      allow_nsfw: true,
      gpt_enabled: false,
      gpt_models: [],
      gpt_image_quality: '1k',
      grok_daily_quota: 0,
      gpt_daily_quota: 0,
      quota_usage: {},
    });
    markDirty();
    render();
  }

  function setSelectedEnabled(enabled) {
    state.users.forEach((user) => {
      if (state.selected.has(user._id)) user.enabled = enabled;
    });
    markDirty();
    render();
  }

  function deleteSelected() {
    if (!state.selected.size) return;
    const confirmed = window.confirm(tx('users.deleteConfirm', 'Delete selected users?'));
    if (!confirmed) return;
    removeUsers(Array.from(state.selected));
  }

  function parseImportText(text) {
    const raw = String(text || '').trim();
    if (!raw) return [];
    let value;
    if (raw[0] === '[' || raw[0] === '{') {
      value = JSON.parse(raw);
    } else {
      value = raw.split(/\r?\n/).map((line) => {
        const trimmed = line.trim();
        if (!trimmed || trimmed.startsWith('#')) return null;
        let index = trimmed.indexOf('=');
        if (index < 0) index = trimmed.indexOf(':');
        if (index < 0) return null;
        return { username: trimmed.slice(0, index).trim(), key: trimmed.slice(index + 1).trim() };
      }).filter(Boolean);
    }
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      if (Array.isArray(value.users)) value = value.users;
      else if (Array.isArray(value.webui_users)) value = value.webui_users;
      else value = Object.entries(value).map(([username, key]) => ({ username, key }));
    }
    if (!Array.isArray(value)) return [];
    const users = value.map((entry, index) => normalizeUser(entry, index)).filter(Boolean);
    validateUsers(users);
    return users;
  }

  function importUsers(replace) {
    try {
      const users = parseImportText($('importText').value);
      if (!users.length) throw new Error(tx('users.importEmpty', 'No valid users found'));
      if (replace) {
        state.users = users;
      } else {
        const seen = new Set(state.users.map((user) => user.username.toLowerCase()));
        users.forEach((user) => {
          if (!seen.has(user.username.toLowerCase())) {
            seen.add(user.username.toLowerCase());
            state.users.push(user);
          }
        });
      }
      state.selected.clear();
      markDirty();
      render();
      showToast(tx('users.importDone', 'Import complete'), 'success');
    } catch (err) {
      showToast(`${tx('users.importFailed', 'Import failed')}: ${err.message}`, 'error');
    }
  }

  function exportUsers() {
    const blob = new Blob([JSON.stringify(payloadUsers(), null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'webui-users.json';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  function bindEvents() {
    $('reloadBtn').addEventListener('click', loadUsers);
    $('saveBtn').addEventListener('click', saveUsers);
    $('addBtn').addEventListener('click', addUser);
    $('exportBtn').addEventListener('click', exportUsers);
    $('importToggleBtn').addEventListener('click', () => $('importPanel').classList.toggle('open'));
    $('importAppendBtn').addEventListener('click', () => importUsers(false));
    $('importReplaceBtn').addEventListener('click', () => importUsers(true));
    $('enableSelectedBtn').addEventListener('click', () => setSelectedEnabled(true));
    $('disableSelectedBtn').addEventListener('click', () => setSelectedEnabled(false));
    $('deleteSelectedBtn').addEventListener('click', deleteSelected);
    $('searchInput').addEventListener('input', (event) => {
      state.search = event.target.value;
      renderRows();
    });
    $('statusFilter').addEventListener('change', (event) => {
      state.filter = event.target.value;
      renderRows();
    });
    $('selectAll').addEventListener('change', (event) => {
      const users = filteredUsers();
      users.forEach((user) => {
        if (event.target.checked) state.selected.add(user._id);
        else state.selected.delete(user._id);
      });
      renderRows();
    });
    window.addEventListener('beforeunload', (event) => {
      if (!state.dirty) return;
      event.preventDefault();
      event.returnValue = '';
    });
  }

  function init() {
    if (window.renderAdminHeader) window.renderAdminHeader();
    if (window.renderSiteFooter) window.renderSiteFooter();
    bindEvents();
    renderSelectionState();
    loadUsers();
  }

  if (window.I18n && typeof I18n.onReady === 'function') I18n.onReady(init);
  else if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
