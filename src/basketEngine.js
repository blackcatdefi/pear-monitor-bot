'use strict';

/**
 * R-BASKET (3 may 2026) — Basket lifecycle engine.
 *
 * Snapshot-driven state machine that collapses ~200 messages/day per wallet
 * into ≤2 messages per basket lifecycle (one OPEN, one CLOSE). Replaces the
 * per-leg emission loops in walletTrackerScheduler.js and
 * externalWalletTracker.js.
 *
 * Why this exists (root-cause notes):
 *   1. Per-leg messages: existing code emitted one Telegram message per
 *      `assetPosition` returned by Hyperliquid. A 10-leg Pear basket → 10
 *      OPEN messages, 10 CLOSE messages, plus a "BASKET CLOSED" wrapper.
 *   2. No state-diff: every poll cycle re-classified live legs as "new",
 *      yielding duplicate OPENs across cycles.
 *   3. False "Manual close $0.00": when a user added margin to one leg,
 *      that leg flickered through size=0 momentarily → bot saw a close,
 *      computed PnL from a stale empty snapshot, emitted "$0 close".
 *   4. No basket abstraction: Pear baskets have no on-chain ID; basket
 *      grouping lives only in Pear's auth-gated backend, so any third-
 *      party tracker MUST reconstruct baskets client-side from the
 *      `(wallet, sorted-leg-tuple)` signature.
 *
 * Design (poll-cadence-friendly):
 *   • States: EMPTY → ACTIVE → PENDING_CLOSE → EMPTY (or rotation back to
 *     ACTIVE if same signature reappears within zombie grace).
 *   • Signature: deterministic key from sorted (LONG coins, SHORT coins).
 *   • Zombie grace (default 90 s = 1.5 poll cycles at 60 s cadence)
 *     suppresses transient close events caused by leg-resize race conditions.
 *   • Persistence: tiny JSON file on the Railway volume (same pattern as
 *     basketDedup.js), so a redeploy doesn't re-fire OPEN for live baskets.
 *   • Pure-ish: events are returned synchronously; the caller does the
 *     side-effects (Telegram send + PnL fetch + sanity gate). This makes
 *     the engine trivially unit-testable with an injectable clock.
 */

const fs = require('fs');
const path = require('path');

const STATES = Object.freeze({
  EMPTY: 'EMPTY',
  ACTIVE: 'ACTIVE',
  PENDING_CLOSE: 'PENDING_CLOSE',
});

// Default 90 s — slightly larger than one 60-second poll cycle so a single
// missed/late poll never triggers a spurious close. Override per-instance
// for tests via the constructor option.
const DEFAULT_ZOMBIE_GRACE_MS = parseInt(
  process.env.BASKET_ZOMBIE_GRACE_MS || '90000',
  10
);

function _resolveDbPath() {
  return (
    process.env.BASKET_ENGINE_DB_PATH ||
    path.join(
      process.env.RAILWAY_VOLUME_MOUNT_PATH || '/app/data',
      'basket_engine.json'
    )
  );
}

function _emptyStore() {
  return { byWallet: {}, version: 1 };
}

function _loadStore(dbPath) {
  try {
    if (fs.existsSync(dbPath)) {
      const raw = JSON.parse(fs.readFileSync(dbPath, 'utf-8'));
      if (raw && typeof raw === 'object' && raw.byWallet) {
        if (!raw.version) raw.version = 1;
        return raw;
      }
    }
  } catch (e) {
    console.error(
      '[basketEngine] load failed, starting empty:',
      e && e.message ? e.message : e
    );
  }
  return _emptyStore();
}

function _saveStore(dbPath, store) {
  try {
    fs.mkdirSync(path.dirname(dbPath), { recursive: true });
    const tmp = dbPath + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(store, null, 2));
    fs.renameSync(tmp, dbPath);
  } catch (e) {
    console.error(
      '[basketEngine] save failed:',
      e && e.message ? e.message : e
    );
  }
}

