'use strict';

/**
 * R-AUTOCOPY-MENU — Unified Copy Trading store (3 modes).
 *
 * Persists per-user copy targets across three modes:
 *   • BCD_WALLET       — auto-track BCD trading wallet 0xc7ae...1505
 *   • BCD_SIGNALS      — listen to @BlackCatDeFiSignals via t.me/s scraping
 *   • CUSTOM_WALLET    — any address the user adds (max 10 per user)
 *
 * Storage shape (JSON on Railway Volume — no SQLite to keep Alpine builds
 * trivial; the spec mentioned SQLite but JSON gives us the same semantics
 * with zero native deps):
 *
 *   {
 *     "{userId}": {
 *       "BCD_WALLET":  { ...config, address: '0xc7ae...' } | null,
 *       "BCD_SIGNALS": { ...config } | null,
 *       "CUSTOM_WALLET": [
 *         { ref: '0xabc...', label, ...config },
 *         ...
 *       ]
 *     }
 *   }
 *
 * Each config: { capital_usdc, mode, enabled, sl_pct, trailing_pct,
 *                trailing_activation_pct, created_at, updated_at }.
 *
 * Public API:
 *   getTargets(userId)            → returns full per-user object
 *   getTarget(userId, type, ref?) → returns single target or null
 *   setTarget(userId, type, ref?, partial) → upsert + persist
 *   removeTarget(userId, type, ref?)
 *   listEnabledByType(type)       → [{ userId, target }] across all users
 *   listAllCustomAddresses()      → unique [{address, subscribers:[userId,...]}]
 *
 *   markSignalSeen(messageId, payload) / hasSignalBeenSeen(messageId)
 *
 * Constants: BCD_WALLET, BCD_SIGNALS_CHANNEL, REFERRAL_CODE.
 */

const fs = require('fs');
const path = require('path');

const BCD_WALLET = (process.env.BCD_WALLET ||
  '0xc7ae23316b47f7e75f455f53ad37873a18351505').toLowerCase();
const BCD_SIGNALS_CHANNEL =
  process.env.BCD_SIGNALS_CHANNEL || 'BlackCatDeFiSignals';
const REFERRAL_CODE = process.env.PEAR_REFERRAL_CODE || 'BlackCatDeFi';

const MIN_CAPITAL = parseFloat(process.env.COPY_AUTO_MIN_CAPITAL || '10');
const MAX_CAPITAL = parseFloat(process.env.COPY_AUTO_MAX_CAPITAL || '50000');
const DEFAULT_CAPITAL = parseFloat(
  process.env.COPY_AUTO_DEFAULT_CAPITAL || '100'
);
const MAX_CUSTOM_PER_USER = parseInt(
  process.env.COPY_AUTO_MAX_TARGETS_PER_USER || '10',
  10
);
const DEFAULT_SL = 50;
const DEFAULT_TRAILING = 10;
const DEFAULT_TRAILING_ACTIVATION = 30;

const TYPE_BCD_WALLET = 'BCD_WALLET';
const TYPE_BCD_SIGNALS = 'BCD_SIGNALS';
const TYPE_CUSTOM_WALLET = 'CUSTOM_WALLET';
const VALID_TYPES = [TYPE_BCD_WALLET, TYPE_BCD_SIGNALS, TYPE_CUSTOM_WALLET];

const ADDRESS_RX = /^0x[a-fA-F0-9]{40}$/;

function _resolveDbPath(filename) {
  const root =
    process.env.COPY_TRADING_DB_DIR ||
    process.env.RAILWAY_VOLUME_MOUNT_PATH ||
    '/app/data';
  return path.join(root, filename);
}

const DB_PATH = _resolveDbPath('copy_trading.json');
const SEEN_PATH = _resolveDbPath('signal_channel_seen.json');

let _store = null;
let _seen = null;

function _ensureDir(p) {
  try {
    fs.mkdirSync(path.dirname(p), { recursive: true });
  } catch (_) {}
}

function _emptyStore() {
  return {};
}

