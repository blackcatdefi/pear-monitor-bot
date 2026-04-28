'use strict';

/**
 * Unified close-alert helpers.
 *
 * Solves four bugs surfaced on 2026-04-28 19:07 UTC during a Pear basket close
 * (6 SHORTs in wallet 0xc7AE...1505):
 *
 *   BUG 1: PnL individual incorrecto. Old code took the LAST fill's `closedPnl`,
 *          so multi-fill TWAP closes (Pear basket close) only reported the last
 *          slice. BLUR was reported as +$47.69 when the true aggregate was
 *          +$406.94. Fix: aggregateClosePnl() sums `closedPnl` across all fills
 *          for the coin since the position was opened.
 *
 *   BUG 2: Mensajes duplicados. The TP/SL handler fired first, and then the
 *          manual-close handler fired again on the same closure. Fix:
 *          shouldSendAlert() de-dupes by (wallet, coin, minute) within a 60s
 *          window, and the new monitor flow runs ONE branch per closed coin.
 *
 *   BUG 3: TP+SL ambos disparados. When a position closed, BOTH the cached TP
 *          and the cached SL trigger orders disappeared in the same poll cycle,
 *          firing two alerts (one TP, one SL). Fix: classifyCloseReason()
 *          returns a SINGLE reason — TAKE_PROFIT, STOP_LOSS, TRAILING_OR_MANUAL,
 *          or MANUAL_CLOSE — by matching the actual exit price to the trigger
 *          price closer than 1%; otherwise falls back to TRAILING_OR_MANUAL.
 *
 *   BUG 4: Falta resumen total al cerrar basket completo. No consolidation.
 *          Fix: trackCloseForBasket() detects 3+ closes for the same wallet
 *          within 5 minutes and, after a 30s debounce, emits a single
 *          consolidated basket-close summary.
 */

// ---------- in-memory caches ----------
const _alertDedup = new Map();          // key: wallet:coin:minute -> ts
const _recentCloses = new Map();        // key: chatId:wallet     -> [closeData...]
const _basketSummaryTimers = new Map(); // key: chatId:wallet     -> timeoutId

// ---------- tunables ----------
const DEDUP_WINDOW_MS = 60 * 1000;
const BASKET_WINDOW_MS = 5 * 60 * 1000;
const BASKET_MIN_COUNT = 3;
const BASKET_DEBOUNCE_MS = 30 * 1000;
const PRICE_MATCH_TOLERANCE = 0.01; // 1%

// ---------- helpers ----------

function _now() {
  return Date.now();
}

function _cleanDedupCache() {
  const cutoff = _now() - DEDUP_WINDOW_MS * 5; // keep 5 min of dedup history
  for (const [k, ts] of _alertDedup.entries()) {
    if (ts < cutoff) _alertDedup.delete(k);
  }
}

/**
 * Returns false if an alert for (wallet, coin) was already sent within the
 * current minute (dedup window). Otherwise returns true and registers it.
 */
function shouldSendAlert(wallet, coin) {
  _cleanDedupCache();
  const minute = Math.floor(_now() / 60000);
  const key = `${String(wallet || '').toLowerCase()}:${String(coin || '').toUpperCase()}:${minute}`;
  if (_alertDedup.has(key)) return false;
  _alertDedup.set(key, _now());
  return true;
}

/**
 * Aggregates `closedPnl` and fee across all fills for the given coin since
 * `sinceMs`. Returns the total PnL, the most-recent fill's exit price, and
 * the cumulative fees.
 */
function aggregateClosePnl(fills, coin, sinceMs) {
  const result = { pnl: 0, exitPrice: null, fees: 0, lastFill: null, fillsUsed: 0 };
  if (!Array.isArray(fills)) return result;
  const sinceFloor = Number.isFinite(sinceMs) ? sinceMs : 0;
  for (const f of fills) {
    if (!f || f.coin !== coin) continue;
    const t = typeof f.time === 'number' ? f.time : 0;
    if (t < sinceFloor) continue;
    const cp = parseFloat(f.closedPnl);
    if (Number.isFinite(cp)) result.pnl += cp;
    const fee = parseFloat(f.fee);
    if (Number.isFinite(fee)) result.fees += fee;
    if (!result.lastFill || t > (result.lastFill.time || 0)) {
      result.lastFill = f;
    }
    result.fillsUsed += 1;
  }
  if (result.lastFill) {
    const px = parseFloat(result.lastFill.px);
    if (Number.isFinite(px) && px > 0) result.exitPrice = px;
  }
  return result;
}

