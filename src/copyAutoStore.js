'use strict';

/**
 * R-AUTOCOPY — Per-user copy-auto config store.
 *
 * Persists each user's copy-auto preferences to a JSON file on the Railway
 * Volume (same disk used by walletTracker / timezoneManager / basketDedup —
 * we deliberately avoid better-sqlite3 because Alpine Node images don't ship
 * the build deps).
 *
 * Storage shape:
 *   {
 *     "{userId}": {
 *       enabled: 0|1,
 *       mode: 'MANUAL' | 'AUTO',
 *       capital_usdc: 100,
 *       sl_pct: 50,
 *       trailing_pct: 10,
 *       trailing_activation_pct: 30,
 *       updated_at: 1735...
 *     }
 *   }
 *
 * Public surface:
 *   getConfig(userId)
 *   setConfig(userId, partial)
 *   setEnabled(userId, bool)
 *   setMode(userId, 'MANUAL'|'AUTO')
 *   setCapital(userId, amount)
 *   listEnabledUsers()
 */

const fs = require('fs');
const path = require('path');

const MIN_CAPITAL = parseFloat(process.env.COPY_AUTO_MIN_CAPITAL || '10');
const MAX_CAPITAL = parseFloat(process.env.COPY_AUTO_MAX_CAPITAL || '50000');
const DEFAULT_CAPITAL = parseFloat(process.env.COPY_AUTO_DEFAULT_CAPITAL || '100');
const DEFAULT_SL = 50;
const DEFAULT_TRAILING = 10;
const DEFAULT_TRAILING_ACTIVATION = 30;

function _resolveDbPath() {
  return (
    process.env.COPY_AUTO_DB_PATH ||
    path.join(
      process.env.RAILWAY_VOLUME_MOUNT_PATH || '/app/data',
      'copy_auto.json'
    )
  );
}

const DB_PATH = _resolveDbPath();
let _store = null;

function _ensureDir() {
  try {
    fs.mkdirSync(path.dirname(DB_PATH), { recursive: true });
  } catch (_) {}
}

function _emptyStore() {
  return {};
}

function _load() {
  if (_store !== null) return _store;
  try {
    if (fs.existsSync(DB_PATH)) {
      const raw = JSON.parse(fs.readFileSync(DB_PATH, 'utf-8'));
      _store = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : _emptyStore();
    } else {
      _store = _emptyStore();
    }
  } catch (e) {
    console.error('[copyAutoStore] load failed, starting empty:', e && e.message ? e.message : e);
    _store = _emptyStore();
  }
  return _store;
}

function _save() {
  try {
    _ensureDir();
    const tmp = DB_PATH + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(_load(), null, 2));
    fs.renameSync(tmp, DB_PATH);
  } catch (e) {
    console.error('[copyAutoStore] save failed:', e && e.message ? e.message : e);
  }
}

function _defaults() {
  return {
    enabled: 0,
    mode: 'MANUAL',
    capital_usdc: DEFAULT_CAPITAL,
    sl_pct: DEFAULT_SL,
    trailing_pct: DEFAULT_TRAILING,
    trailing_activation_pct: DEFAULT_TRAILING_ACTIVATION,
    updated_at: 0,
  };
}

function getConfig(userId) {
  if (userId == null) return _defaults();
  const s = _load();
  const rec = s[String(userId)];
  if (!rec || typeof rec !== 'object') return _defaults();
  return Object.assign(_defaults(), rec);
}

function setConfig(userId, partial) {
  if (userId == null) throw new Error('userId requerido');
  const s = _load();
  const key = String(userId);
  const cur = s[key] || _defaults();
  const next = Object.assign({}, cur, partial || {}, { updated_at: Date.now() });
  s[key] = next;
  _save();
  return next;
}

function setEnabled(userId, enabled) {
  return setConfig(userId, { enabled: enabled ? 1 : 0 });
}

function setMode(userId, mode) {
  const m = String(mode || '').toUpperCase();
  if (m !== 'MANUAL' && m !== 'AUTO') {
    throw new Error('Modo debe ser MANUAL o AUTO');
  }
  return setConfig(userId, { mode: m });
}

function validateCapital(amount) {
  const n = Number(amount);
  if (!Number.isFinite(n)) {
    throw new Error('Monto inválido');
  }
  if (n < MIN_CAPITAL) {
    throw new Error(`Monto mínimo: $${MIN_CAPITAL} USDC`);
  }
  if (n > MAX_CAPITAL) {
    throw new Error(`Monto máximo: $${MAX_CAPITAL} USDC`);
  }
  return Math.round(n * 100) / 100;
}

function setCapital(userId, amount) {
  const validated = validateCapital(amount);
  return setConfig(userId, { capital_usdc: validated });
}

function listEnabledUsers() {
  const s = _load();
  const out = [];
  for (const userId of Object.keys(s)) {
    const rec = s[userId];
    if (rec && rec.enabled) out.push({ userId, config: getConfig(userId) });
  }
  return out;
}

function _resetForTests(customPath) {
  _store = _emptyStore();
  try {
    const p = customPath || DB_PATH;
    if (fs.existsSync(p)) fs.unlinkSync(p);
  } catch (_) {}
}

module.exports = {
  DB_PATH,
  MIN_CAPITAL,
  MAX_CAPITAL,
  DEFAULT_CAPITAL,
  DEFAULT_SL,
  DEFAULT_TRAILING,
  DEFAULT_TRAILING_ACTIVATION,
  getConfig,
  setConfig,
  setEnabled,
  setMode,
  setCapital,
  validateCapital,
  listEnabledUsers,
  _resetForTests,
};
