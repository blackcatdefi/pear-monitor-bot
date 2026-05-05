'use strict';

/**
 * R-PUBLIC-V4-COPYMENU — Unified Copy Trading store (2 modes).
 *
 * Persists per-user copy targets across two modes:
 *   • BCD_WALLET       — auto-track BCD trading wallet 0xc7ae...1505
 *   • CUSTOM_WALLET    — any address the user adds (max 3 per user)
 *
 * NOTE — V4: BCD_SIGNALS has been removed. Signals scraping (channel
 * polling for #signal posts) was killed because it produced too much noise
 * for too little conversion lift. The only copy source now is on-chain
 * wallet polling: either BCD's public wallet, or a custom address the user
 * provides. The community signals/thesis Telegram channels remain available
 * as URL buttons in /start (informational, NOT a copy source).
 *
 * Storage shape (JSON on Railway Volume `copy_trading_state.json`):
 *
 *   {
 *     "{userId}": {
 *       "BCD_WALLET":  { ...config, address: '0xc7ae...' } | null,
 *       "CUSTOM_WALLET": [
 *         { ref: '0xabc...', label, ...config },
 *         ...
 *       ],
 *       "settings": {
 *         "basket_level_only": 1,   // suppress per-leg fan-out
 *         "paused": 0               // global pause for this user
 *       }
 *     }
 *   }
 *
 * Each config: { capital_usdc, mode, enabled, sl_pct, trailing_pct,
 *                trailing_activation_pct, created_at, updated_at }.
 */

const fs = require('fs');
const path = require('path');

const BCD_WALLET = (process.env.BCD_WALLET ||
  '0xc7ae23316b47f7e75f455f53ad37873a18351505').toLowerCase();
const REFERRAL_CODE = process.env.PEAR_REFERRAL_CODE || 'BlackCatDeFi';

const MIN_CAPITAL = parseFloat(process.env.COPY_AUTO_MIN_CAPITAL || '10');
const MAX_CAPITAL = parseFloat(process.env.COPY_AUTO_MAX_CAPITAL || '50000');
const DEFAULT_CAPITAL = parseFloat(
  process.env.COPY_AUTO_DEFAULT_CAPITAL || '100'
);
// V4: cap reduced from 10 → 3. Custom-wallet copy is *separate* from /track
// (max 10) — users who want to monitor more wallets can still use /track.
const MAX_CUSTOM_PER_USER = parseInt(
  process.env.COPY_TRADING_MAX_CUSTOM_PER_USER || '3',
  10
);
const DEFAULT_SL = 50;
const DEFAULT_TRAILING = 10;
const DEFAULT_TRAILING_ACTIVATION = 30;

const TYPE_BCD_WALLET = 'BCD_WALLET';
const TYPE_CUSTOM_WALLET = 'CUSTOM_WALLET';
const VALID_TYPES = [TYPE_BCD_WALLET, TYPE_CUSTOM_WALLET];

const ADDRESS_RX = /^0x[a-fA-F0-9]{40}$/;

function _resolveDbPath(filename) {
  const root =
    process.env.COPY_TRADING_DB_DIR ||
    process.env.RAILWAY_VOLUME_MOUNT_PATH ||
    '/app/data';
  return path.join(root, filename);
}

const DB_PATH = _resolveDbPath('copy_trading_state.json');

let _store = null;

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

function _defaultSettings() {
  return {
    basket_level_only: 1, // default ON — avoids per-leg spam
    paused: 0,
  };
}

function _ensureUserSlot(userId) {
  const s = _loadStore();
  const key = String(userId);
  if (!s[key]) {
    s[key] = {
      [TYPE_BCD_WALLET]: null,
      [TYPE_CUSTOM_WALLET]: [],
      settings: _defaultSettings(),
    };
  } else {
    if (s[key][TYPE_BCD_WALLET] === undefined) s[key][TYPE_BCD_WALLET] = null;
    if (!Array.isArray(s[key][TYPE_CUSTOM_WALLET]))
      s[key][TYPE_CUSTOM_WALLET] = [];
    if (!s[key].settings || typeof s[key].settings !== 'object') {
      s[key].settings = _defaultSettings();
    } else {
      // Backfill missing keys
      const def = _defaultSettings();
      for (const k of Object.keys(def)) {
        if (s[key].settings[k] === undefined) s[key].settings[k] = def[k];
      }
    }
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
  if (type === TYPE_BCD_WALLET) {
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
 * type, filtering enabled=1 only. Users with `paused=1` are excluded.
 */
function listEnabledByType(type) {
  const out = [];
  const s = _loadStore();
  for (const userId of Object.keys(s)) {
    const slot = s[userId];
    if (!slot) continue;
    if (slot.settings && slot.settings.paused) continue;
    if (type === TYPE_BCD_WALLET) {
      const cfg = slot[type];
      if (cfg && cfg.enabled) {
        out.push({
          userId,
          config: cfg,
          ref: BCD_WALLET,
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
 * Paused users are excluded.
 */
function listAllCustomAddresses() {
  const map = new Map();
  const s = _loadStore();
  for (const userId of Object.keys(s)) {
    const slot = s[userId];
    if (!slot || !Array.isArray(slot[TYPE_CUSTOM_WALLET])) continue;
    if (slot.settings && slot.settings.paused) continue;
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

// ---- Per-user settings (V4) ---------------------------------------------

function getSettings(userId) {
  const slot = _ensureUserSlot(userId);
  return { ...slot.settings };
}

function setSetting(userId, key, value) {
  const slot = _ensureUserSlot(userId);
  if (!Object.prototype.hasOwnProperty.call(_defaultSettings(), key)) {
    throw new Error(`Unknown setting: ${key}`);
  }
  slot.settings[key] = value ? 1 : 0;
  _saveStore();
  return { ...slot.settings };
}

// --- test hooks -----------------------------------------------------------
function _resetForTests() {
  _store = _emptyStore();
}

function _setStoreForTests(s) {
  _store = s || _emptyStore();
}

module.exports = {
  // constants
  BCD_WALLET,
  REFERRAL_CODE,
  MIN_CAPITAL,
  MAX_CAPITAL,
  DEFAULT_CAPITAL,
  MAX_CUSTOM_PER_USER,
  TYPE_BCD_WALLET,
  TYPE_CUSTOM_WALLET,
  VALID_TYPES,
  // crud
  getTargets,
  getTarget,
  setTarget,
  removeTarget,
  listEnabledByType,
  listAllCustomAddresses,
  // settings
  getSettings,
  setSetting,
  // test
  _resetForTests,
  _setStoreForTests,
  _DB_PATH: DB_PATH,
};
