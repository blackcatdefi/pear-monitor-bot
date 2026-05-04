'use strict';

/**
 * R-PUBLIC-BASKET-SPAM-NUCLEAR (Bug C) — 60s wallet-level debounce on
 * BASKET_OPEN. Independent of and stricter than basketDedup's SHA-256
 * persistence, because Pear basket TWAPs sometimes emerge as 2 sub-baskets
 * across consecutive polls (different leg subsets → different SHA-256
 * hashes). The wallet debounce catches that case.
 *
 * Acceptance:
 *   • First BASKET_OPEN for a wallet always dispatches.
 *   • A second BASKET_OPEN within BASKET_WALLET_DEBOUNCE_MS is suppressed.
 *   • A different wallet within the same window still dispatches.
 *   • After the window expires, the same wallet can dispatch again.
 */

const test = require('node:test');
const assert = require('node:assert');
const path = require('path');

// Force the in-memory dedup store + reset the basketDedup persistent file
// so this suite is hermetic.
process.env.BASKET_DEDUP_TTL_DAYS = '7';
process.env.BASKET_DEDUP_ENABLED = 'true';
process.env.OPEN_ALERTS_ENABLED = 'true';
process.env.DEDUP_DB_PATH = path.join(
  __dirname,
  '..',
  'data',
  '__test_basket_dedup_walletDebounce.json'
);

const openAlerts = require('../src/openAlerts');
const basketDedup = require('../src/basketDedup');

function makeBasket(coins, side = 'SHORT') {
  return coins.map((coin, i) => ({
    coin,
    side,
    size: side === 'LONG' ? 100 : -100,
    entryPrice: 1.234567 + i,
    notional: 12345 + i,
  }));
}

test('emitAlerts: first BASKET_OPEN dispatches', async () => {
  basketDedup._resetForTests();
  openAlerts._resetWalletDebounceForTests();

  const calls = [];
  const notify = async (chatId, msg) => {
    calls.push({ chatId, msg });
  };

  const result = await openAlerts.emitAlerts({
    chatId: 1,
    wallet: '0xAAAA000000000000000000000000000000000001',
    label: 'TEST-1',
    newPositions: makeBasket(['BTC', 'ETH', 'SOL']),
    notify,
  });

  assert.strictEqual(result.dispatched, 1, 'first basket should dispatch');
  assert.strictEqual(result.type, 'BASKET_OPEN');
  assert.strictEqual(calls.length, 1);
  assert.match(calls[0].msg, /NEW BASKET OPENED/);
});

test('emitAlerts: second BASKET_OPEN within debounce is suppressed (different SHA hash)', async () => {
  basketDedup._resetForTests();
  openAlerts._resetWalletDebounceForTests();

  const wallet = '0xBBBB000000000000000000000000000000000002';
  const notify = async () => {};

  // First basket — 3 legs.
  const r1 = await openAlerts.emitAlerts({
    chatId: 1,
    wallet,
    label: 'TEST-2',
    newPositions: makeBasket(['BTC', 'ETH', 'SOL']),
    notify,
  });
  assert.strictEqual(r1.type, 'BASKET_OPEN');

  // Immediately after — DIFFERENT 3-leg basket (different SHA hash) but
  // SAME wallet within debounce window. Must be suppressed by wallet-level
  // debounce, not by basketDedup (because the hash differs).
  const r2 = await openAlerts.emitAlerts({
    chatId: 1,
    wallet,
    label: 'TEST-2',
    newPositions: makeBasket(['DYDX', 'OP', 'ARB']),
    notify,
  });
  assert.strictEqual(
    r2.type,
    'BASKET_OPEN_WALLET_DEBOUNCED',
    'second basket within 60s must be wallet-debounced'
  );
  assert.strictEqual(r2.dispatched, 0);
});

test('emitAlerts: identical basket within debounce is dedup-suppressed', async () => {
  basketDedup._resetForTests();
  openAlerts._resetWalletDebounceForTests();

  const wallet = '0xCCCC000000000000000000000000000000000003';
  const notify = async () => {};
  const positions = makeBasket(['BTC', 'ETH', 'SOL']);

  const r1 = await openAlerts.emitAlerts({
    chatId: 1,
    wallet,
    label: 'TEST-3',
    newPositions: positions,
    notify,
  });
  assert.strictEqual(r1.type, 'BASKET_OPEN');

  // Manually expire the wallet debounce so we're testing the SHA-256 gate.
  openAlerts._resetWalletDebounceForTests();

  const r2 = await openAlerts.emitAlerts({
    chatId: 1,
    wallet,
    label: 'TEST-3',
    newPositions: positions,
    notify,
  });
  assert.strictEqual(
    r2.type,
    'BASKET_OPEN_DEDUPED',
    'identical basket must be SHA-256-deduped'
  );
});

test('emitAlerts: different wallet bypasses debounce', async () => {
  basketDedup._resetForTests();
  openAlerts._resetWalletDebounceForTests();

  const notify = async () => {};

  const r1 = await openAlerts.emitAlerts({
    chatId: 1,
    wallet: '0xDDDD000000000000000000000000000000000004',
    label: 'WALLET-A',
    newPositions: makeBasket(['BTC', 'ETH', 'SOL']),
    notify,
  });
  assert.strictEqual(r1.type, 'BASKET_OPEN');

  const r2 = await openAlerts.emitAlerts({
    chatId: 1,
    wallet: '0xEEEE000000000000000000000000000000000005',
    label: 'WALLET-B',
    newPositions: makeBasket(['DYDX', 'OP', 'ARB']),
    notify,
  });
  assert.strictEqual(
    r2.type,
    'BASKET_OPEN',
    'different wallet must dispatch independently'
  );
});

test('BASKET_WALLET_DEBOUNCE_MS exported and ≥ 60000', () => {
  assert.ok(
    Number.isFinite(openAlerts.BASKET_WALLET_DEBOUNCE_MS),
    'BASKET_WALLET_DEBOUNCE_MS must be exported'
  );
  assert.ok(
    openAlerts.BASKET_WALLET_DEBOUNCE_MS >= 60000,
    'debounce must be at least 60s — Pear TWAP basket emergence window'
  );
});
