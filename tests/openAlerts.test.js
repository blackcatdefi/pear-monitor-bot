'use strict';

// R-PUBLIC-BASKET-SPAM-NUCLEAR — emitAlerts now consults basketDedup (SHA-256
// persistent) AND wallet debounce (in-memory Map) BEFORE shouldSendAlert.
// Use an isolated dedup DB so the default `data/basket_dedup.json` doesn't
// poison the assertions when prior runs leave entries on disk.
const path = require('path');
process.env.DEDUP_DB_PATH = path.join(
  __dirname,
  '..',
  'data',
  '__test_openAlerts_main.json'
);
process.env.BASKET_DEDUP_ENABLED = 'true';

const test = require('node:test');
const assert = require('node:assert/strict');

const openAlerts = require('../src/openAlerts');
const {
  findNewPositions,
  classifyOpenEvent,
  formatBasketOpenAlert,
  formatIndividualOpenAlert,
  emitAlerts,
  BASKET_MIN_COUNT,
} = openAlerts;

const { _resetCachesForTests } = require('../src/closeAlerts');
const basketDedup = require('../src/basketDedup');
const lockout = require('../src/walletBasketLockout');

function _resetAllOpenAlertGates() {
  _resetCachesForTests();
  basketDedup._resetForTests();
  openAlerts._resetWalletDebounceForTests();
  // R-PUBLIC-BASKET-UNIFY — reset wallet-level absolute lockout so each
  // test starts with a clean slate. Tests that exercise downstream gates
  // (SHA-256 dedup, shouldSendAlert) explicitly release the lockout
  // between calls — see comments in those tests.
  lockout._resetForTests({ persist: false });
}

test('findNewPositions returns [] when both lists empty', () => {
  assert.deepEqual(findNewPositions([], []), []);
});

test('findNewPositions returns all current when snapshot empty', () => {
  const cur = [{ coin: 'BTC', dex: 'Hyperliquid' }, { coin: 'ETH', dex: 'Hyperliquid' }];
  assert.equal(findNewPositions(cur, []).length, 2);
});

test('findNewPositions matches by (coin, dex) key', () => {
  const prev = [{ coin: 'BTC', dex: 'Hyperliquid' }];
  const cur = [
    { coin: 'BTC', dex: 'Hyperliquid' }, // same — not new
    { coin: 'BTC', dex: 'Native' }, // different dex — new
    { coin: 'ETH', dex: 'Hyperliquid' }, // different coin — new
  ];
  const out = findNewPositions(cur, prev);
  assert.equal(out.length, 2);
});

test('classifyOpenEvent: empty → NONE', () => {
  assert.equal(classifyOpenEvent([]).type, 'NONE');
});

test('classifyOpenEvent: 2 positions → INDIVIDUAL_OPEN', () => {
  const pos = [
    { coin: 'BTC', size: 1, entryPrice: 100000 },
    { coin: 'ETH', size: -10, entryPrice: 3000 },
  ];
  assert.equal(classifyOpenEvent(pos).type, 'INDIVIDUAL_OPEN');
});

test(`classifyOpenEvent: ≥${BASKET_MIN_COUNT} → BASKET_OPEN`, () => {
  const pos = Array.from({ length: BASKET_MIN_COUNT }, (_, i) => ({
    coin: `C${i}`, size: 1, entryPrice: 1,
  }));
  assert.equal(classifyOpenEvent(pos).type, 'BASKET_OPEN');
});

test('formatBasketOpenAlert: renders English "NEW BASKET OPENED" header', () => {
  const positions = [
    { coin: 'BTC', size: -1, entryPrice: 100000, leverage: 5 },
    { coin: 'ETH', size: -10, entryPrice: 3000, leverage: 5 },
    { coin: 'SOL', size: -100, entryPrice: 200, leverage: 5 },
  ];
  const msg = formatBasketOpenAlert('Primary wallet', positions);
  assert.match(msg, /NEW BASKET OPENED/);
  assert.match(msg, /Total notional/);
  assert.match(msg, /Leverage/);
  assert.match(msg, /TWAP entry/);
});

test('formatIndividualOpenAlert: renders English "NEW POSITION OPENED" header', () => {
  const pos = { coin: 'BLUR', size: -100000, entryPrice: 0.0314, leverage: 4, side: 'SHORT' };
  const msg = formatIndividualOpenAlert('Primary wallet', pos);
  assert.match(msg, /NEW POSITION OPENED/);
  assert.match(msg, /BLUR SHORT/);
  assert.match(msg, /Entry/);
  assert.match(msg, /Leverage/);
});

test('emitAlerts: BASKET_OPEN dispatches single message via dedupe', async () => {
  _resetAllOpenAlertGates();
  const sent = [];
  const positions = [
    { coin: 'A', size: 1, entryPrice: 1 },
    { coin: 'B', size: 1, entryPrice: 1 },
    { coin: 'C', size: 1, entryPrice: 1 },
  ];
  const r = await emitAlerts({
    chatId: 'cid', wallet: '0xabc', label: 'W',
    newPositions: positions,
    notify: async (cid, msg) => { sent.push({ cid, msg }); },
  });
  assert.equal(r.dispatched, 1);
  assert.equal(r.type, 'BASKET_OPEN');
  assert.equal(sent.length, 1);
});

test('emitAlerts: BASKET_OPEN second call within dedupe window suppressed', async () => {
  _resetAllOpenAlertGates();
  const sent = [];
  const positions = [
    { coin: 'A', size: 1, entryPrice: 1 },
    { coin: 'B', size: 1, entryPrice: 1 },
    { coin: 'C', size: 1, entryPrice: 1 },
  ];
  await emitAlerts({
    chatId: 'cid', wallet: '0xabc', label: 'W', newPositions: positions,
    notify: async (cid, msg) => { sent.push(msg); },
  });
  // Second call: identical basket. With R-PUBLIC-BASKET-UNIFY the
  // wallet-level absolute lockout (Gate-0) fires first → returns
  // BASKET_OPEN_WALLET_LOCKED. To verify the downstream SHA-256 dedup
  // gate still works in isolation, release the lockout + clear the
  // in-memory debounce, then expect BASKET_OPEN_DEDUPED.
  openAlerts._resetWalletDebounceForTests();
  lockout.release('0xabc');
  const r2 = await emitAlerts({
    chatId: 'cid', wallet: '0xabc', label: 'W', newPositions: positions,
    notify: async (cid, msg) => { sent.push(msg); },
  });
  assert.equal(sent.length, 1);
  assert.equal(r2.type, 'BASKET_OPEN_DEDUPED');
});
