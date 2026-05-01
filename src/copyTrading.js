'use strict';

/**
 * R-AUTOCOPY-MENU — Unified copy-trading dispatcher.
 *
 * Coordinates the 3 schedulers and fans out alerts via the wrapped notifier:
 *
 *   • Scheduler A — BCD Wallet poller (60s) — calls onBCDOpen / onBCDClose
 *     → fan-out to all users with BCD_WALLET enabled.
 *   • Scheduler B — Signals channel scraper (30s) — emits "signal" events
 *     → fan-out to all users with BCD_SIGNALS enabled.
 *   • Scheduler C — Custom wallet poller (60s) per unique address
 *     → fan-out only to that address's subscribers.
 *
 * The HL fetch is centralized here so each address is queried once per poll,
 * then fanned out to N users. This module stays thin: parsing/diff lives in
 * helpers, alert formatting in copyAlertBuilder, persistence in store.
 */

const store = require('./copyTradingStore');
const builder = require('./copyAlertBuilder');
const scraper = require('./signalsChannelScraper');
const { fetchHyperliquidPositions } = require('./externalWalletTracker');

const POLL_INTERVAL_SEC = parseInt(
  process.env.COPY_TRADING_POLL_INTERVAL_SEC || '60',
  10
);

let _notify = null;
let _bcdTimer = null;
let _customTimer = null;
let _signalsTimer = null;

const _bcdLast = new Map(); // address-lc -> [{coin,side}]
const _customLast = new Map(); // address-lc -> [{coin,side}]

function attach(notify) {
  _notify = notify;
}

function _normalizePos(arr) {
  return (arr || [])
    .filter((p) => p && p.coin)
    .map((p) => ({
      coin: String(p.coin).toUpperCase(),
      side: (p.side || (p.size < 0 ? 'SHORT' : 'LONG')).toUpperCase(),
      size: Math.abs(Number(p.size) || 0),
      entryPx: Number(p.entryPx || p.entryPrice) || 0,
    }));
}

function _diff(prev, curr) {
  const pSet = new Set((prev || []).map((p) => `${p.coin}:${p.side}`));
  const cSet = new Set((curr || []).map((p) => `${p.coin}:${p.side}`));
  const opens = (curr || []).filter((p) => !pSet.has(`${p.coin}:${p.side}`));
  const closes = (prev || []).filter((p) => !cSet.has(`${p.coin}:${p.side}`));
  return { opens, closes };
}

function _splitSides(positions) {
  const longTokens = positions
    .filter((p) => p.side === 'LONG')
    .map((p) => p.coin);
  const shortTokens = positions
    .filter((p) => p.side === 'SHORT')
    .map((p) => p.coin);
  return { longTokens, shortTokens };
}

async function _fanOut(subscribers, alertSpec) {
  if (typeof _notify !== 'function') {
    console.warn('[copyTrading] notify not attached — dropping alert');
    return 0;
  }
  let count = 0;
  for (const sub of subscribers) {
    const userId = parseInt(sub.userId, 10);
    if (!Number.isFinite(userId)) continue;
    const cfg = sub.config || sub;
    const { text, keyboard } = builder.buildAlert({
      ...alertSpec,
      userId,
      capital: cfg.capital_usdc,
      mode: cfg.mode,
      sl_pct: cfg.sl_pct,
      trailing_pct: cfg.trailing_pct,
      trailing_activation_pct: cfg.trailing_activation_pct,
      sourceLabel:
        alertSpec.source === 'CUSTOM_WALLET'
          ? cfg.label || alertSpec.sourceLabel || 'Custom Wallet'
          : alertSpec.sourceLabel,
    });
    try {
      await _notify(userId, text, {
        parse_mode: 'Markdown',
        reply_markup: keyboard,
        disable_web_page_preview: true,
      });
      count += 1;
    } catch (e) {
      console.error(
        '[copyTrading] notify failed for',
        userId,
        e && e.message ? e.message : e
      );
    }
  }
  return count;
}

// --- Scheduler A: BCD Wallet poller -------------------------------------

async function pollBcdWalletOnce() {
  const addr = store.BCD_WALLET;
  let positions;
  try {
    positions = _normalizePos(await fetchHyperliquidPositions(addr));
  } catch (e) {
    console.error(
      '[copyTrading] BCD wallet fetch failed:',
      e && e.message ? e.message : e
    );
    return { opens: 0, closes: 0 };
  }
  const prev = _bcdLast.get(addr);
  if (prev === undefined) {
    // first cycle — baseline only, no spurious alerts
    _bcdLast.set(addr, positions);
    return { opens: 0, closes: 0 };
  }
  const { opens, closes } = _diff(prev, positions);
  _bcdLast.set(addr, positions);

  let openSent = 0;
  let closeSent = 0;
  if (opens.length > 0) {
    const subs = store.listEnabledByType(store.TYPE_BCD_WALLET);
    if (subs.length > 0) {
      const sides = _splitSides(opens);
      openSent = await _fanOut(subs, {
        source: 'BCD_WALLET',
        sourceLabel: 'BCD Wallet',
        positions: opens,
        longTokens: sides.longTokens,
        shortTokens: sides.shortTokens,
        event: 'OPEN',
      });
    }
  }
  if (closes.length > 0) {
    const subs = store.listEnabledByType(store.TYPE_BCD_WALLET);
    if (subs.length > 0) {
      closeSent = await _fanOut(subs, {
        source: 'BCD_WALLET',
        sourceLabel: 'BCD Wallet',
        positions: closes,
        event: 'CLOSE',
      });
    }
  }
  return { opens: openSent, closes: closeSent };
}

