'use strict';

/**
 * R(v3) — Funds Available Gate.
 *
 * Suppresses the "💰 Funds available to trade!" alert spam BCD saw on
 * 2026-04-30 between 11:58 and 12:13 UTC during a v6 basket TWAP fill.
 * Each bullet of the TWAP frees and immediately reuses ~$50–$100 of
 * margin, which the old edge-triggered logic surfaced as 5+ alerts in
 * 15 minutes.
 *
 * Three gates, evaluated in order:
 *   1. TWAP_ACTIVE — wallet has an active TWAP → never fire
 *   2. BELOW_THRESHOLD_<N> — amount below MIN_FUNDS_THRESHOLD_USD → drop residual
 *   3. RECENTLY_ALERTED — same wallet/amount-bucket fired within DEDUPE_WINDOW_MS → drop
 *
 * If none of the gates trip, returns shouldFire=true and registers the
 * dedup key.
 */

const { isTWAPActive } = require('./twapDetector');

const MIN_FUNDS_THRESHOLD_USD = parseFloat(
  process.env.FUNDS_AVAILABLE_THRESHOLD_USD || '200'
);
const DEDUPE_WINDOW_MS =
  parseFloat(process.env.FUNDS_AVAILABLE_DEDUPE_WINDOW_MIN || '60') *
  60 *
  1000;

function isEnabled() {
  // Off-switch keeps a clean rollback path if anything misbehaves in prod.
  return (
    (process.env.FUNDS_AVAILABLE_GATE_ENABLED || 'true').toLowerCase() !==
    'false'
  );
}

const _recentlyAlerted = new Map(); // key: wallet:bucket  -> timestamp

function _now() {
  return Date.now();
}

/**
 * Decide whether the bot should send a "Funds available" alert now.
 *
 * Returns: { shouldFire: boolean, reason: string }
 *
 * Reason codes:
 *   GATE_DISABLED        — env var disables this gate; pass-through allow
 *   TWAP_ACTIVE          — wallet has an active TWAP
 *   BELOW_THRESHOLD_<N>  — amount below MIN_FUNDS_THRESHOLD_USD
 *   RECENTLY_ALERTED     — same wallet+bucket fired within DEDUPE_WINDOW_MS
 *   OK                   — fire and register dedup key
 */
function shouldFireFundsAvailable(wallet, availableAmount) {
  if (!isEnabled()) {
    return { shouldFire: true, reason: 'GATE_DISABLED' };
  }

  const wlc = String(wallet || '').toLowerCase();

  // Gate 1: TWAP gate
  if (isTWAPActive(wlc)) {
    return { shouldFire: false, reason: 'TWAP_ACTIVE' };
  }

  // Gate 2: threshold gate
  const amount = Number.isFinite(availableAmount) ? availableAmount : 0;
  if (amount < MIN_FUNDS_THRESHOLD_USD) {
    return {
      shouldFire: false,
      reason: `BELOW_THRESHOLD_${MIN_FUNDS_THRESHOLD_USD}`,
    };
  }

  // Gate 3: dedupe gate (bucket by $100)
  const bucket = Math.floor(amount / 100) * 100;
  const key = `${wlc}:${bucket}`;
  const last = _recentlyAlerted.get(key);
  if (last && _now() - last < DEDUPE_WINDOW_MS) {
    return { shouldFire: false, reason: 'RECENTLY_ALERTED' };
  }

  _recentlyAlerted.set(key, _now());
  return { shouldFire: true, reason: 'OK' };
}

/**
 * Periodic cleanup of stale dedupe entries. Setup in module load so
 * tests can avoid running it. The interval is unref()'d so it does
 * not block process exit.
 */
function _cleanup() {
  const cutoff = _now() - DEDUPE_WINDOW_MS * 2;
  for (const [k, ts] of _recentlyAlerted.entries()) {
    if (ts < cutoff) _recentlyAlerted.delete(k);
  }
}

const _cleanupInterval = setInterval(_cleanup, 60 * 60 * 1000);
if (typeof _cleanupInterval.unref === 'function') {
  _cleanupInterval.unref();
}

function _resetForTests() {
  _recentlyAlerted.clear();
}

module.exports = {
  isEnabled,
  shouldFireFundsAvailable,
  MIN_FUNDS_THRESHOLD_USD,
  DEDUPE_WINDOW_MS,
  _resetForTests,
  _cleanup,
};
