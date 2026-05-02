'use strict';

/**
 * Round v2 — PnL cross-validation against Pear Protocol API.
 *
 * Fires only when the bot's calculated PnL is "suspicious":
 *   - |PnL| > $1000 absolute (default), OR
 *   - |PnL| / notional > 50%
 *
 * If suspicious, query Pear API for the position. If both agree within 10%,
 * accept the bot value. Otherwise log a warning + use Pear's value (Pear
 * is authoritative).
 *
 * Best-effort: if Pear API is unreachable, we degrade gracefully and still
 * deliver the alert with a soft "uncertainty" flag rather than blocking.
 */

const axios = require('axios');

const PEAR_API_BASE =
  process.env.PEAR_API_BASE || 'https://api.pear.garden';
const ABS_THRESHOLD_USD = parseFloat(
  process.env.PNL_CROSS_VALIDATION_THRESHOLD_USD || '1000'
);
const PCT_THRESHOLD = parseFloat(
  process.env.PNL_CROSS_VALIDATION_PCT_THRESHOLD || '0.50'
);
const DIFF_TOLERANCE = parseFloat(
  process.env.PNL_CROSS_VALIDATION_DIFF_TOLERANCE || '0.10'
);
const TIMEOUT_MS = parseInt(
  process.env.PNL_CROSS_VALIDATION_TIMEOUT_MS || '8000',
  10
);

function isEnabled() {
  return (
    (process.env.PNL_CROSS_VALIDATION_ENABLED || 'true').toLowerCase() !==
    'false'
  );
}

function _isSuspicious(calculatedPnl, notional) {
  if (!Number.isFinite(calculatedPnl)) return false;
  if (Math.abs(calculatedPnl) > ABS_THRESHOLD_USD) return true;
  if (notional > 0 && Math.abs(calculatedPnl) / notional > PCT_THRESHOLD) {
    return true;
  }
  return false;
}

async function _fetchPearPosition(wallet, coin) {
  try {
    const url = `${PEAR_API_BASE}/positions?address=${wallet}`;
    const res = await axios.get(url, {
      timeout: TIMEOUT_MS,
      validateStatus: () => true,
    });
    if (res.status !== 200 || !res.data) return null;
    let positions = [];
    if (Array.isArray(res.data)) {
      positions = res.data;
    } else if (Array.isArray(res.data.positions)) {
      positions = res.data.positions;
    } else if (Array.isArray(res.data.data)) {
      positions = res.data.data;
    }
    const target = (coin || '').toUpperCase();
    return (
      positions.find((p) => {
        const sym = (
          p.coin ||
          p.symbol ||
          p.ticker ||
          p.asset ||
          ''
        ).toUpperCase();
        return sym === target;
      }) || null
    );
  } catch (e) {
    console.warn(
      '[pnlCrossValidation] pear api fetch failed:',
      e && e.message ? e.message : e
    );
    return null;
  }
}

function _extractPearPnl(pos) {
  if (!pos || typeof pos !== 'object') return null;
  for (const k of [
    'realized_pnl',
    'realizedPnl',
    'closedPnl',
    'closed_pnl',
    'pnl',
    'total_pnl',
    'totalPnl',
  ]) {
    const v = pos[k];
    if (v != null) {
      const n = parseFloat(v);
      if (Number.isFinite(n)) return n;
    }
  }
  return null;
}

/**
 * validatePnlBeforeAlert — main entry.
 * Returns:
 *   { valid: true, pnl, source: 'bot'|'pear_api', flagged?: bool, note?: string }
 */
async function validatePnlBeforeAlert({
  wallet,
  coin,
  calculatedPnl,
  notional,
}) {
  if (!isEnabled()) {
    return { valid: true, pnl: calculatedPnl, source: 'bot' };
  }
  if (!_isSuspicious(calculatedPnl, notional)) {
    return { valid: true, pnl: calculatedPnl, source: 'bot' };
  }
  const pearPos = await _fetchPearPosition(wallet, coin);
  if (!pearPos) {
    return {
      valid: true,
      pnl: calculatedPnl,
      source: 'bot',
      flagged: true,
      note: 'Pear API unavailable — using bot calculation',
    };
  }
  const pearPnl = _extractPearPnl(pearPos);
  if (pearPnl == null) {
    return {
      valid: true,
      pnl: calculatedPnl,
      source: 'bot',
      flagged: true,
      note: 'Pear API has no PnL — using bot calculation',
    };
  }
  const denom =
    Math.abs(pearPnl) > 0 ? Math.abs(pearPnl) : Math.abs(calculatedPnl) || 1;
  const diff = Math.abs(calculatedPnl - pearPnl) / denom;
  if (diff > DIFF_TOLERANCE) {
    console.warn(
      `[pnlCrossValidation] discrepancy ${coin}: bot=${calculatedPnl} pear=${pearPnl} diff=${(diff * 100).toFixed(1)}%`
    );
    return {
      valid: true,
      pnl: pearPnl,
      source: 'pear_api',
      flagged: true,
      note: `Bot $${calculatedPnl.toFixed(2)} vs Pear $${pearPnl.toFixed(2)} — usando Pear`,
      bot_pnl: calculatedPnl,
      pear_pnl: pearPnl,
      diff_pct: diff * 100,
    };
  }
  return { valid: true, pnl: calculatedPnl, source: 'bot' };
}

module.exports = {
  isEnabled,
  validatePnlBeforeAlert,
  ABS_THRESHOLD_USD,
  PCT_THRESHOLD,
  DIFF_TOLERANCE,
  _isSuspicious,
};
