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

// R-PUBLIC-BASKET-SPAM-NUCLEAR — forensic counters with no-op fallback so
// pure unit tests don't need to stub healthServer.
const _healthCounters = (() => {
  try {
    return require('./healthServer');
  } catch (_) {
    return {
      recordPhantomSuppressed: () => {},
      recordEventDeduplicated: () => {},
    };
  }
})();

// R-PUBLIC-BASKET-SPAM-NUCLEAR — persistent SHA-256 dedup. Required so
// 18-leg baskets don't get re-emitted when the basket detector splits them
// into two consecutive polls (Bug C: 1 basket = 2 BASKET_OPEN events
// because Hyperliquid surfaces TWAP fills across multiple snapshots).
const basketDedup = require('./basketDedup');

// R-PUBLIC-BASKET-UNIFY (4 may 2026) — wallet-level absolute lockout.
// Gate-0 (strictest gate): once a wallet has emitted BASKET_OPEN, NO
// further BASKET_OPEN may emit from that wallet until the basket fully
// closes (caller invokes lockout.markClosed) and the close-grace window
// elapses. Independent of and stricter than the 60s debounce — survives
// the 14-min Pear TWAP windows that broke R-PUBLIC-BASKET-SPAM-NUCLEAR.
const lockout = require('./walletBasketLockout');

const BASKET_WINDOW_MS = 5 * 60 * 1000;
const BASKET_MIN_COUNT = 3;

// R-PUBLIC-BASKET-SPAM-NUCLEAR (Bug C) — 60s wallet-level debounce. This
// is independent of and stricter than the per-coin shouldSendAlert window:
// even if the SHA-256 hash differs (because the "split basket" emerges with
// a different leg subset on each poll), we refuse to emit a *second*
// BASKET_OPEN for the same wallet within 60s of the first.
//
// Implemented as an in-memory Map<wallet, lastEmitMs>. We accept that this
// resets across bot restarts — that's why basketDedup (persistent SHA-256)
// stacks on top. The debounce is the fast lane; the SHA-256 is the durable
// lane. Either gate alone is sufficient to suppress the duplicate.
const BASKET_WALLET_DEBOUNCE_MS = parseInt(
  process.env.BASKET_WALLET_DEBOUNCE_MS || `${60 * 1000}`,
  10
);
const _walletLastBasketEmit = new Map();

function _walletDebounceCheck(wallet) {
  const w = String(wallet || '').toLowerCase();
  if (!w) return { allowed: true };
  const last = _walletLastBasketEmit.get(w);
  if (last && Date.now() - last < BASKET_WALLET_DEBOUNCE_MS) {
    return {
      allowed: false,
      ageMs: Date.now() - last,
    };
  }
  return { allowed: true };
}

function _walletDebounceMark(wallet) {
  const w = String(wallet || '').toLowerCase();
  if (!w) return;
  _walletLastBasketEmit.set(w, Date.now());
}