/**
 * Classify a close as TAKE_PROFIT / STOP_LOSS / TRAILING_OR_MANUAL /
 * MANUAL_CLOSE based on which (if any) trigger orders disappeared and how
 * close the exit price was to each trigger price.
 *
 * Critical fix for BUG 3: when BOTH TP and SL triggers disappeared (because
 * BCD switched to a trailing stop and his old TP/SL were still cached), the
 * old loop fired two alerts. Now we resolve to ONE reason.
 */
function classifyCloseReason(disappearedTriggers, exitPrice) {
  if (!Array.isArray(disappearedTriggers) || disappearedTriggers.length === 0) {
    return 'MANUAL_CLOSE';
  }

  const isTP = (t) => t && t.orderType && String(t.orderType).includes('Take Profit');
  const isSL = (t) => t && t.orderType && String(t.orderType).includes('Stop');

  const tpTriggers = disappearedTriggers.filter(isTP);
  const slTriggers = disappearedTriggers.filter(isSL);

  if (tpTriggers.length > 0 && slTriggers.length === 0) return 'TAKE_PROFIT';
  if (slTriggers.length > 0 && tpTriggers.length === 0) return 'STOP_LOSS';

  if (tpTriggers.length > 0 && slTriggers.length > 0) {
    if (Number.isFinite(exitPrice) && exitPrice > 0) {
      let bestType = null;
      let bestDist = Infinity;
      for (const t of disappearedTriggers) {
        const tp = parseFloat(t.triggerPx);
        if (!Number.isFinite(tp) || tp <= 0) continue;
        const dist = Math.abs(exitPrice - tp) / exitPrice;
        if (dist < bestDist) {
          bestDist = dist;
          bestType = isTP(t) ? 'TAKE_PROFIT' : 'STOP_LOSS';
        }
      }
      if (bestType && bestDist < PRICE_MATCH_TOLERANCE) return bestType;
    }
    return 'TRAILING_OR_MANUAL';
  }

  return 'MANUAL_CLOSE';
}

/**
 * Records a close event under (chatId, wallet) and, when 3+ closes have
 * accumulated within 5 minutes, schedules a consolidated basket-close
 * summary after a 30s debounce. The debounce is reset on every new close so
 * the summary lands once all closes have arrived.
 */
function trackCloseForBasket(chatId, wallet, label, closeData, onSummary) {
  const key = `${chatId}:${String(wallet || '').toLowerCase()}`;
  if (!_recentCloses.has(key)) _recentCloses.set(key, []);
  const arr = _recentCloses.get(key);
  arr.push({ ...closeData, timestamp: _now(), label });

  // Drop entries older than the basket window
  const cutoff = _now() - BASKET_WINDOW_MS;
  const recent = arr.filter((c) => c.timestamp >= cutoff);
  _recentCloses.set(key, recent);

  if (recent.length < BASKET_MIN_COUNT) return false;

  if (_basketSummaryTimers.has(key)) {
    clearTimeout(_basketSummaryTimers.get(key));
  }
  const timerId = setTimeout(() => {
    _basketSummaryTimers.delete(key);
    const cur = (_recentCloses.get(key) || []).filter(
      (c) => c.timestamp >= _now() - BASKET_WINDOW_MS
    );
    if (cur.length >= BASKET_MIN_COUNT) {
      Promise.resolve()
        .then(() => onSummary(chatId, wallet, label, cur))
        .catch((err) =>
          console.error('basket summary failed:', err && err.message ? err.message : err)
        );
      _recentCloses.set(key, []);
    }
  }, BASKET_DEBOUNCE_MS);
  if (typeof timerId === 'object' && timerId && typeof timerId.unref === 'function') {
    timerId.unref(); // do not block process exit
  }
  _basketSummaryTimers.set(key, timerId);
  return true;
}

