'use strict';

/**
 * R-PUBLIC — Scheduler for the per-user wallet tracker.
 *
 * Polls each unique address from walletTracker.js, diffs against last
 * snapshot, and fans out OPEN / CLOSE alerts to every subscribing user
 * (so multiple users tracking the same whale share one HL fetch).
 *
 * Each alert is rendered with the subscriber's local timezone (via
 * timezoneManager.formatLocalTime) and ships with a Pear "Copy trade"
 * inline-keyboard button (via pearUrlBuilder).
 */

const wt = require('./walletTracker');
const tzMgr = require('./timezoneManager');
const pearUrl = require('./pearUrlBuilder');
const alertButtons = require('./alertButtons');
const { fetchHyperliquidPositions } = require('./externalWalletTracker');

const POLL_INTERVAL_SEC = parseInt(
  process.env.TRACK_POLL_INTERVAL_SEC || '60', 10
);

function isEnabled() {
  return (
    (process.env.TRACK_ENABLED || 'true').toLowerCase() !== 'false'
  );
}

let _timer = null;
let _notify = null;

function _shortAddr(a) {
  if (!a) return '?';
  const s = String(a);
  if (s.length < 12) return s;
  return `${s.slice(0, 6)}...${s.slice(-4)}`;
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

function _diffPositions(prev, curr) {
  const pSet = new Set((prev || []).map((p) => `${p.coin}:${p.side}`));
  const cSet = new Set((curr || []).map((p) => `${p.coin}:${p.side}`));
  const opens = (curr || []).filter((p) => !pSet.has(`${p.coin}:${p.side}`));
  const closes = (prev || []).filter((p) => !cSet.has(`${p.coin}:${p.side}`));
  return { opens, closes };
}

function _renderOpenForUser(userId, label, address, opens) {
  const isBasket = opens.length >= 3;
  const heading = isBasket
    ? '🚀 *NUEVA BASKET ABIERTA*'
    : '🐋 *NUEVA POSICIÓN ABIERTA*';
  const traderLabel = label || _shortAddr(address);
  const lines = [
    heading,
    '',
    `👤 Trader: ${traderLabel} (\`${_shortAddr(address)}\`)`,
    '',
    `📊 Composición (${opens.length}):`,
  ];
  let totalNotional = 0;
  for (const p of opens) {
    const side = p.side || 'SHORT';
    const px = p.entryPx || p.entryPrice || 0;
    const sz = Math.abs(Number(p.size) || 0);
    totalNotional += sz * px;
    lines.push(`  • ${p.coin} ${side} @ $${_fmtPx(px)}`);
  }
  lines.push('');
  lines.push(`💰 Notional: ${_fmtUsd(totalNotional)}`);
  if (opens[0] && opens[0].leverage) {
    lines.push(`⚡ Leverage: ${opens[0].leverage}x`);
  }
  lines.push('');
  // R-START — copy-trade invitation text precedes the hero button row.
  lines.push(alertButtons.getCopyCtaText());
  lines.push('');
  lines.push(`🕐 ${tzMgr.formatLocalTime(userId)}`);
  return lines.join('\n');
}

function _renderCloseForUser(userId, label, address, closes) {
  const lines = [
    '✅ *POSICIÓN CERRADA EN WALLET TRACKEADA*',
    '',
    `Wallet: ${label || _shortAddr(address)} (\`${_shortAddr(address)}\`)`,
    '',
    `Cerradas (${closes.length}):`,
  ];
  for (const p of closes) {
    const side = p.side || 'SHORT';
    const px = p.entryPx || p.entryPrice || 0;
    lines.push(`  • ${p.coin} ${side} @ entry $${_fmtPx(px)}`);
  }
  lines.push('');
  lines.push(`🕐 ${tzMgr.formatLocalTime(userId)}`);
  return lines.join('\n');
}

async function _fanOut(subscribers, msgFn, opts) {
  for (const sub of subscribers) {
    const userId = parseInt(sub.userId, 10);
    if (!userId) continue;
    try {
      const personalized = msgFn(userId, sub.label);
      await _notify(userId, personalized, opts || { parse_mode: 'Markdown' });
    } catch (err) {
      console.error(
        '[walletTrackerScheduler] notify failed for',
        sub.userId, err && err.message ? err.message : err
      );
    }
  }
}

async function pollOnce() {
  if (!isEnabled()) return;
  if (typeof _notify !== 'function') return;
  const all = wt.getAllUniqueAddresses();
  if (all.length === 0) return;

  for (const entry of all) {
    const address = entry.address;
    const subs = entry.subscribers || [];
    let curr;
    try {
      curr = await fetchHyperliquidPositions(address);
    } catch (err) {
      console.error(
        '[walletTrackerScheduler] HL fetch failed for', address,
        err && err.message ? err.message : err
      );
      continue;
    }
    const prev = wt.getLastSnapshot(address);
    if (prev === null) {
      // First sighting → baseline only, no alert
      wt.setLastSnapshot(address, curr);
      continue;
    }
    const { opens, closes } = _diffPositions(prev, curr);
    if (opens.length > 0) {
      // R-START — hero CTA layout (Pear copy button row 1, mute button row 2).
      const keyboard = alertButtons.buildAlertKeyboard(opens, 'open', {
        wallet: address,
      });
      await _fanOut(
        subs,
        (uid, lbl) => _renderOpenForUser(uid, lbl, address, opens),
        keyboard
          ? { parse_mode: 'Markdown', reply_markup: keyboard }
          : { parse_mode: 'Markdown' }
      );
    }
    if (closes.length > 0) {
      await _fanOut(
        subs,
        (uid, lbl) => _renderCloseForUser(uid, lbl, address, closes),
        { parse_mode: 'Markdown' }
      );
    }
    wt.setLastSnapshot(address, curr);
  }
}

function startSchedule({ notify }) {
  if (!isEnabled()) {
    console.log('[walletTrackerScheduler] disabled via TRACK_ENABLED');
    return null;
  }
  _notify = notify;
  if (_timer) clearInterval(_timer);
  _timer = setInterval(() => {
    pollOnce().catch((e) =>
      console.error(
        '[walletTrackerScheduler] poll cycle failed:',
        e && e.message ? e.message : e
      )
    );
  }, POLL_INTERVAL_SEC * 1000);
  if (typeof _timer.unref === 'function') _timer.unref();
  console.log(
    `[walletTrackerScheduler] started, every ${POLL_INTERVAL_SEC}s`
  );
  return _timer;
}

function stopSchedule() {
  if (_timer) {
    clearInterval(_timer);
    _timer = null;
  }
}

module.exports = {
  isEnabled,
  pollOnce,
  startSchedule,
  stopSchedule,
  POLL_INTERVAL_SEC,
  _diffPositions,
  _renderOpenForUser,
  _renderCloseForUser,
};