/**
 * Deterministic basket key. Two snapshots with the same set of (coin, side)
 * tuples produce identical signatures regardless of array order.
 *
 *   [BTC LONG, ETH SHORT]   → "L:BTC|S:ETH"
 *   [ETH SHORT, BTC LONG]   → "L:BTC|S:ETH"
 *   []                      → ""    (empty signature ⇒ no positions)
 */
function basketSignature(positions) {
  if (!Array.isArray(positions) || positions.length === 0) return '';
  const longs = [];
  const shorts = [];
  for (const p of positions) {
    if (!p || !p.coin) continue;
    const sz = Number(p.size);
    let side = p.side;
    if (!side) side = sz < 0 ? 'SHORT' : 'LONG';
    side = String(side).toUpperCase();
    const coin = String(p.coin).toUpperCase();
    if (side === 'LONG') longs.push(coin);
    else if (side === 'SHORT') shorts.push(coin);
  }
  longs.sort();
  shorts.sort();
  return `L:${longs.join(',')}|S:${shorts.join(',')}`;
}

/**
 * Snapshot-driven basket lifecycle engine.
 *
 *   const engine = new BasketEngine();
 *   const events = engine.processSnapshot({ walletKey: '0x…', positions, baseline: prev === null });
 *   for (const e of events) { … render + send … }
 *
 * Returned events:
 *   { type: 'BASKET_OPEN',  walletKey, signature, legs, openedAt }
 *   { type: 'BASKET_CLOSE', walletKey, signature, legs, openedAt, closedAt }
 *
 * No more than ONE OPEN and ONE CLOSE event are returned for any basket
 * lifecycle. Multi-leg basket grouping is automatic via the signature.
 */
class BasketEngine {
  constructor({
    now = () => Date.now(),
    zombieGraceMs = DEFAULT_ZOMBIE_GRACE_MS,
    persist = true,
    dbPath = null,
    initialStore = null,
  } = {}) {
    this._now = now;
    this._zombieGrace = zombieGraceMs;
    this._persist = persist === true;
    this._dbPath = dbPath || _resolveDbPath();
    this._store = initialStore
      ? initialStore
      : this._persist
      ? _loadStore(this._dbPath)
      : _emptyStore();
  }

  _wallet(walletKey) {
    const lc = String(walletKey || '').toLowerCase();
    if (!this._store.byWallet[lc]) {
      this._store.byWallet[lc] = {
        state: STATES.EMPTY,
        signature: '',
        legs: [],
        openedAt: null,
        lastSeenAt: null,
        pendingClose: null,
      };
    }
    return this._store.byWallet[lc];
  }

