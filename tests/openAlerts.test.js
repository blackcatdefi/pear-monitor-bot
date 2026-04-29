'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const {
  findNewPositions,
  classifyOpenEvent,
  formatBasketOpenAlert,
  formatIndividualOpenAlert,
  emitAlerts,
  BASKET_MIN_COUNT,
} = require('../src/openAlerts');

const { _resetCachesForTests } = require('../src/closeAlerts');

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

test('formatBasketOpenAlert: includes Spanish "NUEVA BASKET ABIERTA"', () => {
  const positions = [
    { coin: 'BTC', size: -1, entryPrice: 100000, leverage: 5 },
    { coin: 'ETH', size: -10, entryPrice: 3000, leverage: 5 },
    { coin: 'SOL', size: -100, entryPrice: 200, leverage: 5 },
  ];
  const msg = formatBasketOpenAlert('Wallet primaria', positions);
  assert.match(msg, /NUEVA BASKET ABIERTA/);
  assert.match(msg, /Notional total/);
  assert.match(msg, /Leverage/);
  assert.match(msg, /NORBER WAY/);
});

test('formatIndividualOpenAlert: includes Spanish "NUEVA POSICIÓN ABIERTA"', () => {
  const pos = { coin: 'BLUR', size: -100000, entryPrice: 0.0314, leverage: 4, side: 'SHORT' };
  const msg = formatIndividualOpenAlert('Wallet primaria', pos);
  assert.match(msg, /NUEVA POSICIÓN ABIERTA/);
  assert.match(msg, /BLUR SHORT/);
  assert.match(msg, /Entry/);
  assert.match(msg, /Leverage/);
});

test('emitAlerts: BASKET_OPEN dispatches single message via dedupe', async () => {
  _resetCachesForTests();
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
  _resetCachesForTests();
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
  const r2 = await emitAlerts({
    chatId: 'cid', wallet: '0xabc', label: 'W', newPositions: positions,
    notify: async (cid, msg) => { sent.push(msg); },
  });
  assert.equal(sent.length, 1);
  assert.equal(r2.type, 'BASKET_OPEN_DEDUPED');
});
