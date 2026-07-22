'use strict';

/**
 * R-PUBLIC-FUNDS — Scheduled scanner for the opt-in funds-available alert.
 *
 * Design constraints (public bot, unbounded user count, $0 budget):
 *   • interval: FUNDS_ALERT_POLL_MIN (default 20, clamped 15–30 min)
 *   • unique-wallet de-dup: N users watching the same wallet = ONE fetch
 *   • per-wallet result cache (FUNDS_ALERT_CACHE_MIN, default 10 min) so a
 *     wallet shared across users inside one cycle is computed once
 *   • jittered pacing: 300–1200 ms sleep between wallet fetches — at most
 *     ~2 req/s against HL /info at any user count
 *   • hard cap FUNDS_ALERT_MAX_WALLETS_PER_CYCLE (default 60); remaining
 *     wallets rotate to the front of the next cycle so nobody starves
 *   • hysteresis lives in fundsAlertStore.evaluate (below→above crossing,
 *     re-arm <50% threshold or 12h cooldown)
 *   • alert body = compact branded message + standard Pear referral footer
 */

const walletTracker = require('./walletTracker');
const fundsEngine = require('./fundsEngine');
const fundsAlertStore = require('./fundsAlertStore');
const { appendFooter } = require('./branding');

function _clampInt(v, def, min, max) {
  const n = parseInt(v, 10);
  if (!Number.isFinite(n)) return def;
  return Math.min(max, Math.max(min, n));
}

const POLL_MIN = _clampInt(process.env.FUNDS_ALERT_POLL_MIN, 20, 15, 30);
const CACHE_MS =
  _clampInt(process.env.FUNDS_ALERT_CACHE_MIN, 10, 1, 60) * 60 * 1000;
const MAX_WALLETS_PER_CYCLE = _clampInt(
  process.env.FUNDS_ALERT_MAX_WALLETS_PER_CYCLE, 60, 5, 500
);
const JITTER_MIN_MS = 300;
const JITTER_MAX_MS = 1200;

function isEnabled() {
  return (
    (process.env.FUNDS_ALERT_ENABLED || 'true').toLowerCase() !== 'false'
  );
}

let _timer = null;
let _notify = null;
let _cache = new Map(); // wallet_lc → { at, view }
let _rotationCursor = 0;

function _sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function _jitter() {
  return JITTER_MIN_MS + Math.random() * (JITTER_MAX_MS - JITTER_MIN_MS);
}

async function _getView(wallet, { fetcher } = {}) {
  const lc = wallet.toLowerCase();
  const hit = _cache.get(lc);
  if (hit && Date.now() - hit.at < CACHE_MS) return { view: hit.view, cached: true };
  const fn = fetcher || fundsEngine.getDeployableView;
  const view = await fn(wallet);
  _cache.set(lc, { at: Date.now(), view });
  return { view, cached: false };
}

