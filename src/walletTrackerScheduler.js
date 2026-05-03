'use strict';

/**
 * R-PUBLIC + R-BASKET — Scheduler for the per-user wallet tracker.
 *
 * R-BASKET (3 may 2026) — Wired through src/basketEngine.js so each tracked
 * wallet emits AT MOST one OPEN and one CLOSE message per basket lifecycle.
 * Replaces the legacy per-leg loop that produced ~200 messages/day per user
 * for a single 10-leg Pear basket. See src/basketEngine.js header for the
 * root-cause and design notes.
 *
 * Per-user behaviour preserved from R-PUBLIC:
 *   • Per-subscriber timezone in the rendered footer (timezoneManager).
 *   • Per-subscriber inline keyboard with anonymized utm_id (alertButtons).
 *   • Multiple users tracking the same whale share ONE Hyperliquid fetch.
 */

const wt = require('./walletTracker');
// tzMgr no longer required here — R-BASKET dropped the per-message
// timestamp footer in favour of Telegram's native delivery timestamp.
// timezoneManager is still imported by /timezone (hidden command) and by
// schedulers that legitimately need to format absolute times.
const alertButtons = require('./alertButtons');
const { fetchHyperliquidPositions } = require('./externalWalletTracker');
const { BasketEngine } = require('./basketEngine');
const formattersV2 = require('./messageFormattersV2');

const POLL_INTERVAL_SEC = parseInt(
  process.env.TRACK_POLL_INTERVAL_SEC || '60', 10
);

function isEnabled() {
  return (process.env.TRACK_ENABLED || 'true').toLowerCase() !== 'false';
}

let _timer = null;
let _notify = null;
let _engine = null; // lazy — first poll constructs it

function _getEngine() {
  if (!_engine) {
    _engine = new BasketEngine({
      // separate file from the BCD-internal monitor's basket engine so
      // public-bot lifecycle state can't accidentally collide with fund
      // wallets. Both live on the same Railway volume.
      dbPath:
        process.env.PUBLIC_BASKET_ENGINE_DB_PATH ||
        require('path').join(
          process.env.RAILWAY_VOLUME_MOUNT_PATH || '/app/data',
          'basket_engine_public.json'
        ),
    });
  }
  return _engine;
}

function _shortAddr(a) {
  if (!a) return '?';
  const s = String(a);
  if (s.length < 12) return s;
  return `${s.slice(0, 6)}...${s.slice(-4)}`;
}

function _renderOpenMessage(label, address, legs) {
  return formattersV2.renderBasketOpen({
    traderLabel: label || null,
    traderAddr: address,
    legs,
  });
}

function _renderCloseMessage(label, address, legs, openedAt) {
  // Phase-1 PnL estimator: sum the unrealizedPnl carried on each leg in
  // the last snapshot the engine saw before the basket vanished. We don't
  // have authoritative fills here without an extra call; this is the
  // closest-to-exit PnL available from clearinghouseState alone.
  let realized = 0;
  let gross = 0;
  for (const p of legs || []) {
    const u = Number(p.unrealizedPnl);
    if (Number.isFinite(u)) realized += u;
    const sz = Math.abs(Number(p.size) || 0);
    const px = Number(p.entryPrice || p.entryPx) || 0;
    gross += sz * px;
  }
  // Conservative fee estimate: Hyperliquid taker fee ≈ 0.05% per side,
  // so a round-trip on the gross is ≈ 0.10%. Used only by the sanity
  // gate (and informally rendered as part of the message via the PnL).
  const fees = gross * 0.001;
  const pnl = { realized, fees };
  if (!formattersV2.isCloseEmittable(pnl)) {
    return null; // sanity gate refuses to emit
  }
  const heldMs =
    openedAt && Number.isFinite(openedAt)
      ? Math.max(0, Date.now() - Number(openedAt))
      : null;
  return formattersV2.renderBasketClose({
    traderLabel: label || null,
    traderAddr: address,
    legs,
    pnl,
    heldMs,
  });
}

async function _fanOutOpen(subs, address, legs) {
  for (const sub of subs) {
    const userId = parseInt(sub.userId, 10);
    if (!userId) continue;
    try {
      const message = _renderOpenMessage(sub.label, address, legs);
      const keyboard = alertButtons.buildAlertKeyboard(legs, 'open', {
        wallet: address,
        userId,
        source: 'tg-track',
      });
      const sendOpts = keyboard
        ? { parse_mode: 'Markdown', reply_markup: keyboard }
        : { parse_mode: 'Markdown' };
      await _notify(userId, message, sendOpts);
    } catch (err) {
      console.error(
        '[walletTrackerScheduler] notify (open) failed for',
        sub.userId,
        err && err.message ? err.message : err
      );
    }
  }
}

async function _fanOutClose(subs, address, legs, openedAt) {
  for (const sub of subs) {
    const userId = parseInt(sub.userId, 10);
    if (!userId) continue;
    try {
      const message = _renderCloseMessage(sub.label, address, legs, openedAt);
      if (!message) continue; // sanity gate refused
      await _notify(userId, message, { parse_mode: 'Markdown' });
    } catch (err) {
      console.error(
        '[walletTrackerScheduler] notify (close) failed for',
        sub.userId,
        err && err.message ? err.message : err
      );
    }
  }
}

async function pollOnce() {
  if (!isEnabled()) return;
  if (typeof _notify !== 'function') return;
  const all = wt.getAllUniqueAddresses();
  if (all.length === 0) return;

  const engine = _getEngine();

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
    const baseline = prev === null;

    const events = engine.processSnapshot({
      walletKey: address,
      positions: curr,
      baseline,
    });

    for (const ev of events) {
      if (ev.type === 'BASKET_OPEN') {
        await _fanOutOpen(subs, address, ev.legs);
      } else if (ev.type === 'BASKET_CLOSE') {
        await _fanOutClose(subs, address, ev.legs, ev.openedAt);
      }
    }

    // Persist the raw snapshot so we know "we've seen this wallet at
    // least once" — basketEngine needs no more than that since it holds
    // its own structured state.
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
    `[walletTrackerScheduler] started, every ${POLL_INTERVAL_SEC}s (R-BASKET engine)`
  );
  return _timer;
}

function stopSchedule() {
  if (_timer) {
    clearInterval(_timer);
    _timer = null;
  }
}

function _resetEngineForTests() {
  _engine = null;
}

module.exports = {
  isEnabled,
  pollOnce,
  startSchedule,
  stopSchedule,
  POLL_INTERVAL_SEC,
  _renderOpenMessage,
  _renderCloseMessage,
  _resetEngineForTests,
};
