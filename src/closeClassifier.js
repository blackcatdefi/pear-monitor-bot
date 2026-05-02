'use strict';

/**
 * Round v2 — Partial vs full close classifier + Trailing vs Manual upgrade.
 *
 * Two bug classes:
 *   1. Partial close noise: a TWAP slice during a basket close reduces the
 *      position size but the position is still open. Old code treated any
 *      size reduction as a close. We need to classify and silently track
 *      partial closes; only emit a close alert when size goes to 0.
 *   2. TRAILING_OR_MANUAL bucket too coarse: split into TRAILING_STOP vs
 *      MANUAL_CLOSE based on whether positionConfig.trailingStopPct exists.
 *
 * Reasons returned: PARTIAL_CLOSE, FULL_CLOSE, TRAILING_STOP, MANUAL_CLOSE.
 */

const PARTIAL_THRESHOLD = 0.95; // current size < prev * 0.95 = partial close

function isPartialClose(prevPos, currentPos) {
  if (!prevPos || !currentPos) return false;
  const prevSize = Math.abs(prevPos.size || 0);
  const curSize = Math.abs(currentPos.size || 0);
  if (prevSize <= 0) return false;
  if (curSize === 0) return false;
  return curSize < prevSize * PARTIAL_THRESHOLD;
}

function isFullClose(prevPos, currentPos) {
  if (!prevPos) return false;
  if (!currentPos) return true;
  return Math.abs(currentPos.size || 0) === 0;
}

function classifyCloseEvent(prevPos, currentPos) {
  if (!prevPos) return { type: 'OTHER' };
  if (currentPos && Math.abs(currentPos.size || 0) > 0) {
    if (isPartialClose(prevPos, currentPos)) {
      const sizeReduction =
        Math.abs(prevPos.size || 0) - Math.abs(currentPos.size || 0);
      return { type: 'PARTIAL_CLOSE', sizeReduction };
    }
    return { type: 'STILL_OPEN' };
  }
  return { type: 'FULL_CLOSE' };
}

/**
 * Refines TRAILING_OR_MANUAL into TRAILING_STOP vs MANUAL_CLOSE.
 * Inputs:
 *   - reason: result of closeAlerts.classifyCloseReason
 *   - positionConfig: optional {trailingStopPct, ...} pulled from store
 */
function refineTrailingVsManual(reason, positionConfig) {
  if (reason !== 'TRAILING_OR_MANUAL') return reason;
  const hadTrailing =
    positionConfig &&
    Number.isFinite(parseFloat(positionConfig.trailingStopPct));
  return hadTrailing ? 'TRAILING_STOP' : 'MANUAL_CLOSE';
}

const REASON_DISPLAY = {
  TAKE_PROFIT: { emoji: '🎯', label: 'TAKE PROFIT hit' },
  STOP_LOSS: { emoji: '🛑', label: 'STOP LOSS triggered' },
  TRAILING_STOP: { emoji: '🔄', label: 'TRAILING STOP triggered' },
  TRAILING_OR_MANUAL: { emoji: '🔄', label: 'Closed (trailing/manual)' },
  MANUAL_CLOSE: { emoji: '📋', label: 'Manual close' },
};

// Partial-close in-memory tracker. Used for aggregate PnL accounting if
// needed. Resets when position goes to size 0.
const _partialTracker = new Map(); // wallet:coin -> [{ts, sizeReduction}]

function trackPartialClose(wallet, coin, sizeReduction) {
  const key = `${(wallet || '').toLowerCase()}:${(coin || '').toUpperCase()}`;
  if (!_partialTracker.has(key)) _partialTracker.set(key, []);
  _partialTracker.get(key).push({
    timestamp: Date.now(),
    sizeReduction: Math.abs(sizeReduction),
  });
}

function getPartialHistory(wallet, coin) {
  const key = `${(wallet || '').toLowerCase()}:${(coin || '').toUpperCase()}`;
  return _partialTracker.get(key) || [];
}

function clearPartialHistory(wallet, coin) {
  const key = `${(wallet || '').toLowerCase()}:${(coin || '').toUpperCase()}`;
  _partialTracker.delete(key);
}

function _resetForTests() {
  _partialTracker.clear();
}

module.exports = {
  isPartialClose,
  isFullClose,
  classifyCloseEvent,
  refineTrailingVsManual,
  REASON_DISPLAY,
  trackPartialClose,
  getPartialHistory,
  clearPartialHistory,
  PARTIAL_THRESHOLD,
  _resetForTests,
};
