'use strict';

/**
 * R-BASKET (3 may 2026) — Compact OPEN/CLOSE message templates.
 *
 * Replaces the per-leg "🚀 NEW POSITION OPENED" + "✅ POSITION CLOSED" loops.
 * One message per basket lifecycle, ≤12 visible lines, designed to fit a
 * mobile screen without scrolling.
 *
 * Templating rules (informed by the spec, kept Markdown-compatible because
 * the rest of the bot uses parse_mode=Markdown — full HTML migration is
 * Phase 2):
 *   • compact OPEN  : 1 trader line + 1 line per leg + notional summary
 *   • expanded OPEN : auto when legs.length ≥ 3 OR notional ≥ $50k
 *   • compact CLOSE : 1 trader line + 1 line per leg's entry→exit + PnL
 *   • loss CLOSE    : 5-line short variant
 *   • exactly one emoji per concept (🟢 long, 🔴 short, ✅ win, ❌ loss)
 *   • always includes the Pear referral CTA line at the bottom
 */

const REFERRAL_CODE = process.env.PEAR_REFERRAL_CODE || 'BlackCatDeFi';
const PEAR_REF_URL =
  process.env.PEAR_HERO_URL || `https://app.pear.garden/?referral=${REFERRAL_CODE}`;
// Compact CTA used in the message footer. Kept short for mobile rendering, but
// MUST carry the canonical referral=<code> query param so the sanitizer's
// `isAllowedBlackCat` check passes (any other shape of "blackcat" string in a
// user-facing literal is a regression — see tests/sanitizer.test.js).
const COMPACT_REF_URL =
  process.env.PEAR_COMPACT_REF_URL ||
  `pear.garden/?referral=${REFERRAL_CODE}`;

const EXPAND_THRESHOLD_LEGS = 3;
const EXPAND_THRESHOLD_NOTIONAL = 50_000;

function _shortAddr(addr) {
  const a = String(addr || '');
  if (a.length < 12) return a;
  return `${a.slice(0, 6)}...${a.slice(-4)}`;
}

function _fmtPx(n) {
  const num = Number(n);
  if (!Number.isFinite(num) || num <= 0) return '?';
  if (num >= 100) return num.toFixed(2);
  if (num >= 1) return num.toFixed(4);
  return num.toFixed(6);
}

function _fmtUsdK(n) {
  const num = Number(n);
  if (!Number.isFinite(num) || num <= 0) return '$0';
  if (num >= 1000) return `$${Math.round(num / 100) / 10}k`;
  return `$${Math.round(num)}`;
}

function _fmtUsd(n) {
  const num = Number(n);
  if (!Number.isFinite(num)) return '$0';
  return `$${Math.round(num).toLocaleString()}`;
}

function _legNotional(p) {
  const sz = Math.abs(Number(p.size) || 0);
  const px = Number(p.entryPrice || p.entryPx || p.markPrice) || 0;
  return sz * px;
}

function _normSide(p) {
  if (p.side) return String(p.side).toUpperCase();
  return Number(p.size) < 0 ? 'SHORT' : 'LONG';
}

function _sideEmoji(side) {
  return String(side).toUpperCase() === 'SHORT' ? '🔴' : '🟢';
}

function _sideLetter(side) {
  return String(side).toUpperCase() === 'SHORT' ? 'S' : 'L';
}

function _gross(legs) {
  return (legs || []).reduce((sum, p) => sum + _legNotional(p), 0);
}

/**
 * Render a basket OPEN alert.
 *
 *   ctx = {
 *     traderLabel: 'huf.eth' | null,
 *     traderAddr:  '0x…',
 *     legs:        [...],
 *     leverage?:   number,
 *   }
 */
