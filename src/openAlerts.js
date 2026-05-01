'use strict';

/**
 * Round v2 — OPEN-event detection.
 *
 * Counterpart to closeAlerts.js. When the tracked wallet opens a new position,
 * alert. If 3+ new positions appear within 5 minutes for the same wallet,
 * treat it as a basket OPEN and emit a consolidated message instead of N
 * individual alerts.
 *
 * Edge-triggered: this module owns no state; the caller passes the previous
 * snapshot and current snapshot. The dedupe layer is shouldSendAlert from
 * closeAlerts.js (shared 60s window per wallet:coin).
 */

const { shouldSendAlert } = require('./closeAlerts');
const pearUrlBuilder = require('./pearUrlBuilder');

const BASKET_WINDOW_MS = 5 * 60 * 1000;
const BASKET_MIN_COUNT = 3;

function isEnabled() {
  return (process.env.OPEN_ALERTS_ENABLED || 'true').toLowerCase() !== 'false';
}

/**
 * Diff currentPositions vs lastSnapshot. A "new" position is one whose
 * (coin, dex) combo wasn't in the snapshot. Returns the array of new
 * positions; the caller decides whether to fire individual or basket alert.
 */
function findNewPositions(currentPositions, lastSnapshot) {
  if (!Array.isArray(currentPositions)) return [];
  const prev = Array.isArray(lastSnapshot) ? lastSnapshot : [];
  const prevSet = new Set(
    prev.map((p) => `${(p.coin || '').toUpperCase()}:${p.dex || 'Native'}`)
  );
  const out = [];
  for (const pos of currentPositions) {
    const key = `${(pos.coin || '').toUpperCase()}:${pos.dex || 'Native'}`;
    if (!prevSet.has(key)) out.push(pos);
  }
  return out;
}

/**
 * Decide BASKET_OPEN vs INDIVIDUAL_OPEN. We treat 3+ new positions detected
 * in the same poll cycle as a basket open. (Pear baskets place all legs in
 * a single TWAP burst, so they will all surface within the same poll.)
 */
function classifyOpenEvent(newPositions) {
  if (!Array.isArray(newPositions) || newPositions.length === 0) {
    return { type: 'NONE', positions: [] };
  }
  if (newPositions.length >= BASKET_MIN_COUNT) {
    return { type: 'BASKET_OPEN', positions: newPositions };
  }
  return { type: 'INDIVIDUAL_OPEN', positions: newPositions };
}

function _fmtPx(n) {
  if (!Number.isFinite(n) || n <= 0) return '?';
  if (n >= 100) return n.toFixed(2);
  if (n >= 1) return n.toFixed(4);
  return n.toFixed(6);
}

function _fmtUsd(n) {
  if (!Number.isFinite(n)) return '$0';
  return `$${Math.round(n).toLocaleString()}`;
}

function formatBasketOpenAlert(label, positions) {
  const totalNotional = positions.reduce(
    (s, p) =>
      s +
      Math.abs(
        (p.size || 0) * (p.entryPrice || p.markPrice || 0)
      ),
    0
  );
  const lev = positions[0] && positions[0].leverage
    ? `${positions[0].leverage}x`
    : '4x';
  const lines = [
    '🚀 *NUEVA BASKET ABIERTA*',
    '',
    `📍 Wallet: ${label}`,
    `📊 Composición (${positions.length} posiciones):`,
  ];
  for (const p of positions) {
    const side = p.side || (p.size < 0 ? 'SHORT' : 'LONG');
    lines.push(`  • ${p.coin} ${side} @ $${_fmtPx(p.entryPrice)}`);
  }
  lines.push('');
  lines.push(`💰 Notional total: ${_fmtUsd(totalNotional)}`);
  lines.push(`⚡ Leverage: ${lev}`);
  lines.push(`🎯 Estrategia: TWAP entry (DCA temporal)`);
  return lines.join('\n');
}

function formatIndividualOpenAlert(label, pos) {
  const side = pos.side || (pos.size < 0 ? 'SHORT' : 'LONG');
  const emoji = side === 'SHORT' ? '🔴' : '🟢';
  const notional = Math.abs(
    (pos.size || 0) * (pos.entryPrice || pos.markPrice || 0)
  );
  const lev = pos.leverage ? `${pos.leverage}x` : '4x';
  return [
    `${emoji} *NUEVA POSICIÓN ABIERTA*`,
    '',
    `📍 Wallet: ${label}`,
    `🪙 ${pos.coin} ${side}`,
    `💲 Entry: $${_fmtPx(pos.entryPrice)}`,
    `📦 Size: ${Math.abs(pos.size || 0).toLocaleString()}`,
    `💰 Notional: ${_fmtUsd(notional)}`,
    `⚡ Leverage: ${lev}`,
  ].join('\n');
}

/**
 * Convenience: given the diff result, emit alerts via the supplied notifier.
 * The notifier signature is (wallet, coin, message) so the dedupe layer can
 * key on (wallet, coin). For basket opens we use a synthetic coin "BASKET".
 */
function _enrichWithNotional(positions) {
  return (positions || []).map((p) => {
    if (Number.isFinite(p && p.notional) && p.notional > 0) return p;
    const sz = Math.abs(Number(p && p.size) || 0);
    const px = Number((p && (p.entryPrice || p.markPrice)) || 0);
    return Object.assign({}, p, { notional: sz * px });
  });
}

async function emitAlerts({ chatId, wallet, label, newPositions, notify }) {
  if (!isEnabled()) return { dispatched: 0, type: 'DISABLED' };
  const ev = classifyOpenEvent(newPositions);
  if (ev.type === 'NONE') return { dispatched: 0, type: 'NONE' };

  if (ev.type === 'BASKET_OPEN') {
    if (shouldSendAlert(wallet, 'BASKET_OPEN')) {
      const msg = formatBasketOpenAlert(label, ev.positions);
      const keyboard = pearUrlBuilder.buildInlineKeyboard(
        _enrichWithNotional(ev.positions)
      );
      await notify(chatId, msg, keyboard ? { reply_markup: keyboard } : undefined);
      return { dispatched: 1, type: 'BASKET_OPEN' };
    }
    return { dispatched: 0, type: 'BASKET_OPEN_DEDUPED' };
  }

  let count = 0;
  for (const pos of ev.positions) {
    if (!shouldSendAlert(wallet, `OPEN_${pos.coin}`)) continue;
    const msg = formatIndividualOpenAlert(label, pos);
    const keyboard = pearUrlBuilder.buildInlineKeyboard(
      _enrichWithNotional([pos])
    );
    await notify(chatId, msg, keyboard ? { reply_markup: keyboard } : undefined);
    count += 1;
  }
  return { dispatched: count, type: 'INDIVIDUAL_OPEN' };
}

module.exports = {
  isEnabled,
  findNewPositions,
  classifyOpenEvent,
  formatBasketOpenAlert,
  formatIndividualOpenAlert,
  emitAlerts,
  BASKET_WINDOW_MS,
  BASKET_MIN_COUNT,
};
