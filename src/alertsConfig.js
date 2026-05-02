'use strict';

/**
 * R-AUTOCOPY — Granular alerts opt-in/out per user.
 *
 * Categories:
 *   basket_open      — new basket opened in tracked wallet
 *   basket_close     — basket closed (SL/TP/manual)
 *   signals          — incoming signals from @BlackCatDeFiSignals
 *   compounding      — compound detected in tracked wallet
 *   hf_critical      — HF<X.XX in tracked wallet
 *   daily_summary    — daily digest 9am local TZ
 *
 * Storage: JSON file on Railway Volume.
 *   {
 *     "{userId}": { basket_open: 1, basket_close: 1, signals: 1, ... }
 *   }
 */

const fs = require('fs');
const path = require('path');

const CATEGORIES = Object.freeze([
  'basket_open',
  'basket_close',
  'signals',
  'compounding',
  'hf_critical',
  'daily_summary',
]);

const DEFAULTS_ON = Object.freeze({
  basket_open: 1,
  basket_close: 1,
  signals: 1,
  compounding: 0,
  hf_critical: 0,
  daily_summary: 0,
});

function _resolveDbPath() {
  return (
    process.env.ALERTS_CONFIG_DB_PATH ||
    path.join(
      process.env.RAILWAY_VOLUME_MOUNT_PATH || '/app/data',
      'alerts_config.json'
    )
  );
}

const DB_PATH = _resolveDbPath();
let _store = null;

function _ensureDir() {
  try { fs.mkdirSync(path.dirname(DB_PATH), { recursive: true }); }
  catch (_) {}
}

function _load() {
  if (_store !== null) return _store;
  try {
    if (fs.existsSync(DB_PATH)) {
      const raw = JSON.parse(fs.readFileSync(DB_PATH, 'utf-8'));
      _store = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
    } else {
      _store = {};
    }
  } catch (e) {
    console.error('[alertsConfig] load failed:', e && e.message ? e.message : e);
    _store = {};
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
    console.error('[alertsConfig] save failed:', e && e.message ? e.message : e);
  }
}

function getConfig(userId) {
  const s = _load();
  const rec = s[String(userId)] || {};
  const out = {};
  for (const cat of CATEGORIES) {
    out[cat] = (rec[cat] !== undefined ? rec[cat] : DEFAULTS_ON[cat]) ? 1 : 0;
  }
  return out;
}

function setCategory(userId, category, enabled) {
  if (!CATEGORIES.includes(category)) throw new Error(`Unknown category: ${category}`);
  const s = _load();
  const key = String(userId);
  if (!s[key]) s[key] = {};
  s[key][category] = enabled ? 1 : 0;
  s[key]._updated_at = Date.now();
  _save();
  return getConfig(userId);
}

function toggle(userId, category) {
  const cur = getConfig(userId);
  return setCategory(userId, category, !cur[category]);
}

function isAllowed(userId, category) {
  const cfg = getConfig(userId);
  return Boolean(cfg[category]);
}

function _resetForTests(customPath) {
  _store = {};
  try {
    const p = customPath || DB_PATH;
    if (fs.existsSync(p)) fs.unlinkSync(p);
  } catch (_) {}
}

module.exports = {
  CATEGORIES,
  DEFAULTS_ON,
  DB_PATH,
  getConfig,
  setCategory,
  toggle,
  isAllowed,
  _resetForTests,
};