function renderBasketOpen(ctx) {
  const legs = (ctx && ctx.legs) || [];
  const trader = (ctx && ctx.traderLabel) || _shortAddr(ctx && ctx.traderAddr);
  const gross = _gross(legs);
  const expand =
    legs.length >= EXPAND_THRESHOLD_LEGS || gross >= EXPAND_THRESHOLD_NOTIONAL;

  if (legs.length === 1) {
    // Single-leg trade — keep it tight, no "basket" heading.
    const p = legs[0];
    const side = _normSide(p);
    const emoji = _sideEmoji(side);
    const notional = _legNotional(p);
    const lev =
      p.leverage || (ctx && ctx.leverage) ? `${p.leverage || ctx.leverage}x` : null;
    const lines = [
      `🍐 NEW TRADE — ${trader}`,
      '━━━━━━━━━━━━━━━━━━━━',
      `${emoji} ${side} ${p.coin}  ${_fmtUsdK(notional)}${lev ? `  ${lev}` : ''}  @ $${_fmtPx(p.entryPrice || p.entryPx)}`,
      `🔗 ${COMPACT_REF_URL}`,
    ];
    return lines.join('\n');
  }

  if (expand) {
    const lines = [
      `🍐 NEW BASKET — ${trader}`,
      '━━━━━━━━━━━━━━━━━━━━━━',
      `🆔 ${legs.length} legs`,
    ];
    for (const p of legs) {
      const side = _normSide(p);
      const emoji = _sideEmoji(side);
      const notional = _legNotional(p);
      const lev = p.leverage ? `${p.leverage}x` : null;
      lines.push(
        `${emoji} ${side.padEnd(5)} ${p.coin.padEnd(5)} ${_fmtUsdK(notional)}${lev ? ` ${lev}` : ''}  @ $${_fmtPx(p.entryPrice || p.entryPx)}`
      );
    }
    lines.push(`📊 Gross ${_fmtUsdK(gross)}  •  Δ ≈ 0`);
    lines.push(`🔗 ${COMPACT_REF_URL}`);
    return lines.join('\n');
  }

  // 2 legs, compact pair-trade card
  const lines = [`🍐 NEW PAIR TRADE — ${trader}`, '━━━━━━━━━━━━━━━━━━━━'];
  for (const p of legs) {
    const side = _normSide(p);
    const emoji = _sideEmoji(side);
    const notional = _legNotional(p);
    const lev = p.leverage ? `${p.leverage}x` : null;
    lines.push(
      `${emoji} ${side.padEnd(5)} ${p.coin.padEnd(4)} ${_fmtUsdK(notional)}${lev ? ` ${lev}` : ''}  @ $${_fmtPx(p.entryPrice || p.entryPx)}`
    );
  }
  lines.push(`📊 Notional ${_fmtUsdK(gross)}  •  Δ-neutral`);
  lines.push(`🔗 ${COMPACT_REF_URL}`);
  return lines.join('\n');
}

/**
 * Render a basket CLOSE alert.
 *
 *   ctx = {
 *     traderLabel: 'huf.eth' | null,
 *     traderAddr:  '0x…',
 *     legs:        [{ coin, side, entryPrice|entryPx, exitPrice?, ... }],
 *     pnl:         { realized: number, fees: number, marginUsed?: number },
 *     heldMs?:     number,
 *   }
 */
