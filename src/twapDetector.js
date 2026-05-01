'use strict';

/**
 * R(v3) — TWAP detector.
 *
 * Identifies an active TWAP execution on a tracked wallet so downstream gates
 * (fundsAvailableGate, compoundingGate) can suppress noisy alerts that would
 * otherwise misfire while a Pear basket is filling progressively.
 *
 * Detection signal: 3+ DISTINCT coins recorded as new opens for the same
 * wallet within `windowMinutes` (default 5min). Pear baskets ship as TWAPs
 * with ~28min cadence over ~14h (typical TWAP entry default), so a 5min
 * detection window catches the initial burst.
 *
 * State is in-memory only — that is fine for the bot's scope: a Railway
 * restart during a TWAP just means we miss the suppression window for the
 * remainder of that TWAP, no false alerts persist.
 */

const logger = (() => {
  // Reuse the existing logger pattern (console-based) without importing a
  // module that may not exist. This file does not block on the logger.
  return {
    info: (...args) => console.log('[twapDetector]', ...args),
    debug: (...args) => {
      if ((process.env.LOG_LEVEL || '').toLowerCase() === 'debug') {
        console.log('[twapDetector]', ...args);
      }
    },
    error: (...args) => console.error('[twapDetector]', ...args),
  };
})();

// ---- tunables (env-overridable) ----
const TWAP_DEFAULT_DURATION_HOURS = parseFloat(
  process.env.TWAP_DEFAULT_DURATION_HOURS || '14'
);
const TWAP_BULLET_INTERVAL_MINUTES = parseFloat(
  process.env.TWAP_BULLET_INTERVAL_MINUTES || '28'
);
const TWAP_MIN_FILLS_TO_DETECT = parseInt(
  process.env.TWAP_MIN_FILLS_TO_DETECT || '3',
  10
);
const TWAP_DETECT_WINDOW_MINUTES = parseInt(
  process.env.TWAP_DETECT_WINDOW_MINUTES || '5',
  10
);

function isEnabled() {
  return (process.env.TWAP_AWARENESS_ENABLED || 'true').toLowerCase() !== 'false';
}

/**
 * Active TWAPs.
 *   key: wallet address (lowercase)
 *   val: { startedAt, expiresAt, fillCount, basketId }
 */
const _activeTWAPs = new Map();

/**
 * Recent open events used for sliding-window detection.
 *   key: wallet address (lowercase)
 *   val: array of { coin, timestamp }
 */
const _recentOpens = new Map();

function _now() {
  return Date.now();
}

function _lc(addr) {
  return String(addr || '').toLowerCase();
}

/**
 * Mark a TWAP as started for a wallet. Idempotent.
 */
function markTWAPStarted(wallet, basketId = null) {
  if (!isEnabled()) return;
  const w = _lc(wallet);
  if (!w) return;
  const start = _now();
  const expiresAt = start + TWAP_DEFAULT_DURATION_HOURS * 60 * 60 * 1000;

  // Reset (or start) the entry. If already active, extend the expiry to keep
  // the most-recent fill window honored.
  const existing = _activeTWAPs.get(w);
  if (existing) {
    existing.expiresAt = expiresAt;
    existing.basketId = basketId || existing.basketId;
    return;
  }

  _activeTWAPs.set(w, {
    startedAt: start,
    expiresAt,
    fillCount: 0,
    basketId,
  });
  logger.info(
    `TWAP marked started for ${w}, expires ${new Date(expiresAt).toISOString()}`
  );
}

/**
 * Increment fill count for an active TWAP. Tolerant of unknown wallets.
 */
function recordTWAPFill(wallet) {
  const state = _activeTWAPs.get(_lc(wallet));
  if (state) state.fillCount += 1;
}

/**
 * Mark a TWAP completed (e.g., 30 bullets reached). Optional — TWAPs also
 * auto-expire on isTWAPActive() based on `expiresAt`.
 */
function markTWAPCompleted(wallet) {
  const w = _lc(wallet);
  const state = _activeTWAPs.get(w);
  if (state) {
    logger.info(`TWAP completed for ${w}, ${state.fillCount} fills tracked`);
    _activeTWAPs.delete(w);
  }
}

/**
 * Returns true if the wallet currently has an active TWAP. Auto-expires
 * stale entries past their duration.
 */
function isTWAPActive(wallet) {
  if (!isEnabled()) return false;
  const w = _lc(wallet);
  const state = _activeTWAPs.get(w);
  if (!state) return false;
  if (_now() > state.expiresAt) {
    logger.info(`TWAP auto-expired for ${w}`);
    _activeTWAPs.delete(w);
    return false;
  }
  return true;
}

/**
 * Diagnostic accessor.
 */
function getTWAPInfo(wallet) {
  return _activeTWAPs.get(_lc(wallet)) || null;
}

/**
 * Record a single open event for sliding-window TWAP detection.
 *
 * If 3+ distinct coins appear for the same wallet within the last
 * `windowMinutes`, mark a TWAP as started. Designed to be invoked from the
 * monitor when a new position is detected — no extra HL API call required.
 */
function recordOpenEvent(wallet, coin, timestampMs = null) {
  if (!isEnabled()) return;
  const w = _lc(wallet);
  if (!w || !coin) return;
  const ts = Number.isFinite(timestampMs) ? timestampMs : _now();

  const arr = _recentOpens.get(w) || [];
  arr.push({ coin: String(coin).toUpperCase(), timestamp: ts });

  // Drop entries past detection window
  const cutoff = _now() - TWAP_DETECT_WINDOW_MINUTES * 60 * 1000;
  const recent = arr.filter((e) => e.timestamp >= cutoff);
  _recentOpens.set(w, recent);

  const distinctCoins = new Set(recent.map((e) => e.coin));
  if (
    distinctCoins.size >= TWAP_MIN_FILLS_TO_DETECT &&
    !isTWAPActive(w)
  ) {
    markTWAPStarted(w);
  }
}

/**
 * Bulk version of `recordOpenEvent` for callers that already aggregate
 * fills. Each fill should have at least { wallet, coin, timestamp }.
 */
function detectTWAPFromFills(fills, windowMinutes = TWAP_DETECT_WINDOW_MINUTES) {
  if (!Array.isArray(fills)) return;
  const cutoff = _now() - windowMinutes * 60 * 1000;
  const byWallet = new Map();
  for (const f of fills) {
    if (!f || !f.wallet || !f.coin) continue;
    const ts = Number.isFinite(f.timestamp) ? f.timestamp : _now();
    if (ts < cutoff) continue;
    const w = _lc(f.wallet);
    if (!byWallet.has(w)) byWallet.set(w, new Set());
    byWallet.get(w).add(String(f.coin).toUpperCase());
  }
  for (const [wallet, coins] of byWallet.entries()) {
    if (coins.size >= TWAP_MIN_FILLS_TO_DETECT && !isTWAPActive(wallet)) {
      markTWAPStarted(wallet);
    }
  }
}

/**
 * Test-only reset.
 */
function _resetForTests() {
  _activeTWAPs.clear();
  _recentOpens.clear();
}

module.exports = {
  // primary API
  isEnabled,
  isTWAPActive,
  markTWAPStarted,
  markTWAPCompleted,
  recordTWAPFill,
  recordOpenEvent,
  detectTWAPFromFills,
  getTWAPInfo,
  // tunables
  TWAP_DEFAULT_DURATION_HOURS,
  TWAP_BULLET_INTERVAL_MINUTES,
  TWAP_MIN_FILLS_TO_DETECT,
  TWAP_DETECT_WINDOW_MINUTES,
  // tests
  _resetForTests,
};
