'use strict';

/**
 * R-PUBLIC-FUNDS — Opt-in store + anti-flap hysteresis for the public
 * funds-available alert.
 *
 * Persistence: JSON on the Railway volume (same pattern as walletTracker).
 *
 * Shape:
 *   {
 *     "byUser": {
 *       "<userId>": { "enabled": true, "threshold": 500, "updated_at": 173... }
 *     },
 *     "gates": {
 *       "<userId>:<wallet_lc>:<metric>": {
 *         "armed": true, "lastFireAt": 173..., "lastValue": 123.4
 *       }
 *     }
 *   }
 *
 * Hysteresis contract (per user+wallet+metric, metric ∈ total|pm):
 *   • fire only on a below→at-or-above crossing while ARMED
 *   • after firing, DISARM
 *   • re-arm when value falls below 50% of threshold, OR after the 12h
 *     cooldown elapses (periodic re-reminder if capital stays deployable)
 */

const fs = require('fs');
const path = require('path');

const DEFAULT_THRESHOLD_USD = parseFloat(
  process.env.FUNDS_ALERT_DEFAULT_THRESHOLD_USD || '500'
);
const MIN_THRESHOLD_USD = 50;
const MAX_THRESHOLD_USD = 10000000;
const REARM_FRACTION = 0.5;
const COOLDOWN_MS = parseFloat(
  process.env.FUNDS_ALERT_COOLDOWN_HOURS || '12'
) * 3600 * 1000;

function _resolveDbPath() {
  return (
    process.env.FUNDS_ALERT_DB_PATH ||
    path.join(
      process.env.RAILWAY_VOLUME_MOUNT_PATH || '/app/data',
      'funds_alerts.json'
    )
  );
}

let _store = null;
let _dbPath = null;

function _dbFile() {
  if (!_dbPath) _dbPath = _resolveDbPath();
  return _dbPath;
}

function _empty() {
  return { byUser: {}, gates: {} };
}

function _load() {
  if (_store !== null) return _store;
  try {
    const p = _dbFile();
    if (fs.existsSync(p)) {
      const raw = JSON.parse(fs.readFileSync(p, 'utf-8'));
      _store = raw && typeof raw === 'object' ? raw : _empty();
    } else {
      _store = _empty();
    }
  } catch (e) {
    console.error('[fundsAlertStore] load failed, starting empty:', e.message);
    _store = _empty();
  }
  if (!_store.byUser) _store.byUser = {};
  if (!_store.gates) _store.gates = {};
  return _store;
}

function _save() {
  try {
    const p = _dbFile();
    fs.mkdirSync(path.dirname(p), { recursive: true });
    const tmp = p + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(_load(), null, 2));
    fs.renameSync(tmp, p);
  } catch (e) {
    console.error('[fundsAlertStore] save failed:', e.message);
  }
}

// ───────────────────────────── opt-in CRUD ─────────────────────────────

function optIn(userId, threshold) {
  const t = Number.isFinite(threshold) ? threshold : DEFAULT_THRESHOLD_USD;
  if (t < MIN_THRESHOLD_USD || t > MAX_THRESHOLD_USD) {
    throw new Error(
      `Threshold must be between $${MIN_THRESHOLD_USD} and $${MAX_THRESHOLD_USD.toLocaleString('en-US')}.`
    );
  }
  const s = _load();
  s.byUser[String(userId)] = {
    enabled: true,
    threshold: t,
    updated_at: Date.now(),
  };
  _save();
  return s.byUser[String(userId)];
}

function optOut(userId) {
  const s = _load();
  const key = String(userId);
  const had = !!(s.byUser[key] && s.byUser[key].enabled);
  delete s.byUser[key];
  // Drop gate state so a future re-opt-in starts armed & clean.
  for (const k of Object.keys(s.gates)) {
    if (k.startsWith(`${key}:`)) delete s.gates[k];
  }
  _save();
  return had;
}

function getConfig(userId) {
  const s = _load();
  const cfg = s.byUser[String(userId)];
  return cfg ? { ...cfg } : null;
}

function getAllOptedIn() {
  const s = _load();
  return Object.entries(s.byUser)
    .filter(([, cfg]) => cfg && cfg.enabled)
    .map(([userId, cfg]) => ({ userId, threshold: cfg.threshold }));
}

// ───────────────────────────── hysteresis gate ─────────────────────────────

function _gateKey(userId, wallet, metric) {
  return `${userId}:${String(wallet || '').toLowerCase()}:${metric}`;
}

/**
 * Evaluate one metric value against the user's threshold.
 * Returns { shouldFire, reason } and mutates persisted gate state.
 *
 * value == null (fetch error / not applicable) → never fires, state kept.
 */
function evaluate(userId, wallet, metric, value, threshold, now = Date.now()) {
  if (value == null || !Number.isFinite(value)) {
    return { shouldFire: false, reason: 'NO_VALUE' };
  }
  const s = _load();
  const key = _gateKey(userId, wallet, metric);
  let g = s.gates[key];
  if (!g) {
    g = { armed: true, lastFireAt: 0, lastValue: null };
    s.gates[key] = g;
  }

  const prev = g.lastValue;
  g.lastValue = value;

  // Re-arm conditions.
  if (!g.armed) {
    if (value < threshold * REARM_FRACTION) {
      g.armed = true;
    } else if (now - g.lastFireAt >= COOLDOWN_MS) {
      g.armed = true;
    }
  }

  let fire = false;
  let reason = 'BELOW_THRESHOLD';
  if (value >= threshold) {
    if (!g.armed) {
      reason = 'DISARMED';
    } else if (prev != null && prev >= threshold && now - g.lastFireAt < COOLDOWN_MS) {
      // Already above last scan and inside cooldown — not a crossing.
      reason = 'NO_CROSSING';
    } else {
      fire = true;
      reason = 'CROSSED';
      g.armed = false;
      g.lastFireAt = now;
    }
  }
  _save();
  return { shouldFire: fire, reason };
}

function _resetForTests(customPath) {
  _store = _empty();
  _dbPath = customPath || null; // re-resolve from env on next access
  try {
    const p = _dbFile();
    if (fs.existsSync(p)) fs.unlinkSync(p);
  } catch (_) {}
}

module.exports = {
  DEFAULT_THRESHOLD_USD,
  MIN_THRESHOLD_USD,
  MAX_THRESHOLD_USD,
  REARM_FRACTION,
  COOLDOWN_MS,
  optIn,
  optOut,
  getConfig,
  getAllOptedIn,
  evaluate,
  _resetForTests,
};