  /**
   * Push a fresh snapshot through the engine.
   *
   * @param {object}   p
   * @param {string}   p.walletKey  — anything stable per wallet (address, addr+chatId, …).
   * @param {Array}    p.positions  — array of { coin, side, size, entryPrice|entryPx, … }
   * @param {boolean=} p.baseline   — true on the first poll cycle for this wallet
   *                                  (record state silently, never emit).
   * @returns {Array}  events (zero, one, or two)
   */
  processSnapshot({ walletKey, positions, baseline = false }) {
    const w = this._wallet(walletKey);
    const now = this._now();
    const sig = basketSignature(positions);
    const events = [];

    if (baseline) {
      // First contact — adopt current state, NEVER emit an alert. The very
      // first time we see a wallet, anything it's already running is "old
      // news" from our perspective.
      if (sig) {
        w.state = STATES.ACTIVE;
        w.signature = sig;
        w.legs = positions.slice();
        w.openedAt = now;
        w.lastSeenAt = now;
      } else {
        w.state = STATES.EMPTY;
        w.signature = '';
        w.legs = [];
        w.openedAt = null;
        w.lastSeenAt = now;
      }
      w.pendingClose = null;
      this._maybeSave();
      return events;
    }

    if (sig) {
      // Some positions present.
      if (w.state === STATES.PENDING_CLOSE) {
        if (sig === w.signature) {
          // Same basket reappeared within the zombie grace → ROTATION.
          // Silently cancel the pending close and stay ACTIVE.
          w.state = STATES.ACTIVE;
          w.legs = positions.slice();
          w.lastSeenAt = now;
          w.pendingClose = null;
        } else {
          // Different basket appeared while old one was zombieing. The old
          // basket is genuinely closed — emit CLOSE for it, then OPEN the
          // new one.
          events.push(this._buildCloseEvent(walletKey, w));
          w.state = STATES.ACTIVE;
          w.signature = sig;
          w.legs = positions.slice();
          w.openedAt = now;
          w.lastSeenAt = now;
          w.pendingClose = null;
          events.push(this._buildOpenEvent(walletKey, w));
        }
      } else if (w.state === STATES.EMPTY) {
        // Fresh basket forming.
        w.state = STATES.ACTIVE;
        w.signature = sig;
        w.legs = positions.slice();
        w.openedAt = now;
        w.lastSeenAt = now;
        events.push(this._buildOpenEvent(walletKey, w));
      } else if (w.state === STATES.ACTIVE) {
        // Already alerted on this basket. Silent refresh; never re-emit.
        // If signature mutates (leg added/removed/swapped) we treat it as
        // a SILENT modification — Phase 1 favours under-emitting over
        // over-emitting. The eventual close still fires.
        w.signature = sig;
        w.legs = positions.slice();
        w.lastSeenAt = now;
      }
    } else {
      // No positions in current snapshot.
      if (w.state === STATES.ACTIVE) {
        // Enter the zombie window — wait one grace period before we
        // commit to "this basket is dead". Stops false closes during
        // single-leg margin-add flickers.
        w.state = STATES.PENDING_CLOSE;
        w.pendingClose = { sinceTs: now };
      } else if (w.state === STATES.PENDING_CLOSE) {
        const startedAt = w.pendingClose ? w.pendingClose.sinceTs : now;
        if (now - startedAt >= this._zombieGrace) {
          // Grace elapsed with positions still empty — emit CLOSE.
          events.push(this._buildCloseEvent(walletKey, w));
          w.state = STATES.EMPTY;
          w.signature = '';
          w.legs = [];
          w.openedAt = null;
          w.pendingClose = null;
        }
        // else: still in grace — wait.
      }
      // EMPTY → EMPTY = nothing to do.
    }

    this._maybeSave();
    return events;
  }

  _buildOpenEvent(walletKey, w) {
    return {
      type: 'BASKET_OPEN',
      walletKey,
      signature: w.signature,
      legs: w.legs.map((p) => ({ ...p })),
      openedAt: w.openedAt,
    };
  }

  _buildCloseEvent(walletKey, w) {
    return {
      type: 'BASKET_CLOSE',
      walletKey,
      signature: w.signature,
      legs: w.legs.map((p) => ({ ...p })),
      openedAt: w.openedAt,
      closedAt: this._now(),
    };
  }

  _maybeSave() {
    if (this._persist) _saveStore(this._dbPath, this._store);
  }

  /**
   * Inspect the engine's view of a wallet — exposed for tests + logging.
   */
  peek(walletKey) {
    return JSON.parse(JSON.stringify(this._wallet(walletKey)));
  }

  /**
   * Force-evict a wallet (e.g., user untracked it). Drops in-memory state
   * and persists.
   */
  forget(walletKey) {
    const lc = String(walletKey || '').toLowerCase();
    if (this._store.byWallet[lc]) {
      delete this._store.byWallet[lc];
      this._maybeSave();
    }
  }

  _resetForTests() {
    this._store = _emptyStore();
    if (this._persist) {
      try {
        if (fs.existsSync(this._dbPath)) fs.unlinkSync(this._dbPath);
      } catch (_) {}
    }
  }
}

module.exports = {
  BasketEngine,
  basketSignature,
  STATES,
  DEFAULT_ZOMBIE_GRACE_MS,
  _resolveDbPath,
};
