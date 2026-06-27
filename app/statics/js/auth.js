/* grokManager — Auth module */
const ADMIN_API = '/admin/api';
const WEBUI_API = '/webui/api';

const _ENC = new TextEncoder(), _DEC = new TextDecoder();
const _SECRET = 'grok2api-admin-key';
const _XOR_P = 'enc:xor:', _AES_P = 'enc:v1:';
const _WEBUI_AUTH_KEY = 'grok2api_webui_auth_v2';
const _WEBUI_LOGGED_OUT_KEY = 'grok2api_webui_logged_out_v1';
const _MEM_STORE = {};

function _toB64(b) { let s=''; b.forEach(v=>s+=String.fromCharCode(v)); return btoa(s); }
function _fromB64(s) { const d=atob(s), a=new Uint8Array(d.length); for(let i=0;i<d.length;i++) a[i]=d.charCodeAt(i); return a; }
function _xor(d,k) { const o=new Uint8Array(d.length); for(let i=0;i<d.length;i++) o[i]=d[i]^k[i%k.length]; return o; }
function _b64Utf8(s) { return btoa(unescape(encodeURIComponent(s))); }
function _b64UrlUtf8(s) { return _b64Utf8(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, ''); }
function _slug(s) { return String(s || 'user').trim().toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 32) || 'user'; }

async function _deriveKey(salt) {
  const subtle = _cryptoSubtle();
  const km = await subtle.importKey('raw',_ENC.encode(_SECRET),'PBKDF2',false,['deriveKey']);
  return subtle.deriveKey({name:'PBKDF2',salt,iterations:100000,hash:'SHA-256'},km,{name:'AES-GCM',length:256},false,['encrypt','decrypt']);
}

function _webCrypto() {
  if (typeof globalThis !== 'undefined' && globalThis.crypto) return globalThis.crypto;
  if (typeof window !== 'undefined' && window.crypto) return window.crypto;
  return null;
}

function _cryptoSubtle() {
  const cryptoObj = _webCrypto();
  return cryptoObj && cryptoObj.subtle ? cryptoObj.subtle : null;
}

async function _encrypt(plain) {
  if (!plain) return '';
  const cryptoObj = _webCrypto();
  const subtle = _cryptoSubtle();
  if (!cryptoObj || !subtle) return _XOR_P+_toB64(_xor(_ENC.encode(plain),_ENC.encode(_SECRET)));
  const salt=cryptoObj.getRandomValues(new Uint8Array(16)), iv=cryptoObj.getRandomValues(new Uint8Array(12));
  const key=await _deriveKey(salt), ct=await subtle.encrypt({name:'AES-GCM',iv},key,_ENC.encode(plain));
  return `${_AES_P}${_toB64(salt)}:${_toB64(iv)}:${_toB64(new Uint8Array(ct))}`;
}

async function _decrypt(s) {
  if (!s) return '';
  if (s.startsWith(_XOR_P)) return _DEC.decode(_xor(_fromB64(s.slice(_XOR_P.length)),_ENC.encode(_SECRET)));
  const subtle = _cryptoSubtle();
  if (!s.startsWith(_AES_P)||!subtle) return '';
  const p=s.split(':'); if(p.length!==5) return '';
  const key=await _deriveKey(_fromB64(p[2]));
  return _DEC.decode(await subtle.decrypt({name:'AES-GCM',iv:_fromB64(p[3])},key,_fromB64(p[4])));
}

function _storageList() {
  const stores = [];
  try { if (typeof sessionStorage !== 'undefined') stores.push(sessionStorage); } catch {}
  try { if (typeof localStorage !== 'undefined') stores.push(localStorage); } catch {}
  return stores;
}

function _storageGet(k) {
  const stores = _storageList();
  for (let i = 0; i < stores.length; i++) {
    try {
      const value = stores[i].getItem(k);
      if (value) return value;
    } catch {}
  }
  return Object.prototype.hasOwnProperty.call(_MEM_STORE, k) ? _MEM_STORE[k] : '';
}

function _storageSet(k, v) {
  let persisted = false;
  const stores = _storageList().reverse();
  for (let i = 0; i < stores.length; i++) {
    try {
      if (v) stores[i].setItem(k, v);
      else stores[i].removeItem(k);
      persisted = true;
    } catch {}
  }
  if (!persisted) {
    if (v) _MEM_STORE[k] = v;
    else delete _MEM_STORE[k];
  }
}

function _storageRemove(k) {
  _storageSet(k, '');
}

/* Key store factory */
function _keyStore(k) {
  return {
    get:   async()=>{ const s=_storageGet(k); if(!s)return''; try{return await _decrypt(s)}catch{_storageRemove(k);return''} },
    set:   async(v)=>{ if(!v){_storageRemove(k);return} _storageSet(k,await _encrypt(v)||'') },
    clear: ()=>_storageRemove(k),
  };
}

const adminKey = _keyStore('grok2api_admin_key');
const webuiKey = _keyStore('grok2api_webui_key');

function _webuiAuthHeader(username, password) {
  const user = String(username || '').trim();
  const pass = String(password || '');
  if (!pass) return {};
  if (user) return { Authorization: `Basic ${_b64Utf8(`${user}:${pass}`)}` };
  return { Authorization: `Bearer ${pass}` };
}

