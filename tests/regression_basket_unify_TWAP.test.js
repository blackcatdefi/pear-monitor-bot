'use strict';

/**
 * R-PUBLIC-BASKET-UNIFY (4 may 2026) — TWAP 6-poll regression test.
 *
 * Reproduces the live production failure observed at 12:09–12:23 UTC on
 * 4 may 2026 (14 minutes, 6 BASKET_OPEN messages from wallet 0xc7AE for
 * what was actually ONE Pear basket whose 18 legs surfaced progressively
 * across a TWAP entry):
 *
 *   12:09 — 9 positions  (5 alts + 4 xyz)
 *   12:11 — 4 positions  (4 xyz only — different leg subset)
 *   12:13 — 12 positions (3 xyz + 9 cash:*)
 *   12:16 — 5 positions  (5 alts only)
 *   12:21 — 15 positions (5 alts + ANTHROPIC + 9 cash:*)
 *   12:23 — 13 positions (3 xyz + ANTHROPIC + 9 cash:*)
 *
 * Each poll's leg subset is unique → unique SHA-256 hash → bypasses
 * basketDedup. Polls 2-6 are >60s after poll 1 → bypasses 60s wallet
 * debounce. Result: 6 BASKET_OPEN emissions for ONE basket.
 *
 * Acceptance after R-PUBLIC-BASKET-UNIFY:
 *   • Exactly ONE BASKET_OPEN dispatches (the first poll).
 *   • Polls 2-6 are blocked by Gate-0 (wallet absolute lockout) with
 *     reason 'wallet_already_has_open_basket'.
 *   • Counters in healthServer accurately reflect 5 dedup'd events.
 *   • After lockout.markClosed + grace elapses, a FRESH basket can emit
 *     a new BASKET_OPEN.
 */

const test = require('node:test');
const assert = require('node:assert');
const path = require('path');
const os = require('os');

// Hermetic DBs for both basketDedup AND walletBasketLockout
process.env.BASKET_DEDUP_TTL_DAYS = '7';
process.env.BASKET_DEDUP_ENABLED = 'true';
process.env.OPEN_ALERTS_ENABLED = 'true';
process.env.DEDUP_DB_PATH = path.join(
  os.tmpdir(),
  `__test_basket_dedup_unify_${process.pid}.json`
);

const openAlerts = require('../src/openAlerts');
const basketDedup = require('../src/basketDedup');
const closeAlerts = require('../src/closeAlerts');
const lockout = require('../src/walletBasketLockout');
const healthServer = require('../src/healthServer');

const WALLET = '0xc7AE000000000000000000000000000000000001';

// The 6 leg subsets observed on production. Coin lists are taken
// verbatim from the user-supplied evidence.
const POLLS = [
  ['DYDX', 'OP', 'ARB', 'PYTH', 'ENA', 'ANTHROPIC', 'XYZ100', 'PLTR', 'BRENT'],
  ['ANTHROPIC', 'XYZ100', 'PLTR', 'BRENT'],
  ['XYZ100', 'PLTR', 'BRENT', 'CASH1', 'CASH2', 'CASH3', 'CASH4', 'CASH5', 'CASH6', 'CASH7', 'CASH8', 'CASH9'],
  ['DYDX', 'OP', 'ARB', 'PYTH', 'ENA'],
  ['DYDX', 'OP', 'ARB', 'PYTH', 'ENA', 'ANTHROPIC', 'CASH1', 'CASH2', 'CASH3', 'CASH4', 'CASH5', 'CASH6', 'CASH7', 'CASH8', 'CASH9'],
  ['XYZ100', 'PLTR', 'BRENT', 'ANTHROPIC', 'CASH1', 'CASH2', 'CASH3', 'CASH4', 'CASH5', 'CASH6', 'CASH7', 'CASH8', 'CASH9'],
];

function makePositions(coins) {
  return coins.map((coin, i) => ({
    coin,
    side: 'SHORT',
    size: -100 - i,
    entryPrice: 1.234 + i * 0.01,
    notional: 1000 + i * 10,
  }));
}

