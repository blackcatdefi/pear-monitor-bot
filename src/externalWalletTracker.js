'use strict';

/**
 * R(v3) + R-BASKET — External Wallet Tracker.
 *
 * Polls a configurable list of Hyperliquid wallets (whales, top traders,
 * benchmark accounts) and emits a Telegram alert whenever any of them
 * OPENs or CLOSEs a basket. Operators use these alerts as intel signal —
 * to track whales/top traders and inform their own trades.
 *
 * R-BASKET (3 may 2026): all events flow through src/basketEngine.js so the
 * per-leg loop ("one Telegram message per assetPosition") is replaced with
 * one OPEN + one CLOSE per basket lifecycle, keyed by the sorted-leg
 * signature. Eliminates the apr-30 incident where an external whale opened
 * a 12-leg basket and the bot fired 12 OPENs in 60 seconds.
 *
 * Configuration:
 *   EXTERNAL_WALLETS_ENABLED              — kill-switch (default true)
 *   EXTERNAL_WALLETS_JSON                 — JSON array; entries:
 *                                            { address, label, chain? }
 *   EXTERNAL_WALLETS_POLL_INTERVAL_SECONDS — poll cadence (default 60)
 */

const { appendFooter } = (() => {
  try {
    return require('./branding');
  } catch (_) {
    return { appendFooter: (m) => m };
  }
})();
// R-BASKET — basket lifecycle engine + compact templates.
const { BasketEngine } = require('./basketEngine');
const formattersV2 = require('./messageFormattersV2');

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
// R-BASKET — single shared engine for the external whale-tracker schedule.
// Persists to a dedicated file so external-whale lifecycle never collides
// with the public per-user tracker engine or the BCD fund engine.
let _engine = null;
function _getEngine() {
  if (_engine) return _engine;
  const path = require('path');
  _engine = new BasketEngine({
    dbPath:
      process.env.EXTERNAL_BASKET_ENGINE_DB_PATH ||
      path.join(
        process.env.RAILWAY_VOLUME_MOUNT_PATH || '/app/data',
        'basket_engine_external.json'
      ),
  });
  return _engine;
}

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
    // R-BASKET: timestamp footer dropped (Telegram already shows the
    // delivery time on every message). branding footer kept since it
    // distinguishes BCD's fund alerts from generic public broadcasts.
    const decorated = appendFooter(message, false);
    await _notify(_primaryChatId, decorated, { parse_mode: 'Markdown' });
  } catch (err) {
    console.error(
      '[externalWalletTracker] notify failed:',
      err && err.message ? err.message : err
    );
  }
}

/**
 * One poll cycle. Iterates each tracked wallet, fetches positions, hands
 * the snapshot to the basket engine, and emits at most ONE OPEN and ONE
 * CLOSE message per basket lifecycle. Errors per-wallet are isolated so
 * one flaky address can't kill the schedule.
 */
async function pollExternalWallets() {
  if (!isEnabled()) return;
  if (_trackedExternalWallets.length === 0) return;

  const engine = _getEngine();

  for (const config of _trackedExternalWallets) {
    const lc = String(config.address || '').toLowerCase();
    if (!lc) continue;

    try {
      const curr = await fetchHyperliquidPositions(config.address);
      const baseline = !_lastSeenPositions.has(lc);

      const events = engine.processSnapshot({
        walletKey: config.address,
        positions: curr,
        baseline,
      });

      for (const ev of events) {
        if (ev.type === 'BASKET_OPEN') {
          await _send(formatExternalBasketOpenAlert(config, ev.legs));
        } else if (ev.type === 'BASKET_CLOSE') {
          const msg = formatExternalBasketCloseAlert(config, ev.legs, ev.openedAt);
          if (msg) await _send(msg);
        }
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
 * Render a basket OPEN alert via the V2 compact template, then prepend the
 * intel banner the legacy operators rely on ("EXTERNAL WALLET — possible
 * market signal"). One Telegram message per basket, regardless of leg count.
 */
function formatExternalBasketOpenAlert(config, legs) {
  const body = formattersV2.renderBasketOpen({
    traderLabel: config.label || null,
    traderAddr: config.address,
    legs,
  });
  return `🐋 EXTERNAL WALLET — possible market signal\n${body}`;
}

function formatExternalBasketCloseAlert(config, legs, openedAt) {
  let realized = 0;
  let gross = 0;
  for (const p of legs || []) {
    const u = Number(p.unrealizedPnl);
    if (Number.isFinite(u)) realized += u;
    const sz = Math.abs(Number(p.size) || 0);
    const px = Number(p.entryPrice || p.entryPx) || 0;
    gross += sz * px;
  }
  const fees = gross * 0.001;
  const pnl = { realized, fees };
  if (!formattersV2.isCloseEmittable(pnl)) {
    return null;
  }
  const heldMs =
    openedAt && Number.isFinite(openedAt)
      ? Math.max(0, Date.now() - Number(openedAt))
      : null;
  const body = formattersV2.renderBasketClose({
    traderLabel: config.label || null,
    traderAddr: config.address,
    legs,
    pnl,
    heldMs,
  });
  return `🐋 EXTERNAL WALLET — possible exit signal\n${body}`;
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
  _engine = null;
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