function _resetWalletDebounceForTests() {
  _walletLastBasketEmit.clear();
}

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
    '🚀 *NEW BASKET OPENED*',
    '',
    `📍 Wallet: ${label}`,
    `📊 Composition (${positions.length} positions):`,
  ];
  for (const p of positions) {
    const side = p.side || (p.size < 0 ? 'SHORT' : 'LONG');
    lines.push(`  • ${p.coin} ${side} @ $${_fmtPx(p.entryPrice)}`);
  }
  lines.push('');
  lines.push(`💰 Total notional: ${_fmtUsd(totalNotional)}`);
  lines.push(`⚡ Leverage: ${lev}`);
  lines.push(`🎯 Strategy: TWAP entry (time-based DCA)`);
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
    `${emoji} *NEW POSITION OPENED*`,
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
    // Gate 0 (R-PUBLIC-BASKET-UNIFY) — wallet-level ABSOLUTE lockout.
    // The strictest gate: if this wallet already has an OPEN basket in
    // the persistent state machine, refuse to emit. The lockout only
    // releases when the caller invokes lockout.markClosed (i.e. when the
    // basket fully closes) and the close-grace window elapses.
    //
    // This is the canonical fix for the 4-may-2026 14-min TWAP regression
    // where Pear basket legs surfaced across 6 polls and each poll emitted
    // a fresh BASKET_OPEN with a different leg subset (different hash =
    // bypassed SHA-256 dedup; >60s after first emit = bypassed wallet
    // debounce).
    let lockoutResult = { allowed: true };
    try {
      lockoutResult = lockout.tryAcquireOpen(wallet);
    } catch (_) {
      /* lockout failure must not block alerts — fail-open */
    }
    if (!lockoutResult.allowed) {
      try {
        _healthCounters.recordEventDeduplicated(
          `openAlerts.${lockoutResult.reason || 'wallet_lockout'}:${String(wallet).slice(0, 10)}`
        );
      } catch (_) {
        /* ignore */
      }
      return {
        dispatched: 0,
        type: 'BASKET_OPEN_WALLET_LOCKED',
        reason: lockoutResult.reason || 'wallet_already_has_open_basket',
      };
    }

    // Gate 1 — fast in-memory wallet-level debounce. Even if SHA-256 hash
    // differs (split basket on Bug C), 60s after the first emit blocks
    // the second.
    const debounce = _walletDebounceCheck(wallet);
    if (!debounce.allowed) {
      try {
        _healthCounters.recordPhantomSuppressed(
          `openAlerts.wallet_debounce:${String(wallet).slice(0, 10)}:${debounce.ageMs}ms`
        );
      } catch (_) {
        /* ignore */
      }
      // Roll back the Gate-0 acquire so it doesn't lock the wallet on a
      // path that ultimately doesn't emit.
      try { lockout.release(wallet); } catch (_) { /* ignore */ }
      return { dispatched: 0, type: 'BASKET_OPEN_WALLET_DEBOUNCED' };
    }

    // Gate 2 — persistent SHA-256 dedup (Bug D). Survives bot restarts
    // via Railway volume. checkAlreadyAlerted internally increments the
    // events_deduplicated_lifetime counter on a hit.
    let dedupResult = { wasAlerted: false, hash: null };
    try {
      dedupResult = basketDedup.checkAlreadyAlerted(wallet, ev.positions);
    } catch (_) {
      /* basketDedup failure must not block alerts — fail-open */
    }
    if (dedupResult.wasAlerted) {
      // counter already incremented inside basketDedup — return early
      try { lockout.release(wallet); } catch (_) { /* ignore */ }
      return { dispatched: 0, type: 'BASKET_OPEN_DEDUPED' };
    }

    // Gate 3 — legacy shouldSendAlert keyed on wallet+'BASKET_OPEN' string
    // (60s window per process). Kept as belt-and-suspenders.
    if (!shouldSendAlert(wallet, 'BASKET_OPEN')) {
      try {
        _healthCounters.recordPhantomSuppressed(
          `openAlerts.shouldSendAlert_block:${String(wallet).slice(0, 10)}`
        );
      } catch (_) {
        /* ignore */
      }
      try { lockout.release(wallet); } catch (_) { /* ignore */ }
      return { dispatched: 0, type: 'BASKET_OPEN_DEDUPED' };
    }

    // All four gates clear — emit and mark.
    const msg = formatBasketOpenAlert(label, ev.positions);
    const keyboard = pearUrlBuilder.buildInlineKeyboard(
      _enrichWithNotional(ev.positions)
    );
    await notify(chatId, msg, keyboard ? { reply_markup: keyboard } : undefined);
    _walletDebounceMark(wallet);
    try {
      basketDedup.markAsAlerted(wallet, ev.positions);
    } catch (_) {
      /* persist failure logged inside basketDedup */
    }
    return { dispatched: 1, type: 'BASKET_OPEN' };
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
  BASKET_WALLET_DEBOUNCE_MS,
  // test-only — never call from production
  _resetWalletDebounceForTests,
};
