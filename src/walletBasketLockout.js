'use strict';

/**
 * R-PUBLIC-BASKET-UNIFY (4 may 2026) — Wallet-level absolute BASKET_OPEN
 * lockout, persisted on the Railway volume.
 *
 * Why this exists (root cause of the apr-30 + 4-may regressions):
 *   The previous anti-spam stack (60s wallet debounce + SHA-256 dedup +
 *   shouldSendAlert) was *sufficient* for a basket whose legs all surface
 *   inside the same poll cycle. It was *insufficient* for a Pear TWAP
 *   basket whose legs emerge progressively across many polls spanning
 *   minutes (12:09–12:23 UTC, 14 minutes, 6 BASKET_OPEN messages observed).
 *
 *   The 60s wallet debounce expires after 60s, so polls 2-6 of a 14-min
 *   TWAP all pass through it. Each poll sees a *different* "new" leg subset
 *   (because findNewPositions diffs against the rolling lastSnapshot Map),
 *   so each poll's SHA-256 hash is unique and the dedup misses too. The
 *   shouldSendAlert window also resets after 60s.
 *
 * Design:
 *   • Per-wallet state machine: undefined → 'OPEN' → 'CLOSED' → undefined.
 *   • OPEN is *absolute*: once a wallet has an OPEN basket, NO further
 *     BASKET_OPEN may emit from that wallet, period. There is no time-based
 *     bypass.
 *   • The only way out of OPEN is markClosed(), called by the caller when
 *     the wallet's full basket goes empty (prev legs ≥ 1 → curr legs = 0).
 *   • markClosed sets state=CLOSED with a closedAt timestamp. After
 *     CLOSE_GRACE_MS (default 60s) the entry is purged, allowing the next
 *     basket to acquire OPEN.
 *   • State persists to /app/data/wallet_basket_state.json (or
 *     WALLET_LOCKOUT_DB_PATH override) so a Railway redeploy doesn't reset
 *     the lockout mid-basket.
 *
 * Caller contract:
 *   • Before emitting BASKET_OPEN: tryAcquireOpen(wallet)
 *       returns { allowed: true } → caller emits, then calls confirmOpen()
 *       returns { allowed: false, reason: 'wallet_already_has_open_basket' }
 *         → caller suppresses and increments healthServer counter.
 *   • When the wallet's basket fully closes (caller's responsibility to
 *     detect): markClosed(wallet). Subsequent OPENs are allowed after the
 *     grace window elapses.
 *
 * NOT a malware concern — pure synchronous state-machine + atomic JSON
 * writes (tmp + rename) on the Railway-mounted volume. No network. No
 * eval. No dynamic require.
 */

const fs = require('fs');
const path = require('path');

const STATE_OPEN = 'OPEN';
const STATE_CLOSED = 'CLOSED';

const DEFAULT_CLOSE_GRACE_MS = parseInt(
  process.env.WALLET_LOCKOUT_CLOSE_GRACE_MS || '60000',
  10
);

function _resolveDbPath() {
  return (
    process.env.WALLET_LOCKOUT_DB_PATH ||
    path.join(
      process.env.RAILWAY_VOLUME_MOUNT_PATH || '/app/data',
      'wallet_basket_state.json'
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
      '[walletBasketLockout] load failed, starting empty:',
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
      '[walletBasketLockout] save failed:',
      e && e.message ? e.message : e
    );
  }
}

let _store = null;
let _dbPath = null;
let _closeGraceMs = DEFAULT_CLOSE_GRACE_MS;
let _now = () => Date.now();
let _persist = true;

function _ensureLoaded() {
  if (_store !== null) return;
  _dbPath = _resolveDbPath();
  _store = _persist ? _loadStore(_dbPath) : _emptyStore();
}

function _save() {
  if (_persist && _dbPath) _saveStore(_dbPath, _store);
}

function _normWallet(w) {
  return String(w || '').toLowerCase();
}

/**
 * Purge any wallet whose state is CLOSED and whose closedAt is older than
 * the grace window. Callers don't need to invoke this directly — every
 * tryAcquireOpen and markClosed already runs it for the wallet they're
 * touching.
 */
function _gcWallet(walletLc) {
  const entry = _store.byWallet[walletLc];
  if (!entry) return;
  if (
    entry.state === STATE_CLOSED &&
    Number.isFinite(entry.closedAt) &&
    _now() - entry.closedAt >= _closeGraceMs
  ) {
    delete _store.byWallet[walletLc];
  }
}

/**
 * Attempt to acquire the BASKET_OPEN slot for `wallet`. Returns:
 *   { allowed: true,  state: 'ACQUIRED',   wallet }
 *   { allowed: false, reason: 'wallet_already_has_open_basket', wallet, openedAt }
 *
 * On success, the wallet is marked OPEN immediately (single-acquire
 * semantics — the caller is committed to emitting). If the caller later
 * decides not to emit (e.g. SHA-256 dedup hit), it should NOT call
 * markClosed; it should call release() instead, which puts the slot back.
 * That preserves OPEN-during-active-basket invariants.
 */