test('TWAP 6-poll basket emits EXACTLY ONE BASKET_OPEN (Gate-0 lockout)', async () => {
  basketDedup._resetForTests();
  openAlerts._resetWalletDebounceForTests();
  lockout._resetForTests({ persist: false });
  healthServer._resetForTests();
  closeAlerts._resetCachesForTests();

  let emittedCount = 0;
  const messages = [];
  const notify = async (chatId, msg) => {
    emittedCount += 1;
    messages.push({ chatId, msg });
  };

  const results = [];
  for (let i = 0; i < POLLS.length; i += 1) {
    const r = await openAlerts.emitAlerts({
      chatId: 1,
      wallet: WALLET,
      label: `BCD-PRIMARY (poll ${i + 1})`,
      newPositions: makePositions(POLLS[i]),
      notify,
    });
    results.push(r);
  }

  // Headline assertion
  assert.strictEqual(
    emittedCount,
    1,
    `expected exactly 1 BASKET_OPEN across 6 TWAP polls, got ${emittedCount}`
  );
  assert.strictEqual(results[0].type, 'BASKET_OPEN');
  assert.strictEqual(results[0].dispatched, 1);

  // Polls 2-6 must all be blocked by the wallet-level lockout (Gate-0).
  for (let i = 1; i < POLLS.length; i += 1) {
    assert.strictEqual(
      results[i].type,
      'BASKET_OPEN_WALLET_LOCKED',
      `poll ${i + 1} must be blocked by Gate-0 lockout (got ${results[i].type})`
    );
    assert.strictEqual(results[i].dispatched, 0);
    assert.strictEqual(results[i].reason, 'wallet_already_has_open_basket');
  }

  // /health forensic counters
  const status = healthServer.getStatus();
  assert.ok(
    status.spam_guard.events_deduplicated_lifetime >= 5,
    `expected ≥5 dedup events, got ${status.spam_guard.events_deduplicated_lifetime}`
  );
  assert.match(
    status.spam_guard.last_dedup_reason || '',
    /wallet_already_has_open_basket/,
    `expected last_dedup_reason to mention wallet_already_has_open_basket, got ${status.spam_guard.last_dedup_reason}`
  );

  // Lockout snapshot must show this wallet as OPEN
  const lockSnap = status.spam_guard.wallet_lockout;
  assert.ok(lockSnap.open_count >= 1, 'lockout snapshot must show ≥1 OPEN wallet');
  assert.ok(
    lockSnap.open_wallets.some((w) => w === WALLET.toLowerCase()),
    'wallet must appear in open_wallets'
  );
});

test('after markClosed + grace, a fresh basket on the same wallet can emit again', async () => {
  basketDedup._resetForTests();
  openAlerts._resetWalletDebounceForTests();
  closeAlerts._resetCachesForTests();

  // Use a tiny grace window so the test stays fast.
  let t = 5_000_000;
  lockout._resetForTests({ persist: false, now: () => t, closeGraceMs: 100 });
  healthServer._resetForTests();

  const notify = async () => {};

  // First basket — emits.
  const r1 = await openAlerts.emitAlerts({
    chatId: 1,
    wallet: WALLET,
    label: 'BCD',
    newPositions: makePositions(POLLS[0]),
    notify,
  });
  assert.strictEqual(r1.type, 'BASKET_OPEN');

  // Same wallet, second BASKET_OPEN attempt → blocked.
  const r2 = await openAlerts.emitAlerts({
    chatId: 1,
    wallet: WALLET,
    label: 'BCD',
    newPositions: makePositions(['NEWLEG1', 'NEWLEG2', 'NEWLEG3']),
    notify,
  });
  assert.strictEqual(r2.type, 'BASKET_OPEN_WALLET_LOCKED');

  // Mark closed and let grace elapse
  lockout.markClosed(WALLET);
  t += 200; // > closeGraceMs

  // Fresh basket on same wallet must now emit.
  // Drop the wallet debounce + closeAlerts dedup cache too so we're
  // isolating the lockout pathway. (The closeAlerts dedup window is keyed
  // on the wallet+'BASKET_OPEN'+minute and would otherwise still fire from
  // the r1 emit a few microseconds earlier.)
  openAlerts._resetWalletDebounceForTests();
  basketDedup._resetForTests();
  closeAlerts._resetCachesForTests();
  const r3 = await openAlerts.emitAlerts({
    chatId: 1,
    wallet: WALLET,
    label: 'BCD',
    newPositions: makePositions(['FRESHA', 'FRESHB', 'FRESHC']),
    notify,
  });
  assert.strictEqual(
    r3.type,
    'BASKET_OPEN',
    'after markClosed + grace, fresh basket must emit'
  );
});

test('release() rolls back lockout when SHA-256 dedup blocks emission', async () => {
  basketDedup._resetForTests();
  openAlerts._resetWalletDebounceForTests();
  closeAlerts._resetCachesForTests();
  lockout._resetForTests({ persist: false });

  const wallet = '0xRollbackTest000000000000000000000000aaaa';
  const notify = async () => {};
  const positions = makePositions(['BTC', 'ETH', 'SOL']);

  // First emit — populates basketDedup
  const r1 = await openAlerts.emitAlerts({
    chatId: 1,
    wallet,
    label: 'RELEASE-TEST',
    newPositions: positions,
    notify,
  });
  assert.strictEqual(r1.type, 'BASKET_OPEN');

  // Force the next attempt to lose Gate-0 by releasing first…
  lockout.release(wallet);
  // …so the SAME positions on the same wallet now hit Gate-2 (SHA-256
  // dedup) instead. After dedup blocks, the lockout must be released so
  // the wallet isn't stuck OPEN.
  openAlerts._resetWalletDebounceForTests();
  const r2 = await openAlerts.emitAlerts({
    chatId: 1,
    wallet,
    label: 'RELEASE-TEST',
    newPositions: positions,
    notify,
  });
  assert.strictEqual(
    r2.type,
    'BASKET_OPEN_DEDUPED',
    `expected dedup to block, got ${r2.type}`
  );

  // After dedup, lockout state must NOT show wallet as OPEN.
  const s = lockout.getState(wallet);
  assert.notStrictEqual(s.state, 'OPEN', 'lockout must be released after dedup block');
});