// --- Scheduler B: Signals scraper handler --------------------------------

async function dispatchSignalToSubscribers(signal) {
  const subs = store.listEnabledByType(store.TYPE_BCD_SIGNALS);
  if (subs.length === 0) return 0;
  const positions = [];
  for (const t of signal.longTokens || []) positions.push({ coin: t, side: 'LONG' });
  for (const t of signal.shortTokens || []) positions.push({ coin: t, side: 'SHORT' });
  return _fanOut(subs, {
    source: 'BCD_SIGNALS',
    sourceLabel: 'BCD Signals',
    positions,
    pearUrl: signal.pearUrl,
    longTokens: signal.longTokens,
    shortTokens: signal.shortTokens,
    event: 'OPEN',
  });
}

// --- Scheduler C: Custom wallet poller -----------------------------------

async function pollCustomWalletsOnce() {
  const groups = store.listAllCustomAddresses();
  let totalOpens = 0;
  let totalCloses = 0;
  for (const g of groups) {
    let positions;
    try {
      positions = _normalizePos(await fetchHyperliquidPositions(g.address));
    } catch (e) {
      console.error(
        '[copyTrading] custom fetch failed for',
        g.address,
        e && e.message ? e.message : e
      );
      continue;
    }
    const prev = _customLast.get(g.address);
    if (prev === undefined) {
      _customLast.set(g.address, positions);
      continue;
    }
    const { opens, closes } = _diff(prev, positions);
    _customLast.set(g.address, positions);
    if (opens.length > 0) {
      const sides = _splitSides(opens);
      const sub = g.subscribers.map((s) => ({ userId: s.userId, config: s }));
      const sent = await _fanOut(sub, {
        source: 'CUSTOM_WALLET',
        sourceLabel: g.subscribers[0] && g.subscribers[0].label
          ? g.subscribers[0].label
          : 'Custom Wallet',
        positions: opens,
        longTokens: sides.longTokens,
        shortTokens: sides.shortTokens,
        event: 'OPEN',
      });
      totalOpens += sent;
    }
    if (closes.length > 0) {
      const sub = g.subscribers.map((s) => ({ userId: s.userId, config: s }));
      const sent = await _fanOut(sub, {
        source: 'CUSTOM_WALLET',
        sourceLabel: g.subscribers[0] && g.subscribers[0].label
          ? g.subscribers[0].label
          : 'Custom Wallet',
        positions: closes,
        event: 'CLOSE',
      });
      totalCloses += sent;
    }
  }
  return { opens: totalOpens, closes: totalCloses };
}

// --- Bootstrap -----------------------------------------------------------

function startSchedulers() {
  if (typeof _notify !== 'function') {
    console.warn('[copyTrading] startSchedulers called before attach()');
    return null;
  }
  // BCD wallet poller
  if (!_bcdTimer) {
    setTimeout(() => { pollBcdWalletOnce().catch(() => {}); }, 8_000);
    _bcdTimer = setInterval(() => {
      pollBcdWalletOnce().catch(() => {});
    }, Math.max(15, POLL_INTERVAL_SEC) * 1000);
    if (_bcdTimer && typeof _bcdTimer.unref === 'function') _bcdTimer.unref();
  }
  // Custom wallet poller
  if (!_customTimer) {
    setTimeout(() => { pollCustomWalletsOnce().catch(() => {}); }, 12_000);
    _customTimer = setInterval(() => {
      pollCustomWalletsOnce().catch(() => {});
    }, Math.max(15, POLL_INTERVAL_SEC) * 1000);
    if (_customTimer && typeof _customTimer.unref === 'function') _customTimer.unref();
  }
  // Signals scraper
  if (!_signalsTimer) {
    _signalsTimer = scraper.startSchedule({
      onSignal: dispatchSignalToSubscribers,
    });
  }
  console.log(
    `[copyTrading] schedulers started (poll ${POLL_INTERVAL_SEC}s, scraper ${scraper.SIGNALS_SCRAPER_INTERVAL_SEC}s)`
  );
  return { bcd: _bcdTimer, custom: _customTimer, signals: _signalsTimer };
}

function stopSchedulers() {
  if (_bcdTimer) clearInterval(_bcdTimer);
  if (_customTimer) clearInterval(_customTimer);
  scraper.stopSchedule();
  _bcdTimer = null;
  _customTimer = null;
  _signalsTimer = null;
}

module.exports = {
  attach,
  startSchedulers,
  stopSchedulers,
  pollBcdWalletOnce,
  pollCustomWalletsOnce,
  dispatchSignalToSubscribers,
  _diff,
  _splitSides,
  _normalizePos,
  _fanOut,
  _resetForTests() {
    _bcdLast.clear();
    _customLast.clear();
  },
};