const REASON_META = {
  TAKE_PROFIT: { emoji: '🎯', label: 'TAKE PROFIT hit' },
  STOP_LOSS: { emoji: '🛑', label: 'STOP LOSS triggered' },
  TRAILING_OR_MANUAL: { emoji: '🔄', label: 'Position closed (trailing/manual)' },
  MANUAL_CLOSE: { emoji: '📋', label: 'Position closed' },
};

function _fmtUsd(n) {
  if (!Number.isFinite(n)) return '$0.00';
  const abs = Math.abs(n).toFixed(2);
  return n >= 0 ? `+$${abs}` : `-$${abs}`;
}

function _fmtPrice(n) {
  if (!Number.isFinite(n) || n <= 0) return '';
  if (n >= 100) return n.toFixed(2);
  if (n >= 1) return n.toFixed(4);
  return n.toFixed(6);
}

/**
 * Renders a single close alert. Reason is fixed to one classification by
 * classifyCloseReason() — never both TP and SL.
 */
function formatCloseAlert({ label, oldPos, pnl, exitPrice, reason, dexTag }) {
  const meta = REASON_META[reason] || REASON_META.MANUAL_CLOSE;
  const pnlEmoji = pnl >= 0 ? '🟢' : '🔴';
  const pxStr = _fmtPrice(exitPrice);
  const entryStr =
    oldPos && Number.isFinite(oldPos.entryPrice) && oldPos.entryPrice > 0
      ? _fmtPrice(oldPos.entryPrice)
      : '';

  return [
    `${meta.emoji} *${meta.label}*`,
    ``,
    `📍 Wallet: ${label}`,
    `🪙 ${oldPos.coin}${dexTag || ''} ${oldPos.side || ''}`.trim(),
    `${pnlEmoji} PnL: ${_fmtUsd(pnl)}`,
    entryStr ? `💲 Entry: $${entryStr}` : '',
    pxStr ? `💲 Close: $${pxStr}` : '',
  ]
    .filter(Boolean)
    .join('\n');
}

/**
 * Renders a basket-close summary message: total PnL, breakdown sorted by
 * PnL (best to worst), and the list of closed positions.
 */
function formatBasketSummary(label, closes) {
  const items = Array.isArray(closes) ? closes : [];
  const totalPnl = items.reduce((s, c) => s + (Number.isFinite(c.pnl) ? c.pnl : 0), 0);
  const totalFees = items.reduce((s, c) => s + (Number.isFinite(c.fees) ? c.fees : 0), 0);
  const sorted = [...items].sort((a, b) => (b.pnl || 0) - (a.pnl || 0));
  const symbols = items.map((c) => c.coin).join(', ');
  const pnlEmoji = totalPnl >= 0 ? '🟢' : '🔴';

  const lines = [
    `🐱‍⬛ *BASKET CLOSED* — ${label}`,
    ``,
    `📊 *Summary:*`,
    `• Posiciones cerradas: *${items.length}* (${symbols})`,
    `• ${pnlEmoji} PnL total: *${_fmtUsd(totalPnl)}*`,
  ];
  if (totalFees) lines.push(`• Fees: $${totalFees.toFixed(2)}`);
  lines.push('', '📋 *Breakdown (best → worst):*');
  for (const c of sorted) {
    const e = (c.pnl || 0) >= 0 ? '🟢' : '🔴';
    const side = c.side ? ` ${c.side}` : '';
    lines.push(`  ${e} ${c.coin}${side}: ${_fmtUsd(c.pnl || 0)}`);
  }
  return lines.join('\n');
}

// Test-only utility
function _resetCachesForTests() {
  _alertDedup.clear();
  _recentCloses.clear();
  for (const t of _basketSummaryTimers.values()) {
    try {
      clearTimeout(t);
    } catch (_) {}
  }
  _basketSummaryTimers.clear();
}

module.exports = {
  shouldSendAlert,
  aggregateClosePnl,
  classifyCloseReason,
  trackCloseForBasket,
  formatCloseAlert,
  formatBasketSummary,
  REASON_META,
  // tunables (re-exported in case caller wants to read them)
  DEDUP_WINDOW_MS,
  BASKET_WINDOW_MS,
  BASKET_MIN_COUNT,
  BASKET_DEBOUNCE_MS,
  PRICE_MATCH_TOLERANCE,
  // test-only
  _resetCachesForTests,
};
