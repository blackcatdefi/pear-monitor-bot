'use strict';

/**
 * Round v2 — Compounding detector.
 *
 * If a tracked basket has the SAME positions (same coins + same sides) but
 * the total notional has grown >= COMPOUNDING_GROWTH_THRESHOLD between
 * snapshots, capital was added — a compound event. Emit a single alert when
 * the threshold is crossed; reset the snapshot afterwards so we don't keep
 * re-firing.
 *
 * State keyed by chatId:wallet so multiple users monitoring different
 * wallets each get their own tracker.
 */

const GROWTH_THRESHOLD = parseFloat(
  process.env.COMPOUNDING_GROWTH_THRESHOLD || '0.10'
);

function isEnabled() {
  return (
    (process.env.COMPOUNDING_DETECTOR_ENABLED || 'true').toLowerCase() !==
    'false'
  );
}

const _snapshots = new Map(); // key -> { notional, positionsKey, timestamp }

function _key(chatId, wallet) {
  return `${chatId}:${(wallet || '').toLowerCase()}`;
}

function _positionsKey(positions) {
  return [...positions]
    .filter((p) => p && p.coin)
    .map((p) => {
      const side = p.side || (p.size < 0 ? 'SHORT' : 'LONG');
      return `${String(p.coin).toUpperCase()}:${side}`;
    })
    .sort()
    .join(',');
}

function _notionalOf(positions) {
  return positions.reduce((sum, p) => {
    const sz = Math.abs(p.size || 0);
    const px = p.markPrice || p.entryPrice || 0;
    return sum + sz * px;
  }, 0);
}

/**
 * Returns:
 *   { type: 'NONE' }
 *   { type: 'COMPOUND_DETECTED', prevNotional, currentNotional, growth }
 */
function checkForCompounding(chatId, wallet, currentPositions) {
  if (!isEnabled()) return { type: 'DISABLED' };
  const key = _key(chatId, wallet);
  if (!Array.isArray(currentPositions) || currentPositions.length === 0) {
    _snapshots.delete(key);
    return { type: 'NONE' };
  }
  const currentNotional = _notionalOf(currentPositions);
  const positionsKey = _positionsKey(currentPositions);
  const prev = _snapshots.get(key);

  if (!prev) {
    _snapshots.set(key, {
      notional: currentNotional,
      positionsKey,
      timestamp: Date.now(),
    });
    return { type: 'NONE' };
  }

  if (prev.positionsKey !== positionsKey) {
    _snapshots.set(key, {
      notional: currentNotional,
      positionsKey,
      timestamp: Date.now(),
    });
    return { type: 'NONE' };
  }

  if (prev.notional <= 0) {
    _snapshots.set(key, {
      notional: currentNotional,
      positionsKey,
      timestamp: Date.now(),
    });
    return { type: 'NONE' };
  }

  const growth = (currentNotional - prev.notional) / prev.notional;
  if (growth >= GROWTH_THRESHOLD) {
    const result = {
      type: 'COMPOUND_DETECTED',
      prevNotional: prev.notional,
      currentNotional,
      growth,
    };
    _snapshots.set(key, {
      notional: currentNotional,
      positionsKey,
      timestamp: Date.now(),
    });
    return result;
  }

  return { type: 'NONE' };
}

function formatCompoundAlert(label, result) {
  const pct = (result.growth * 100).toFixed(1);
  return [
    '🔄 *COMPOUNDING DETECTED*',
    '',
    `📍 Wallet: ${label}`,
    `Active basket size has been increased.`,
    '',
    `Previous notional: $${Math.round(result.prevNotional).toLocaleString()}`,
    `Current notional: $${Math.round(result.currentNotional).toLocaleString()}`,
    `Growth: +${pct}%`,
    '',
    `Positions are unchanged (same tokens, same side).`,
    `Capital was added to the position — compounding (TWAP entry).`,
  ].join('\n');
}

function _resetForTests() {
  _snapshots.clear();
}

module.exports = {
  isEnabled,
  checkForCompounding,
  formatCompoundAlert,
  GROWTH_THRESHOLD,
  _resetForTests,
};
