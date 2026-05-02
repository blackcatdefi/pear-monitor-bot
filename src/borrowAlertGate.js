'use strict';

/**
 * R-NOSPAM (2 may 2026) — HyperLend borrow-available alert dedup gate.
 *
 * Bug fixed:
 *   On 2 may 2026 09:10 + 09:11 UTC (1 minute apart), the bot fired two
 *   IDENTICAL "🏦 HyperLend — Borrow Available!" alerts for the same
 *   wallet (DDS, $174.25 available, HF 1.26). Root cause: the legacy
 *   monitor.js gate is a single edge-trigger
 *
 *     crossedThreshold = (available >= $50) && (state.hadBorrowAvailable === false)
 *
 *   When `available` oscillates around the $50 threshold (which happens
 *   every poll cycle for a wallet sitting just above it because of
 *   floating-point + interest accrual + price moves), the boolean
 *   alternates true→false→true and the alert re-fires.
 *
 * Fix shape:
 *   Persistent per-wallet state in JSON file at
 *     ${RAILWAY_VOLUME_MOUNT_PATH}/data/borrow_alerts_state.json
 *   keyed by wallet address. Records last_alert_at, last_available, last_hf.
 *
 *   shouldEmitBorrowAlert(wallet, { available, healthFactor }) returns
 *   { shouldEmit, reason } applying these rules in order:
 *
 *     1. last_alert_at < 30 min ago         → suppress (cooldown)
 *     2. |Δ available| < 5% of last_available → suppress (no material change)
 *     3. |Δ HF| < 0.05                       → suppress (no material change)
 *     4. HF crossed below 1.10 vs last       → FORCE EMIT (critical)
 *     5. |Δ available| > 50% of last_available → FORCE EMIT (material)
 *     6. otherwise emit (cooldown ok + delta ok)
 *
 *   markAlertEmitted(wallet, payload) persists the new state.
 *
 *   Storage uses JSON-file pattern matching basketDedup.js (no node-gyp /
 *   sqlite3 native dep on alpine).
 */

const fs = require('fs');
const path = require('path');

const VOLUME = process.env.RAILWAY_VOLUME_MOUNT_PATH;
const DATA_DIR = VOLUME
  ? path.join(VOLUME, 'data')
  : path.join(__dirname, '..', 'data');

const DEFAULT_DB_PATH = path.join(DATA_DIR, 'borrow_alerts_state.json');
const DB_PATH = process.env.BORROW_ALERT_GATE_PATH || DEFAULT_DB_PATH;

const COOLDOWN_MS = parseInt(
  process.env.BORROW_ALERT_COOLDOWN_MIN || '30',
  10
) * 60 * 1000;

const AVAILABLE_PCT_THRESHOLD = parseFloat(
  process.env.BORROW_ALERT_AVAILABLE_PCT || '0.05'
);

const HF_DELTA_THRESHOLD = parseFloat(
  process.env.BORROW_ALERT_HF_DELTA || '0.05'
);

const HF_CRITICAL = parseFloat(process.env.BORROW_ALERT_HF_CRITICAL || '1.10');

const FORCE_EMIT_AVAILABLE_PCT = parseFloat(
  process.env.BORROW_ALERT_FORCE_PCT || '0.50'
);

const ENABLED =
  (process.env.BORROW_ALERT_GATE_ENABLED || 'true').toLowerCase() !== 'false';

function _ensureDir() {
  const dir = path.dirname(DB_PATH);
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
}

function _readDb() {
  _ensureDir();
  if (!fs.existsSync(DB_PATH)) return {};
  try {
    const raw = fs.readFileSync(DB_PATH, 'utf8');
    if (!raw.trim()) return {};
    return JSON.parse(raw);
  } catch (e) {
    console.error(
      `[borrowAlertGate] failed to read ${DB_PATH}, starting fresh:`,
      e && e.message ? e.message : e
    );
    return {};
  }
}

