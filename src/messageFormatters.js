'use strict';

/**
 * Round v2 — Rich message formatters.
 *
 * Adds duration, ROI%, basket version, and other context to alert messages.
 * Uses i18n + branding helpers.
 */

const { t } = require('./i18n');
const { appendFooter } = require('./branding');

const REASON_DISPLAY = {
  TAKE_PROFIT: { emoji: '🎯', label: 'TAKE PROFIT hit' },
  STOP_LOSS: { emoji: '🛑', label: 'STOP LOSS triggered' },
  TRAILING_STOP: { emoji: '🔄', label: 'TRAILING STOP triggered' },
  TRAILING_OR_MANUAL: { emoji: '🔄', label: 'Position closed (trailing/manual)' },
  MANUAL_CLOSE: { emoji: '📋', label: 'Manual close' },
};

function formatDurationMs(ms) {
  if (!Number.isFinite(ms) || ms < 0) return '0m';
  const days = Math.floor(ms / (24 * 60 * 60 * 1000));
  const hours = Math.floor((ms % (24 * 60 * 60 * 1000)) / (60 * 60 * 1000));
  const minutes = Math.floor((ms % (60 * 60 * 1000)) / (60 * 1000));
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function _fmtPx(n) {
  if (!Number.isFinite(n) || n <= 0) return '?';
  if (n >= 100) return n.toFixed(2);
  if (n >= 1) return n.toFixed(4);
  return n.toFixed(6);
}

function _fmtUsd(n) {
  if (!Number.isFinite(n)) return '$0.00';
  const abs = Math.abs(n).toFixed(2);
  return n >= 0 ? `+$${abs}` : `-$${abs}`;
}

/**
 * Rich close alert.
 *  close: { coin, side, openedAt, exitTimestamp, entryPrice, exitPrice,
 *           size, pnl, fees, basketVersion, dexTag, entryNotional }
 *  reason: TAKE_PROFIT | STOP_LOSS | TRAILING_STOP | MANUAL_CLOSE
 *  isPrimary: bool — controls whether referral footer is appended
 */
function formatRichCloseAlert(label, close, reason, isPrimary = true) {
  const meta = REASON_DISPLAY[reason] || REASON_DISPLAY.MANUAL_CLOSE;
  const pnl = close.pnl || 0;
  const pnlEmoji = pnl >= 0 ? '🟢' : '🔴';
  const notional = close.entryNotional ||
    Math.abs((close.size || 0) * (close.entryPrice || 0));
  const roiPct = notional > 0 ? (pnl / notional) * 100 : 0;
  const openedAt = close.openedAt
    ? Date.parse(close.openedAt) || Date.now()
    : Date.now();
  const exitAt = close.exitTimestamp || Date.now();
  const duration = formatDurationMs(exitAt - openedAt);
  const basketTag = close.basketVersion
    ? ` (basket ${close.basketVersion})`
    : '';

  const lines = [
    `${meta.emoji} *${meta.label}*`,
    '',
    `📍 Wallet: ${label}${basketTag}`,
    `🪙 ${close.coin}${close.dexTag || ''} ${close.side || ''}`.trim(),
    `${pnlEmoji} PnL: ${_fmtUsd(pnl)} (${roiPct.toFixed(2)}%)`,
    `💲 Entry: $${_fmtPx(close.entryPrice)}`,
    close.exitPrice ? `💲 Exit: $${_fmtPx(close.exitPrice)}` : '',
    `⏱ Duration: ${duration}`,
  ];
  if (close.size) {
    lines.push(`📦 Size: ${Math.abs(close.size).toLocaleString()}`);
  }
  if (notional > 0) {
    lines.push(
      `💰 Notional: $${Math.round(notional).toLocaleString()}`
    );
  }
  if (Number.isFinite(close.fees) && close.fees > 0) {
    lines.push(`💸 Fees: $${close.fees.toFixed(2)}`);
  }

  return appendFooter(lines.filter(Boolean).join('\n'), isPrimary);
}

function formatRichBasketSummary(label, closes, isPrimary = true) {
  const items = Array.isArray(closes) ? closes : [];
  const totalPnl = items.reduce((s, c) => s + (c.pnl || 0), 0);
  const totalFees = items.reduce((s, c) => s + (c.fees || 0), 0);
  const sorted = [...items].sort((a, b) => (b.pnl || 0) - (a.pnl || 0));
  const symbols = items.map((c) => c.coin).join(', ');
  const pnlEmoji = totalPnl >= 0 ? '🟢' : '🔴';
  const notional = items.reduce(
    (s, c) =>
      s +
      (c.entryNotional ||
        Math.abs((c.size || 0) * (c.entryPrice || 0))),
    0
  );
  const roi = notional > 0 ? (totalPnl / notional) * 100 : 0;

  const lines = [
    `🐱‍⬛ *BASKET CLOSED* — ${label}`,
    '',
    '📊 *Summary:*',
    `• Closed positions: *${items.length}* (${symbols})`,
    `• ${pnlEmoji} Total PnL: *${_fmtUsd(totalPnl)}* (${roi.toFixed(2)}% ROI)`,
  ];
  if (totalFees) lines.push(`• Fees: $${totalFees.toFixed(2)}`);
  if (notional > 0) {
    lines.push(`• Notional: $${Math.round(notional).toLocaleString()}`);
  }
  lines.push('', '📋 *Breakdown (best → worst):*');
  for (const c of sorted) {
    const e = (c.pnl || 0) >= 0 ? '🟢' : '🔴';
    const side = c.side ? ` ${c.side}` : '';
    lines.push(`  ${e} ${c.coin}${side}: ${_fmtUsd(c.pnl || 0)}`);
  }
  return appendFooter(lines.join('\n'), isPrimary);
}

module.exports = {
  formatRichCloseAlert,
  formatRichBasketSummary,
  formatDurationMs,
  REASON_DISPLAY,
};
