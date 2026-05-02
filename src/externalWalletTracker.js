'use strict';

/**
 * R(v3) — External Wallet Tracker.
 *
 * Polls a configurable list of Hyperliquid wallets (whales, top traders,
 * benchmark accounts) and emits a Telegram alert whenever any of them
 * OPENs or CLOSEs a position. Operators use these alerts as intel signal —
 * to track whales/top traders and inform their own trades (e.g., "top trader
 * just opened BTC long").
 *
 * Configuration:
 *   EXTERNAL_WALLETS_ENABLED              — kill-switch (default true)
 *   EXTERNAL_WALLETS_JSON                 — JSON array; entries:
 *                                            { address, label, chain? }
 *   EXTERNAL_WALLETS_POLL_INTERVAL_SECONDS — poll cadence (default 60)
 *
 * State is in-memory only. On restart we re-baseline (no spurious
 * "closed" alerts because we have no prior snapshot).
 */

const { withTimestamp } = require('./timestampHelper');
const { appendFooter } = (() => {
  try {
    return require('./branding');
  } catch (_) {
    return { appendFooter: (m) => m };
  }
})();

const HL_INFO_URL =
  process.env.HYPERLIQUID_API_URL || 'https://api.hyperliquid.xyz';

function isEnabled() {
  return (
    (process.env.EXTERNAL_WALLETS_ENABLED || 'true').toLowerCase() !== 'false'
  );
}

function getPollIntervalSeconds() {
  const n = parseInt(process.env.EXTERNAL_WALLETS_POLL_INTERVAL_SECONDS || '60', 10);
  return Number.isFinite(n) && n >= 15 ? n : 60;
}

let _trackedExternalWallets = [];
const _lastSeenPositions = new Map(); // key: lc(address) -> array of { coin, side, ... }
let _pollTimer = null;
let _notify = null; // (chatId, message, opts) => Promise
let _primaryChatId = null;

function loadExternalWalletsFromEnv() {
  try {
    const json = process.env.EXTERNAL_WALLETS_JSON || '[]';
    const parsed = JSON.parse(json);
    if (!Array.isArray(parsed)) {
      console.warn('[externalWalletTracker] EXTERNAL_WALLETS_JSON is not an array; ignoring');
      _trackedExternalWallets = [];
      return _trackedExternalWallets;
    }
    _trackedExternalWallets = parsed.filter(
      (e) => e && typeof e.address === 'string'
    );
    console.log(
      `[externalWalletTracker] loaded ${_trackedExternalWallets.length} external wallet(s)`
    );
    return _trackedExternalWallets;
  } catch (err) {
    console.error(
      '[externalWalletTracker] failed to parse EXTERNAL_WALLETS_JSON:',
      err && err.message ? err.message : err
    );
    _trackedExternalWallets = [];
    return _trackedExternalWallets;
  }
}

function getTrackedWallets() {
  return [..._trackedExternalWallets];
}

async function fetchHyperliquidPositions(address) {
  // Hyperliquid public /info clearinghouseState — same shape used elsewhere
  // in this repo; we keep this self-contained so the tracker has no
  // direct dependency on hyperliquidApi.js (avoids axios entanglement).
  const fetchFn = typeof fetch === 'function' ? fetch : null;
  if (!fetchFn) {
    throw new Error('global fetch unavailable; need Node 18+');
  }
  const res = await fetchFn(`${HL_INFO_URL}/info`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: 'clearinghouseState', user: address }),
  });
  if (!res.ok) {
    throw new Error(`HL API error: ${res.status}`);
  }
  const data = await res.json();
  const raw = (data && data.assetPositions) || [];
  const out = [];
  for (const p of raw) {
    const pos = p && p.position;
    if (!pos || !pos.coin) continue;
    const szi = parseFloat(pos.szi);
    if (!Number.isFinite(szi) || Math.abs(szi) === 0) continue;
    const entry = parseFloat(pos.entryPx);
    out.push({
      coin: pos.coin,
      side: szi > 0 ? 'LONG' : 'SHORT',
      size: Math.abs(szi),
      entryPx: Number.isFinite(entry) ? entry : 0,
      notional: Math.abs(szi) * (Number.isFinite(entry) ? entry : 0),
      unrealizedPnl: parseFloat(pos.unrealizedPnl || '0') || 0,
    });
  }
  return out;
}

function _shortAddr(addr) {
  const a = String(addr || '');
  if (a.length < 12) return a;
  return `${a.slice(0, 6)}...${a.slice(-4)}`;
}

function _formatNumber(n) {
  if (!Number.isFinite(n)) return '0';
  if (Math.abs(n) >= 1000) return Math.round(n).toLocaleString();
  if (Math.abs(n) >= 1) return n.toFixed(2);
  return n.toFixed(4);
}

function _formatPx(n) {
  if (!Number.isFinite(n) || n <= 0) return '?';
  if (n >= 100) return n.toFixed(2);
  if (n >= 1) return n.toFixed(4);
  return n.toFixed(6);
}

function formatExternalOpenAlert(config, position) {
  return [
    `🐋 *EXTERNAL WALLET — NEW POSITION OPENED*`,
    ``,
    `📍 ${config.label || 'External'} (${_shortAddr(config.address)})`,
    ``,
    `🪙 ${position.coin} ${position.side}`,
    `💲 Entry: $${_formatPx(position.entryPx)}`,
    `📦 Size: ${_formatNumber(position.size)}`,
    `💰 Notional: $${_formatNumber(position.notional)}`,
    ``,
    `💡 Intel: possible market signal.`,
  ].join('\n');
}