function _writeDb(db) {
  _ensureDir();
  const tmp = `${DB_PATH}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(db, null, 2));
  fs.renameSync(tmp, DB_PATH);
}

function _normWallet(w) {
  return String(w || '').toLowerCase();
}

function _safeNumber(v) {
  if (v === Infinity) return Infinity;
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

/**
 * Decide whether to emit a borrow-available alert for a wallet.
 *
 * @param {string} wallet
 * @param {{available: number, healthFactor: number}} payload
 * @param {number} [nowMs] — for tests
 * @returns {{shouldEmit: boolean, reason: string}}
 */
function shouldEmitBorrowAlert(wallet, payload, nowMs = Date.now()) {
  if (!ENABLED) {
    return { shouldEmit: true, reason: 'GATE_DISABLED' };
  }
  const w = _normWallet(wallet);
  if (!w) return { shouldEmit: false, reason: 'NO_WALLET' };

  const available = _safeNumber(payload && payload.available);
  const hfNum = payload && payload.healthFactor;
  const hf = hfNum === Infinity ? Infinity : _safeNumber(hfNum);

  const db = _readDb();
  const prev = db[w];

  // First-time alert for this wallet — always emit.
  if (!prev) {
    return { shouldEmit: true, reason: 'FIRST_ALERT' };
  }

  const dtMs = nowMs - Number(prev.last_alert_at || 0);
  const lastAvail = _safeNumber(prev.last_available);
  const lastHf =
    prev.last_hf === 'Infinity' || prev.last_hf === Infinity
      ? Infinity
      : _safeNumber(prev.last_hf);

  // Compute deltas (use abs ratio relative to last_available; handle 0)
  const availDelta = Math.abs(available - lastAvail);
  const availPct = lastAvail > 0 ? availDelta / lastAvail : 1; // first alert with $0 prev = treat as 100%
  const hfDelta =
    lastHf === Infinity || hf === Infinity
      ? Infinity
      : Math.abs(hf - lastHf);

  // FORCE EMIT cases — critical conditions bypass cooldown.
  // 4. HF crossed below critical (1.10 default) since last alert.
  const crossedCritical = lastHf >= HF_CRITICAL && hf < HF_CRITICAL;
  if (crossedCritical) {
    return { shouldEmit: true, reason: 'HF_CROSSED_CRITICAL' };
  }
  // 5. available changed by >50% (material change in borrow capacity).
  if (lastAvail > 0 && availPct > FORCE_EMIT_AVAILABLE_PCT) {
    return { shouldEmit: true, reason: 'AVAILABLE_DELTA_FORCE' };
  }

  // Cooldown gate (only applies when no force-emit triggered).
  if (dtMs < COOLDOWN_MS) {
    return { shouldEmit: false, reason: 'COOLDOWN' };
  }

  // Material-change gates
  if (availPct < AVAILABLE_PCT_THRESHOLD) {
    return { shouldEmit: false, reason: 'AVAILABLE_DELTA_TOO_SMALL' };
  }
  // HF delta < 0.05 + available delta < threshold both → suppress.
  // We already passed the available gate above, so check HF separately
  // ONLY when hfDelta is a finite small number AND avail is also small.
  // Spec says: dedup if HF delta <0.05 (regardless of avail delta).
  // Apply strictly: any of the small-delta conditions suppresses.
  if (hfDelta !== Infinity && hfDelta < HF_DELTA_THRESHOLD &&
      availPct < FORCE_EMIT_AVAILABLE_PCT) {
    return { shouldEmit: false, reason: 'HF_DELTA_TOO_SMALL' };
  }

  return { shouldEmit: true, reason: 'OK' };
}

/**
 * Persist that we just emitted an alert for this wallet.
 */
function markAlertEmitted(wallet, payload, nowMs = Date.now()) {
  if (!ENABLED) return;
  const w = _normWallet(wallet);
  if (!w) return;

  const db = _readDb();
  const hfRaw = payload && payload.healthFactor;
  const hfStored = hfRaw === Infinity ? 'Infinity' : _safeNumber(hfRaw);
  db[w] = {
    last_alert_at: nowMs,
    last_available: _safeNumber(payload && payload.available),
    last_hf: hfStored,
  };
  _writeDb(db);
}

function _resetForTests() {
  try {
    if (fs.existsSync(DB_PATH)) fs.unlinkSync(DB_PATH);
  } catch (_) {
    /* ignore */
  }
}

function _backdateForTests(wallet, msAgo) {
  const w = _normWallet(wallet);
  const db = _readDb();
  if (!db[w]) return false;
  db[w].last_alert_at = Date.now() - msAgo;
  _writeDb(db);
  return true;
}

module.exports = {
  shouldEmitBorrowAlert,
  markAlertEmitted,
  ENABLED,
  DB_PATH,
  COOLDOWN_MS,
  AVAILABLE_PCT_THRESHOLD,
  HF_DELTA_THRESHOLD,
  HF_CRITICAL,
  FORCE_EMIT_AVAILABLE_PCT,
  _resetForTests,
  _backdateForTests,
};