function _usd(n) {
  if (n == null || !Number.isFinite(n)) return 'fetch error';
  return `$${n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/**
 * Compact branded alert body. metric: 'total' | 'pm'.
 */
function buildAlertMessage({ wallet, view, metric, threshold }) {
  const title =
    metric === 'pm'
      ? '🏦 *BORROW HEADROOM AVAILABLE*'
      : '💰 *FUNDS AVAILABLE TO DEPLOY*';
  const headline =
    metric === 'pm'
      ? `PM borrow headroom hit ${_usd(view.pm_borrow_headroom)} (your threshold: $${Math.round(threshold).toLocaleString('en-US')})`
      : `Total deployable hit ${_usd(view.total_deployable)} (your threshold: $${Math.round(threshold).toLocaleString('en-US')})`;
  const lines = [title, '', headline, ''];
  lines.push(...fundsEngine.formatDeployableView(view, wallet));
  lines.push('', '_Manage: /fundsalert · Full view: /funds_');
  return appendFooter(lines.join('\n'));
}

/**
 * One scan cycle. Injectable deps for tests:
 *   opts.fetcher(wallet) → view       (replaces fundsEngine.getDeployableView)
 *   opts.now                          (clock override)
 *   opts.noJitter                     (skip sleeps in tests)
 * Returns telemetry { usersScanned, walletsScanned, fetches, alertsSent }.
 */
async function scanOnce(opts = {}) {
  const telemetry = { usersScanned: 0, walletsScanned: 0, fetches: 0, alertsSent: 0 };
  if (!isEnabled()) return telemetry;
  if (typeof _notify !== 'function' && !opts.notify) return telemetry;
  const notify = opts.notify || _notify;
  const now = opts.now || Date.now();

  const optedIn = fundsAlertStore.getAllOptedIn();
  if (optedIn.length === 0) return telemetry;
  telemetry.usersScanned = optedIn.length;

  // Build unique wallet → subscribers map (one fetch per wallet).
  const byWallet = new Map(); // wallet_lc → { wallet, subs: [{userId, threshold}] }
  for (const { userId, threshold } of optedIn) {
    for (const w of walletTracker.getUserWallets(userId)) {
      const lc = String(w.address).toLowerCase();
      if (!byWallet.has(lc)) byWallet.set(lc, { wallet: w.address, subs: [] });
      byWallet.get(lc).subs.push({ userId, threshold });
    }
  }

  // Rotation so the per-cycle cap never starves the tail.
  const entries = Array.from(byWallet.values());
  const start = entries.length > 0 ? _rotationCursor % entries.length : 0;
  const ordered = entries.slice(start).concat(entries.slice(0, start));
  const batch = ordered.slice(0, MAX_WALLETS_PER_CYCLE);
  _rotationCursor = (start + batch.length) % Math.max(entries.length, 1);

  for (const { wallet, subs } of batch) {
    telemetry.walletsScanned++;
    let view, cached;
    try {
      ({ view, cached } = await _getView(wallet, { fetcher: opts.fetcher }));
    } catch (e) {
      console.error('[fundsAlertScheduler] view failed for', wallet, e.message);
      continue;
    }
    if (!cached) {
      telemetry.fetches++;
      if (!opts.noJitter) await _sleep(_jitter());
    }
    if (!view || view.error) continue; // fetch error → never treat as $0

    for (const { userId, threshold } of subs) {
      // Gate 1 — total deployable.
      const totalGate = fundsAlertStore.evaluate(
        userId, wallet, 'total', view.total_deployable, threshold, now
      );
      // Gate 2 — PM borrow headroom, tracked separately for PM accounts.
      const pmGate =
        view.account_type === 'pm'
          ? fundsAlertStore.evaluate(
              userId, wallet, 'pm', view.pm_borrow_headroom, threshold, now
            )
          : { shouldFire: false, reason: 'NOT_PM' };

      // One message per wallet per cycle max — prefer the PM-specific alert
      // (it carries the projected-liq context) when both crossed together.
      const metric = pmGate.shouldFire ? 'pm' : totalGate.shouldFire ? 'total' : null;
      if (!metric) continue;
      try {
        await notify(
          parseInt(userId, 10),
          buildAlertMessage({ wallet, view, metric, threshold }),
          { parse_mode: 'Markdown' }
        );
        telemetry.alertsSent++;
      } catch (e) {
        console.error('[fundsAlertScheduler] notify failed for', userId, e.message);
      }
    }
  }
  return telemetry;
}

function startSchedule({ notify }) {
  if (!isEnabled()) {
    console.log('[fundsAlertScheduler] disabled via FUNDS_ALERT_ENABLED');
    return null;
  }
  _notify = notify;
  if (_timer) clearInterval(_timer);
  _timer = setInterval(() => {
    scanOnce().catch((e) =>
      console.error('[fundsAlertScheduler] scan failed:', e && e.message ? e.message : e)
    );
  }, POLL_MIN * 60 * 1000);
  if (typeof _timer.unref === 'function') _timer.unref();
  console.log(`[fundsAlertScheduler] started, every ${POLL_MIN} min (cap ${MAX_WALLETS_PER_CYCLE} wallets/cycle)`);
  return _timer;
}

function stopSchedule() {
  if (_timer) {
    clearInterval(_timer);
    _timer = null;
  }
}

function _resetForTests() {
  _cache = new Map();
  _rotationCursor = 0;
  _notify = null;
}

module.exports = {
  isEnabled,
  scanOnce,
  startSchedule,
  stopSchedule,
  buildAlertMessage,
  POLL_MIN,
  CACHE_MS,
  MAX_WALLETS_PER_CYCLE,
  _resetForTests,
};