function _loadStore() {
  if (_store !== null) return _store;
  try {
    if (fs.existsSync(DB_PATH)) {
      const raw = JSON.parse(fs.readFileSync(DB_PATH, 'utf-8'));
      _store =
        raw && typeof raw === 'object' && !Array.isArray(raw)
          ? raw
          : _emptyStore();
    } else {
      _store = _emptyStore();
    }
  } catch (e) {
    console.error(
      '[copyTradingStore] load failed, starting empty:',
      e && e.message ? e.message : e
    );
    _store = _emptyStore();
  }
  return _store;
}

function _saveStore() {
  try {
    _ensureDir(DB_PATH);
    const tmp = DB_PATH + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(_loadStore(), null, 2));
    fs.renameSync(tmp, DB_PATH);
  } catch (e) {
    console.error(
      '[copyTradingStore] save failed:',
      e && e.message ? e.message : e
    );
  }
}

function _loadSeen() {
  if (_seen !== null) return _seen;
  try {
    if (fs.existsSync(SEEN_PATH)) {
      const raw = JSON.parse(fs.readFileSync(SEEN_PATH, 'utf-8'));
      _seen = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
    } else {
      _seen = {};
    }
  } catch (e) {
    console.error(
      '[copyTradingStore] seen load failed:',
      e && e.message ? e.message : e
    );
    _seen = {};
  }
  return _seen;
}

function _saveSeen() {
  try {
    _ensureDir(SEEN_PATH);
    const tmp = SEEN_PATH + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(_loadSeen(), null, 2));
    fs.renameSync(tmp, SEEN_PATH);
  } catch (e) {
    console.error(
      '[copyTradingStore] seen save failed:',
      e && e.message ? e.message : e
    );
  }
}

function _now() {
  return Math.floor(Date.now() / 1000);
}

function _defaultConfig() {
  const t = _now();
  return {
    enabled: 0,
    mode: 'MANUAL',
    capital_usdc: DEFAULT_CAPITAL,
    sl_pct: DEFAULT_SL,
    trailing_pct: DEFAULT_TRAILING,
    trailing_activation_pct: DEFAULT_TRAILING_ACTIVATION,
    created_at: t,
    updated_at: t,
  };
}

function _ensureUserSlot(userId) {
  const s = _loadStore();
  const key = String(userId);
  if (!s[key]) {
    s[key] = {
      [TYPE_BCD_WALLET]: null,
      [TYPE_BCD_SIGNALS]: null,
      [TYPE_CUSTOM_WALLET]: [],
    };
  } else {
    if (s[key][TYPE_BCD_WALLET] === undefined) s[key][TYPE_BCD_WALLET] = null;
    if (s[key][TYPE_BCD_SIGNALS] === undefined) s[key][TYPE_BCD_SIGNALS] = null;
    if (!Array.isArray(s[key][TYPE_CUSTOM_WALLET]))
      s[key][TYPE_CUSTOM_WALLET] = [];
  }
  return s[key];
}

function getTargets(userId) {
  return JSON.parse(JSON.stringify(_ensureUserSlot(userId)));
}

function getTarget(userId, type, ref) {
  if (!VALID_TYPES.includes(type)) return null;
  const slot = _ensureUserSlot(userId);
  if (type === TYPE_BCD_WALLET) {
    if (!slot[type]) return null;
    return { ...slot[type], target_type: type, target_ref: BCD_WALLET };
  }
  if (type === TYPE_BCD_SIGNALS) {
    if (!slot[type]) return null;
    return { ...slot[type], target_type: type, target_ref: null };
  }
  // CUSTOM_WALLET
  const lc = String(ref || '').toLowerCase();
  const found = slot[TYPE_CUSTOM_WALLET].find(
    (x) => String(x.ref || '').toLowerCase() === lc
  );
  if (!found) return null;
  return { ...found, target_type: type, target_ref: found.ref };
}

function _validateCapital(v) {
  const n = parseFloat(v);
  if (!Number.isFinite(n) || n <= 0) {
    throw new Error('Invalid amount');
  }
  if (n < MIN_CAPITAL) {
    throw new Error(`Minimum amount: $${MIN_CAPITAL}`);
  }
  if (n > MAX_CAPITAL) {
    throw new Error(`Maximum amount: $${MAX_CAPITAL.toLocaleString()}`);
  }
  return n;
}