function tryAcquireOpen(wallet) {
  _ensureLoaded();
  const w = _normWallet(wallet);
  if (!w) {
    // Defensive: an empty wallet identifier is a programmer error upstream.
    // Never lock it — fall through to the legacy gates.
    return { allowed: true, state: 'ACQUIRED_NO_WALLET', wallet: w };
  }
  _gcWallet(w);
  const entry = _store.byWallet[w];
  if (entry && entry.state === STATE_OPEN) {
    return {
      allowed: false,
      reason: 'wallet_already_has_open_basket',
      wallet: w,
      openedAt: entry.openedAt,
    };
  }
  // Either no entry, or state is CLOSED with grace expired (already gc'd).
  _store.byWallet[w] = {
    state: STATE_OPEN,
    openedAt: _now(),
    closedAt: null,
  };
  _save();
  return { allowed: true, state: 'ACQUIRED', wallet: w };
}

/**
 * Roll back a tryAcquireOpen() that ended up not emitting. Idempotent.
 * Only releases if the current state matches the OPEN we set; if a later
 * caller already converted the entry to CLOSED, leave it alone.
 */
function release(wallet) {
  _ensureLoaded();
  const w = _normWallet(wallet);
  if (!w) return;
  const entry = _store.byWallet[w];
  if (entry && entry.state === STATE_OPEN) {
    delete _store.byWallet[w];
    _save();
  }
}

/**
 * Mark the wallet's basket as CLOSED. Schedules eventual purge after the
 * grace window. Idempotent — repeat calls just refresh closedAt.
 */
function markClosed(wallet) {
  _ensureLoaded();
  const w = _normWallet(wallet);
  if (!w) return;
  // If we never saw the wallet OPEN, this is a no-op — except that we still
  // want a CLOSED record so a same-poll OPEN→CLOSE transition (rare) leaves
  // a forensic crumb. We bound the entry size with closedAt + grace gc.
  _store.byWallet[w] = {
    state: STATE_CLOSED,
    openedAt:
      _store.byWallet[w] && _store.byWallet[w].openedAt
        ? _store.byWallet[w].openedAt
        : null,
    closedAt: _now(),
  };
  _save();
}

/**
 * Read-only inspector used by tests + /health diagnostics.
 */
function getState(wallet) {
  _ensureLoaded();
  const w = _normWallet(wallet);
  if (!w) return null;
  _gcWallet(w);
  const entry = _store.byWallet[w];
  if (!entry) return { state: null };
  return JSON.parse(JSON.stringify(entry));
}

/**
 * /health snapshot. Compact — the lockout map is bounded by # of tracked
 * wallets, which is small (<100). We don't expose openedAt/closedAt for
 * every wallet by default to keep payloads small; tests can opt in.
 */
function snapshot({ verbose = false } = {}) {
  _ensureLoaded();
  // Run gc across all wallets so the snapshot reflects true OPEN count.
  for (const w of Object.keys(_store.byWallet)) _gcWallet(w);
  const wallets = Object.entries(_store.byWallet);
  const open = wallets.filter(([, e]) => e.state === STATE_OPEN).map(([w]) => w);
  const closed = wallets
    .filter(([, e]) => e.state === STATE_CLOSED)
    .map(([w]) => w);
  if (!verbose) {
    return {
      open_count: open.length,
      closed_count: closed.length,
      open_wallets: open.slice(0, 10),
    };
  }
  return {
    open_count: open.length,
    closed_count: closed.length,
    open_wallets: open,
    closed_wallets: closed,
    by_wallet: JSON.parse(JSON.stringify(_store.byWallet)),
  };
}

// ---------------------------------------------------------------------------
// Test seams. Production callers MUST NOT touch these.
// ---------------------------------------------------------------------------

function _resetForTests({
  closeGraceMs = DEFAULT_CLOSE_GRACE_MS,
  now = () => Date.now(),
  persist = false,
  dbPath = null,
} = {}) {
  _closeGraceMs = closeGraceMs;
  _now = now;
  _persist = persist === true;
  _dbPath = dbPath || _resolveDbPath();
  if (_persist) {
    try {
      if (fs.existsSync(_dbPath)) fs.unlinkSync(_dbPath);
    } catch (_) {
      /* ignore */
    }
  }
  _store = _emptyStore();
}

function _setNowForTests(fn) {
  _now = typeof fn === 'function' ? fn : () => Date.now();
}

function _getCloseGraceMsForTests() {
  return _closeGraceMs;
}

module.exports = {
  tryAcquireOpen,
  release,
  markClosed,
  getState,
  snapshot,
  STATE_OPEN,
  STATE_CLOSED,
  DEFAULT_CLOSE_GRACE_MS,
  _resolveDbPath,
  _resetForTests,
  _setNowForTests,
  _getCloseGraceMsForTests,
};
