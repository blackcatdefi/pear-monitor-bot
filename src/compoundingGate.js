'use strict';

/**
 * R(v3) — Compounding Gate.
 *
 * Wraps the existing compoundingDetector with TWAP-awareness so the bot
 * stops firing the "🔄 COMPOUNDING DETECTADO" false positive that BCD saw
 * on 2026-04-30 11:55 UTC ("Notional anterior $20,227 → $22,454 (+11%)").
 * That growth was a TWAP filling progressively, not BCD adding capital.
 *
 * Independent of compoundingDetector.js — this module owns its own
 * snapshots so its decisions are deterministic and testable.
 *
 * Three gates, evaluated in order:
 *   1. TWAP_ACTIVE              — wallet has an active TWAP → suppress
 *   2. POST_TWAP_COOLDOWN       — TWAP completed less than COOLDOWN ago → suppress
 *   3. NO_PREV_SNAPSHOT         — first sighting → take snapshot, no fire
 *   4. POSITIONS_CHANGED        — composition changed → take snapshot, no fire
 *   5. BELOW_THRESHOLD          — growth < 10% → no fire
 *   6. ACCOUNT_VALUE_NOT_GROWN  — notional grew but margin shifted, not capital → no fire
 *   7. OK                       — real compounding event
 */

const { isTWAPActive } = require('./twapDetector');

const COMPOUNDING_GROWTH_THRESHOLD = parseFloat(
  process.env.COMPOUNDING_GROWTH_THRESHOLD || '0.10'
);
const POST_TWAP_COOLDOWN_MS =
  parseFloat(process.env.COMPOUNDING_POST_TWAP_COOLDOWN_MIN || '30') *
  60 *
  1000;

function isEnabled() {
  return (
    (process.env.COMPOUNDING_GATE_ENABLED || 'true').toLowerCase() !== 'false'
  );
}

const _walletSnapshots = new Map();
const _twapCompletionTime = new Map();

function _now() {
  return Date.now();
}

function _lc(s) {
  return String(s || '').toLowerCase();
}

function positionsToKey(positions) {
  if (!Array.isArray(positions)) return '';
  return positions
    .filter((p) => p && p.coin)
    .map((p) => {
      const side = p.side || (Number(p.size) < 0 ? 'SHORT' : 'LONG');
      return `${String(p.coin).toUpperCase()}:${side}`;
    })
    .sort()
    .join('|');
}

function takeSnapshot(wallet, positions, notional, accountValue) {
  _walletSnapshots.set(_lc(wallet), {
    positions: positions || [],
    positionsKey: positionsToKey(positions),
    notional: Number(notional) || 0,
    accountValue: Number(accountValue) || 0,
    timestamp: _now(),
  });
}

/**
 * Decide whether a real compounding event occurred for `wallet`.
 *
 * Returns: { isCompounding: boolean, reason: string, ... }
 */
function detectCompounding(
  wallet,
  currentPositions,
  currentNotional,
  currentAccountValue
) {
  if (!isEnabled()) {
    // Pass-through; caller may still decide to fire based on legacy detector.
    return { isCompounding: false, reason: 'GATE_DISABLED' };
  }

  const w = _lc(wallet);
  if (!w) return { isCompounding: false, reason: 'NO_WALLET' };

  // GATE 1: TWAP active
  if (isTWAPActive(w)) {
    return { isCompounding: false, reason: 'TWAP_ACTIVE' };
  }

  // GATE 2: Post-TWAP cooldown
  const twapEnd = _twapCompletionTime.get(w);
  if (twapEnd && _now() - twapEnd < POST_TWAP_COOLDOWN_MS) {
    return { isCompounding: false, reason: 'POST_TWAP_COOLDOWN' };
  }

  // GATE 3: previous snapshot required
  const prev = _walletSnapshots.get(w);
  if (!prev) {
    takeSnapshot(w, currentPositions, currentNotional, currentAccountValue);
    return { isCompounding: false, reason: 'NO_PREV_SNAPSHOT' };
  }

  // GATE 4: composition stability
  const currentKey = positionsToKey(currentPositions);
  if (prev.positionsKey !== currentKey) {
    takeSnapshot(w, currentPositions, currentNotional, currentAccountValue);
    return { isCompounding: false, reason: 'POSITIONS_CHANGED' };
  }

  // GATE 5: notional growth threshold
  const prevNotional = prev.notional || 0;
  if (prevNotional <= 0) {
    takeSnapshot(w, currentPositions, currentNotional, currentAccountValue);
    return { isCompounding: false, reason: 'PREV_NOTIONAL_ZERO' };
  }
  const notionalGrowth = (currentNotional - prevNotional) / prevNotional;
  if (notionalGrowth < COMPOUNDING_GROWTH_THRESHOLD) {
    return { isCompounding: false, reason: 'BELOW_THRESHOLD' };
  }

  // GATE 6: account value must have grown too. If only notional grew,
  // BCD probably just shifted margin between positions — not real
  // compounding. Threshold = 50% of notional growth threshold so margin
  // shifts don't sneak through but real adds always pass.
  const prevAcct = prev.accountValue || 0;
  if (prevAcct > 0) {
    const acctGrowth = (currentAccountValue - prevAcct) / prevAcct;
    if (acctGrowth < COMPOUNDING_GROWTH_THRESHOLD * 0.5) {
      // Update snapshot still — but report no fire so we don't keep flagging.
      takeSnapshot(w, currentPositions, currentNotional, currentAccountValue);
      return { isCompounding: false, reason: 'ACCOUNT_VALUE_NOT_GROWN' };
    }
  }

  // All gates passed
  takeSnapshot(w, currentPositions, currentNotional, currentAccountValue);
  return {
    isCompounding: true,
    reason: 'OK',
    prevNotional,
    newNotional: currentNotional,
    growthPct: notionalGrowth * 100,
  };
}

/**
 * Caller marks a TWAP as completed → start the post-TWAP cooldown.
 */
function markTWAPCompletedAt(wallet) {
  _twapCompletionTime.set(_lc(wallet), _now());
}

function _resetForTests() {
  _walletSnapshots.clear();
  _twapCompletionTime.clear();
}

module.exports = {
  isEnabled,
  takeSnapshot,
  detectCompounding,
  markTWAPCompletedAt,
  positionsToKey,
  COMPOUNDING_GROWTH_THRESHOLD,
  POST_TWAP_COOLDOWN_MS,
  _resetForTests,
};