function _validateMode(v) {
  const u = String(v || '').toUpperCase();
  return u === 'AUTO' ? 'AUTO' : 'MANUAL';
}

/**
 * Upsert a target. Returns the resulting full record.
 */
function setTarget(userId, type, ref, partial) {
  if (!VALID_TYPES.includes(type)) {
    throw new Error(`invalid type: ${type}`);
  }
  const slot = _ensureUserSlot(userId);
  const t = _now();

  if (type === TYPE_BCD_WALLET) {
    const cur = slot[type] || _defaultConfig();
    const next = { ...cur, ...partial, updated_at: t };
    if (partial && Object.prototype.hasOwnProperty.call(partial, 'capital_usdc')) {
      next.capital_usdc = _validateCapital(partial.capital_usdc);
    }
    if (partial && Object.prototype.hasOwnProperty.call(partial, 'mode')) {
      next.mode = _validateMode(partial.mode);
    }
    if (partial && Object.prototype.hasOwnProperty.call(partial, 'enabled')) {
      next.enabled = partial.enabled ? 1 : 0;
    }
    slot[type] = next;
    _saveStore();
    return { ...next, target_type: type, target_ref: BCD_WALLET };
  }

  if (type === TYPE_BCD_SIGNALS) {
    const cur = slot[type] || _defaultConfig();
    const next = { ...cur, ...partial, updated_at: t };
    if (partial && Object.prototype.hasOwnProperty.call(partial, 'capital_usdc')) {
      next.capital_usdc = _validateCapital(partial.capital_usdc);
    }
    if (partial && Object.prototype.hasOwnProperty.call(partial, 'mode')) {
      next.mode = _validateMode(partial.mode);
    }
    if (partial && Object.prototype.hasOwnProperty.call(partial, 'enabled')) {
      next.enabled = partial.enabled ? 1 : 0;
    }
    slot[type] = next;
    _saveStore();
    return { ...next, target_type: type, target_ref: null };
  }

  // CUSTOM_WALLET
  const lc = String(ref || '').toLowerCase();
  if (!ADDRESS_RX.test(lc)) {
    throw new Error('Invalid address — must be 0x + 40 hex chars');
  }
  const arr = slot[TYPE_CUSTOM_WALLET];
  let entry = arr.find(
    (x) => String(x.ref || '').toLowerCase() === lc
  );
  if (!entry) {
    if (arr.length >= MAX_CUSTOM_PER_USER) {
      throw new Error(
        `Maximum ${MAX_CUSTOM_PER_USER} custom wallets per user.`
      );
    }
    entry = {
      ..._defaultConfig(),
      ref: lc,
      label: (partial && partial.label) || `${lc.slice(0, 6)}...${lc.slice(-4)}`,
    };
    arr.push(entry);
  }
  if (partial) {
    if (Object.prototype.hasOwnProperty.call(partial, 'capital_usdc')) {
      entry.capital_usdc = _validateCapital(partial.capital_usdc);
    }
    if (Object.prototype.hasOwnProperty.call(partial, 'mode')) {
      entry.mode = _validateMode(partial.mode);
    }
    if (Object.prototype.hasOwnProperty.call(partial, 'enabled')) {
      entry.enabled = partial.enabled ? 1 : 0;
    }
    if (Object.prototype.hasOwnProperty.call(partial, 'label')) {
      const lab = String(partial.label || '').trim().slice(0, 64);
      if (lab) entry.label = lab;
    }
  }
  entry.updated_at = t;
  _saveStore();
  return { ...entry, target_type: type, target_ref: entry.ref };
}

function removeTarget(userId, type, ref) {
  const slot = _ensureUserSlot(userId);
  if (type === TYPE_BCD_WALLET || type === TYPE_BCD_SIGNALS) {
    slot[type] = null;
    _saveStore();
    return true;
  }
  if (type === TYPE_CUSTOM_WALLET) {
    const lc = String(ref || '').toLowerCase();
    const before = slot[TYPE_CUSTOM_WALLET].length;
    slot[TYPE_CUSTOM_WALLET] = slot[TYPE_CUSTOM_WALLET].filter(
      (x) => String(x.ref || '').toLowerCase() !== lc
    );
    if (slot[TYPE_CUSTOM_WALLET].length !== before) {
      _saveStore();
      return true;
    }
  }
  return false;
}