function _webuiFallbackUser(password) {
  return password
    ? { id: 'legacy', username: 'legacy', display_name: 'WebUI', legacy: true, anonymous: false }
    : { id: 'anonymous', username: 'anonymous', display_name: 'Anonymous', legacy: false, anonymous: true };
}

function _normalizeWebuiAuth(value) {
  const auth = value && typeof value === 'object' ? value : {};
  const username = String(auth.username || '').trim();
  const password = String(auth.password || auth.key || '');
  const rawUser = auth.user && typeof auth.user === 'object' ? auth.user : {};
  const rawStorageScope = rawUser.storage_scope || rawUser.storageScope || auth.storage_scope || auth.storageScope || '';
  const hasUserIdentity = Boolean(rawUser.id || rawUser.username || rawStorageScope || auth.user_id);
  const fallback = _webuiFallbackUser(password);
  const storageScope = String(rawStorageScope || rawUser.id || auth.user_id || (username ? _slug(username) : fallback.id)).trim() || fallback.id;
  const user = {
    id: String(rawUser.id || auth.user_id || storageScope).trim() || fallback.id,
    username: String(rawUser.username || username || fallback.username).trim() || fallback.username,
    display_name: String(rawUser.display_name || rawUser.displayName || username || fallback.display_name).trim() || fallback.display_name,
    allow_nsfw: rawUser.allow_nsfw !== false && rawUser.allowNsfw !== false,
    legacy: Boolean(rawUser.legacy || (!hasUserIdentity && !username && password)),
    anonymous: Boolean(rawUser.anonymous || (!hasUserIdentity && !username && !password)),
    storage_scope: storageScope,
  };
  return { username, password, user, storage_scope: storageScope };
}

const webuiAuth = {
  get: async () => {
    const raw = await _keyStore(_WEBUI_AUTH_KEY).get();
    if (raw) {
      try { return _normalizeWebuiAuth(JSON.parse(raw)); } catch {}
    }
    const legacy = await webuiKey.get();
    return _normalizeWebuiAuth({ username: '', password: legacy });
  },
  set: async (username, password, user) => {
    const normalized = _normalizeWebuiAuth({ username, password, user });
    await _keyStore(_WEBUI_AUTH_KEY).set(JSON.stringify(normalized));
    if (normalized.username) webuiKey.clear();
    else await webuiKey.set(normalized.password);
  },
  clear: () => {
    _storageRemove(_WEBUI_AUTH_KEY);
    webuiKey.clear();
  },
};

function webuiMarkLoggedOut() {
  _storageSet(_WEBUI_LOGGED_OUT_KEY, '1');
}

function webuiClearLoggedOut() {
  _storageRemove(_WEBUI_LOGGED_OUT_KEY);
}

function webuiWasLoggedOut() {
  return _storageGet(_WEBUI_LOGGED_OUT_KEY) === '1';
}

async function verifyKey(url, key) {
  return (await fetch(url, { headers: key ? { Authorization: `Bearer ${key}` } : {} })).ok;
}

async function verifyWebuiAccess(url, username, password, options = {}) {
  const authOnly = Boolean(options.authOnly);
  const loginIntent = Boolean(options.loginIntent);
  const headers = {
    ..._webuiAuthHeader(username, password),
    ...(authOnly ? { 'X-WebUI-Auth-Only': '1' } : {}),
    ...(loginIntent ? { 'X-WebUI-Login-Intent': '1' } : {}),
  };
  const res = await fetch(url, { headers, cache: 'no-store' });
  if (!res.ok) return null;
  const data = await res.json().catch(() => ({}));
  return data && data.user ? data.user : _webuiFallbackUser(password);
}

async function verifyStoredWebuiAccess(url, options = {}) {
  if (webuiWasLoggedOut()) return false;
  const auth = await webuiAuth.get();
  const user = await verifyWebuiAccess(url, auth.username, auth.password, { authOnly: Boolean(options.authOnly) });
  if (!user) {
    if (auth.password || auth.username) webuiAuth.clear();
    return false;
  }
  await webuiAuth.set(auth.username, auth.password, user);
  return true;
}

async function webuiAuthHeaders(contentType = false) {
  const auth = await webuiAuth.get();
  return {
    ...(contentType ? { 'Content-Type': 'application/json' } : {}),
    ..._webuiAuthHeader(auth.username, auth.password),
  };
}

async function webuiSocketToken() {
  const auth = await webuiAuth.get();
  if (auth.username) return `basic:${_b64UrlUtf8(`${auth.username}:${auth.password}`)}`;
  return auth.password || '';
}

async function webuiStorageScope() {
  const auth = await webuiAuth.get();
  return auth.storage_scope || (auth.user && (auth.user.storage_scope || auth.user.storageScope || auth.user.id)) || 'anonymous';
}

function webuiStorageScopeSuffix(value) {
  return String(value || 'anonymous')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 64) || 'anonymous';
}

async function webuiScopedStorageKey(baseKey) {
  return `${baseKey}.${webuiStorageScopeSuffix(await webuiStorageScope())}`;
}

function adminLogout() { adminKey.clear(); webuiAuth.clear(); location.href='/admin/login'; }
function webuiLogout() { webuiAuth.clear(); webuiMarkLoggedOut(); location.href='/webui/logout'; }