function formatExternalCloseAlert(config, position) {
  const pnlEmoji = position.unrealizedPnl >= 0 ? '🟢' : '🔴';
  const pnlAbs = Math.abs(position.unrealizedPnl).toFixed(2);
  const pnlStr =
    position.unrealizedPnl >= 0 ? `+$${pnlAbs}` : `-$${pnlAbs}`;
  return [
    `🐋 *EXTERNAL WALLET — POSITION CLOSED*`,
    ``,
    `📍 ${config.label || 'External'} (${_shortAddr(config.address)})`,
    ``,
    `🪙 ${position.coin} ${position.side}`,
    `${pnlEmoji} Last PnL snapshot: ${pnlStr}`,
    `💲 Entry was: $${_formatPx(position.entryPx)}`,
    `📦 Size closed: ${_formatNumber(position.size)}`,
    ``,
    `💡 Intel: possible exit signal — review your position.`,
  ].join('\n');
}

function _diffPositions(prev, curr) {
  const prevSet = new Set(prev.map((p) => `${p.coin}:${p.side}`));
  const currSet = new Set(curr.map((p) => `${p.coin}:${p.side}`));
  const opens = curr.filter((p) => !prevSet.has(`${p.coin}:${p.side}`));
  const closes = prev.filter((p) => !currSet.has(`${p.coin}:${p.side}`));
  return { opens, closes };
}

async function _send(message) {
  if (typeof _notify !== 'function' || !_primaryChatId) return;
  try {
    const decorated = withTimestamp(appendFooter(message, false), 'bottom');
    await _notify(_primaryChatId, decorated, { parse_mode: 'Markdown' });
  } catch (err) {
    console.error(
      '[externalWalletTracker] notify failed:',
      err && err.message ? err.message : err
    );
  }
}

/**
 * One poll cycle. Iterates each tracked wallet, fetches positions,
 * diffs against prior snapshot, emits alerts. Errors per-wallet are
 * isolated so one flaky address can't kill the schedule.
 */
async function pollExternalWallets() {
  if (!isEnabled()) return;
  if (_trackedExternalWallets.length === 0) return;

  for (const config of _trackedExternalWallets) {
    const lc = String(config.address || '').toLowerCase();
    if (!lc) continue;

    try {
      const curr = await fetchHyperliquidPositions(config.address);
      const prev = _lastSeenPositions.get(lc);

      // First sighting — baseline only, do not alert.
      if (!prev) {
        _lastSeenPositions.set(lc, curr);
        continue;
      }

      const { opens, closes } = _diffPositions(prev, curr);

      for (const open of opens) {
        await _send(formatExternalOpenAlert(config, open));
      }
      for (const close of closes) {
        // For CLOSE alerts, use the snapshot's last-seen PnL since we don't
        // have a closing fill price for an external wallet without an extra
        // user-fills query. unrealizedPnl is the closest signal we have.
        await _send(formatExternalCloseAlert(config, close));
      }

      _lastSeenPositions.set(lc, curr);
    } catch (err) {
      console.error(
        `[externalWalletTracker] poll failed for ${config.label || config.address}:`,
        err && err.message ? err.message : err
      );
    }
  }
}

/**
 * Start the polling timer. Idempotent. Returns the interval handle.
 */
function startSchedule({ notify, primaryChatId }) {
  loadExternalWalletsFromEnv();
  if (!isEnabled()) {
    console.log('[externalWalletTracker] disabled via env');
    return null;
  }
  if (!notify || !primaryChatId) {
    console.log(
      '[externalWalletTracker] notify or primaryChatId missing → skipping schedule'
    );
    return null;
  }
  if (_trackedExternalWallets.length === 0) {
    console.log(
      '[externalWalletTracker] no wallets configured (EXTERNAL_WALLETS_JSON empty); scheduler idle'
    );
    // Still start a dormant timer so a hot env-var reload (future) can pick up.
  }
  _notify = notify;
  _primaryChatId = primaryChatId;
  const intervalSec = getPollIntervalSeconds();
  if (_pollTimer) clearInterval(_pollTimer);
  _pollTimer = setInterval(() => {
    pollExternalWallets().catch((e) =>
      console.error(
        '[externalWalletTracker] schedule cycle failed:',
        e && e.message ? e.message : e
      )
    );
  }, intervalSec * 1000);
  if (typeof _pollTimer.unref === 'function') _pollTimer.unref();
  console.log(
    `[externalWalletTracker] schedule started, every ${intervalSec}s, ${_trackedExternalWallets.length} wallet(s)`
  );
  return _pollTimer;
}

function stopSchedule() {
  if (_pollTimer) {
    clearInterval(_pollTimer);
    _pollTimer = null;
  }
}

function _resetForTests() {
  _trackedExternalWallets = [];
  _lastSeenPositions.clear();
  _notify = null;
  _primaryChatId = null;
  if (_pollTimer) {
    clearInterval(_pollTimer);
    _pollTimer = null;
  }
}

// init on module load
loadExternalWalletsFromEnv();

module.exports = {
  isEnabled,
  loadExternalWalletsFromEnv,
  getTrackedWallets,
  pollExternalWallets,
  startSchedule,
  stopSchedule,
  formatExternalOpenAlert,
  formatExternalCloseAlert,
  fetchHyperliquidPositions,
  _diffPositions,
  _resetForTests,
};