/**
 * Returns [{ userId, config, ref }] across all users for the requested
 * type, filtering enabled=1 only.
 */
function listEnabledByType(type) {
  const out = [];
  const s = _loadStore();
  for (const userId of Object.keys(s)) {
    const slot = s[userId];
    if (!slot) continue;
    if (type === TYPE_BCD_WALLET || type === TYPE_BCD_SIGNALS) {
      const cfg = slot[type];
      if (cfg && cfg.enabled) {
        out.push({
          userId,
          config: cfg,
          ref: type === TYPE_BCD_WALLET ? BCD_WALLET : null,
        });
      }
    } else if (type === TYPE_CUSTOM_WALLET) {
      for (const entry of slot[TYPE_CUSTOM_WALLET] || []) {
        if (entry.enabled) {
          out.push({ userId, config: entry, ref: entry.ref });
        }
      }
    }
  }
  return out;
}

/**
 * Returns list of unique custom-wallet addresses with their subscribers.
 *   [{ address: '0x...', subscribers: [{userId, label, capital_usdc, mode}, ...] }]
 *
 * Scheduler uses this to do 1 HL fetch per address × fan-out to N users.
 */
function listAllCustomAddresses() {
  const map = new Map();
  const s = _loadStore();
  for (const userId of Object.keys(s)) {
    const slot = s[userId];
    if (!slot || !Array.isArray(slot[TYPE_CUSTOM_WALLET])) continue;
    for (const entry of slot[TYPE_CUSTOM_WALLET]) {
      if (!entry.enabled) continue;
      const addr = String(entry.ref || '').toLowerCase();
      if (!ADDRESS_RX.test(addr)) continue;
      if (!map.has(addr)) map.set(addr, []);
      map.get(addr).push({
        userId,
        label: entry.label,
        capital_usdc: entry.capital_usdc,
        mode: entry.mode,
        sl_pct: entry.sl_pct,
        trailing_pct: entry.trailing_pct,
        trailing_activation_pct: entry.trailing_activation_pct,
      });
    }
  }
  return Array.from(map.entries()).map(([address, subscribers]) => ({
    address,
    subscribers,
  }));
}

/**
 * Channel signal seen-tracker. Keyed by `${channel}/${message_id}`.
 */
function hasSignalBeenSeen(channel, messageId) {
  const seen = _loadSeen();
  return Boolean(seen[`${channel}/${messageId}`]);
}

function markSignalSeen(channel, messageId, payload) {
  const seen = _loadSeen();
  const key = `${channel}/${messageId}`;
  seen[key] = { ...payload, seen_at: _now() };
  // Cap to last 1000 entries to avoid unbounded growth.
  const keys = Object.keys(seen);
  if (keys.length > 1000) {
    keys
      .sort((a, b) => (seen[a].seen_at || 0) - (seen[b].seen_at || 0))
      .slice(0, keys.length - 1000)
      .forEach((k) => delete seen[k]);
  }
  _saveSeen();
}

function listSignalSeen() {
  return { ..._loadSeen() };
}

// --- test hooks -----------------------------------------------------------
function _resetForTests() {
  _store = _emptyStore();
  _seen = {};
}

function _setStoreForTests(s) {
  _store = s || _emptyStore();
}

module.exports = {
  // constants
  BCD_WALLET,
  BCD_SIGNALS_CHANNEL,
  REFERRAL_CODE,
  MIN_CAPITAL,
  MAX_CAPITAL,
  DEFAULT_CAPITAL,
  MAX_CUSTOM_PER_USER,
  TYPE_BCD_WALLET,
  TYPE_BCD_SIGNALS,
  TYPE_CUSTOM_WALLET,
  VALID_TYPES,
  // crud
  getTargets,
  getTarget,
  setTarget,
  removeTarget,
  listEnabledByType,
  listAllCustomAddresses,
  // seen
  hasSignalBeenSeen,
  markSignalSeen,
  listSignalSeen,
  // test
  _resetForTests,
  _setStoreForTests,
  _DB_PATH: DB_PATH,
  _SEEN_PATH: SEEN_PATH,
};