function renderBasketClose(ctx) {
  const legs = (ctx && ctx.legs) || [];
  const trader = (ctx && ctx.traderLabel) || _shortAddr(ctx && ctx.traderAddr);
  const pnl = (ctx && ctx.pnl) || { realized: 0, fees: 0 };
  const held = ctx && ctx.heldMs;
  const win = Number(pnl.realized) > 0;
  const headEmoji = win ? '✅' : '❌';
  const heldStr = held ? ` • held ${_fmtDuration(held)}` : '';

  // Loss compact: ≤6 lines variant
  if (!win && legs.length >= 2) {
    const sides = legs
      .map((p) => `${_sideLetter(_normSide(p))}:${p.coin}`)
      .join(' / ');
    const pctStr = _pctOnMargin(pnl);
    const lines = [
      `🍐 BASKET CLOSED ${headEmoji} — ${trader}`,
      '━━━━━━━━━━━━━━━━━━━━━━',
      `🔴 PnL ${_fmtPnl(pnl.realized)}${pctStr ? `  (${pctStr})` : ''}${heldStr}`,
      `   ${sides}`,
      `🔗 ${COMPACT_REF_URL}`,
    ];
    return lines.join('\n');
  }

  // Default close (win or single-leg)
  const lines = [`🍐 BASKET CLOSED ${headEmoji} — ${trader}`, '━━━━━━━━━━━━━━━━━━━━━━'];
  for (const p of legs) {
    const side = _normSide(p);
    const letter = _sideLetter(side);
    const entry = p.entryPrice || p.entryPx;
    const exit = p.exitPrice || p.markPrice || entry;
    const movePct = _legMovePct(side, entry, exit);
    lines.push(
      `${_sideEmoji(side)} ${p.coin}  ${letter}  $${_fmtPx(entry)} → $${_fmtPx(exit)}  ${movePct}`
    );
  }
  const pctStr = _pctOnMargin(pnl);
  lines.push(
    `💰 PnL ${_fmtPnl(pnl.realized)}${pctStr ? `  (${pctStr})` : ''}`
  );
  if (held) lines.push(`⏱  Held ${_fmtDuration(held)}`);
  lines.push(`🔗 ${COMPACT_REF_URL}`);
  return lines.join('\n');
}

function _fmtPnl(n) {
  const num = Number(n) || 0;
  const abs = Math.abs(num);
  return num >= 0 ? `+$${abs.toFixed(0)}` : `-$${abs.toFixed(0)}`;
}

function _pctOnMargin(pnl) {
  if (!pnl || !Number.isFinite(pnl.marginUsed) || pnl.marginUsed <= 0) return '';
  const p = (pnl.realized / pnl.marginUsed) * 100;
  if (!Number.isFinite(p)) return '';
  return `${p >= 0 ? '+' : ''}${p.toFixed(1)}% on margin`;
}

function _legMovePct(side, entry, exit) {
  const e = Number(entry);
  const x = Number(exit);
  if (!Number.isFinite(e) || !Number.isFinite(x) || e <= 0) return '';
  const sign = String(side).toUpperCase() === 'SHORT' ? -1 : 1;
  const pct = ((x - e) / e) * 100 * sign;
  if (!Number.isFinite(pct)) return '';
  return `${pct >= 0 ? '+' : ''}${pct.toFixed(1)}%`;
}

function _fmtDuration(ms) {
  if (!Number.isFinite(ms) || ms <= 0) return '0m';
  const totalMin = Math.round(ms / 60000);
  const days = Math.floor(totalMin / 1440);
  const hours = Math.floor((totalMin % 1440) / 60);
  const mins = totalMin % 60;
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${mins}m`;
  return `${mins}m`;
}

/**
 * Hard sanity gate. Returns true iff a CLOSE message is safe to dispatch.
 * Refuses when realized PnL == 0 AND fees == 0 — a real close has at least
 * the close fee, so a fully-zero PnL is the "Manual close $0.00" failure
 * mode of the legacy bot.
 */
function isCloseEmittable(pnl) {
  if (!pnl) return false;
  const realized = Number(pnl.realized);
  const fees = Number(pnl.fees);
  if (!Number.isFinite(realized) && !Number.isFinite(fees)) return false;
  const r = Number.isFinite(realized) ? realized : 0;
  const f = Number.isFinite(fees) ? fees : 0;
  // Per spec §3.2 sanity gate: refuse when both are exactly zero. We
  // allow tiny non-zero PnL (sub-dollar) since real closes can produce
  // those for short-duration trades.
  return !(r === 0 && f === 0);
}

module.exports = {
  renderBasketOpen,
  renderBasketClose,
  isCloseEmittable,
  PEAR_REF_URL,
  EXPAND_THRESHOLD_LEGS,
  EXPAND_THRESHOLD_NOTIONAL,
  _shortAddr,
  _fmtPx,
  _fmtUsdK,
  _fmtPnl,
};
