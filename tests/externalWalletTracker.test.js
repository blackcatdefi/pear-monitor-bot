'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const tracker = require('../src/externalWalletTracker');

test.beforeEach(() => {
  tracker._resetForTests();
  process.env.EXTERNAL_WALLETS_ENABLED = 'true';
});

test('loadExternalWalletsFromEnv parses JSON array', () => {
  process.env.EXTERNAL_WALLETS_JSON = JSON.stringify([
    { address: '0x111', label: 'Whale 1' },
    { address: '0x222', label: 'Tom Lee BitMine' },
  ]);
  const loaded = tracker.loadExternalWalletsFromEnv();
  assert.equal(loaded.length, 2);
  assert.equal(loaded[0].label, 'Whale 1');
});

test('loadExternalWalletsFromEnv tolerates missing env var', () => {
  delete process.env.EXTERNAL_WALLETS_JSON;
  const loaded = tracker.loadExternalWalletsFromEnv();
  assert.equal(loaded.length, 0);
});

test('loadExternalWalletsFromEnv tolerates invalid JSON', () => {
  process.env.EXTERNAL_WALLETS_JSON = 'not-json{{';
  const loaded = tracker.loadExternalWalletsFromEnv();
  assert.equal(loaded.length, 0);
});

test('loadExternalWalletsFromEnv filters non-object entries', () => {
  process.env.EXTERNAL_WALLETS_JSON = JSON.stringify([
    { address: '0xabc', label: 'OK' },
    null,
    { label: 'no-address' },
    { address: '0xdef', label: 'OK2' },
  ]);
  const loaded = tracker.loadExternalWalletsFromEnv();
  assert.equal(loaded.length, 2);
});

test('_diffPositions detects opens (in current, not in previous)', () => {
  const prev = [{ coin: 'BTC', side: 'LONG' }];
  const curr = [
    { coin: 'BTC', side: 'LONG' },
    { coin: 'ETH', side: 'SHORT' },
  ];
  const { opens, closes } = tracker._diffPositions(prev, curr);
  assert.equal(opens.length, 1);
  assert.equal(opens[0].coin, 'ETH');
  assert.equal(closes.length, 0);
});

test('_diffPositions detects closes (in previous, not in current)', () => {
  const prev = [
    { coin: 'BTC', side: 'LONG' },
    { coin: 'ETH', side: 'SHORT' },
  ];
  const curr = [{ coin: 'BTC', side: 'LONG' }];
  const { opens, closes } = tracker._diffPositions(prev, curr);
  assert.equal(opens.length, 0);
  assert.equal(closes.length, 1);
  assert.equal(closes[0].coin, 'ETH');
});

test('_diffPositions treats side flips as both close + open', () => {
  const prev = [{ coin: 'BTC', side: 'LONG' }];
  const curr = [{ coin: 'BTC', side: 'SHORT' }];
  const { opens, closes } = tracker._diffPositions(prev, curr);
  assert.equal(opens.length, 1);
  assert.equal(opens[0].side, 'SHORT');
  assert.equal(closes.length, 1);
  assert.equal(closes[0].side, 'LONG');
});

test('formatExternalOpenAlert renders all fields', () => {
  const cfg = { address: '0x1234567890abcdef1234567890abcdef12345678', label: 'Whale 1' };
  const pos = { coin: 'BTC', side: 'LONG', entryPx: 65432.1, size: 0.5, notional: 32716, unrealizedPnl: 0 };
  const msg = tracker.formatExternalOpenAlert(cfg, pos);
  assert.match(msg, /EXTERNAL WALLET/);
  assert.match(msg, /NEW POSITION OPENED/);
  assert.match(msg, /Whale 1/);
  assert.match(msg, /BTC LONG/);
  assert.match(msg, /\$65432\.10/);
  assert.match(msg, /Intel/);
});

test('formatExternalCloseAlert renders pnl emoji + intel hint', () => {
  const cfg = { address: '0xabcdef1234567890abcdef1234567890abcdef12', label: 'Tom Lee' };
  const posWin = { coin: 'ETH', side: 'SHORT', entryPx: 3000, size: 1, notional: 3000, unrealizedPnl: 250 };
  const msgWin = tracker.formatExternalCloseAlert(cfg, posWin);
  assert.match(msgWin, /🟢/);
  assert.match(msgWin, /\+\$250\.00/);
  const posLose = { ...posWin, unrealizedPnl: -100 };
  const msgLose = tracker.formatExternalCloseAlert(cfg, posLose);
  assert.match(msgLose, /🔴/);
  assert.match(msgLose, /-\$100\.00/);
});

test('isEnabled honours kill switch', () => {
  process.env.EXTERNAL_WALLETS_ENABLED = 'false';
  assert.equal(tracker.isEnabled(), false);
  process.env.EXTERNAL_WALLETS_ENABLED = 'true';
  assert.equal(tracker.isEnabled(), true);
});

test('startSchedule with empty wallets does not crash', () => {
  process.env.EXTERNAL_WALLETS_JSON = '[]';
  tracker.loadExternalWalletsFromEnv();
  const handle = tracker.startSchedule({
    notify: async () => {},
    primaryChatId: '123',
  });
  // Should still return a timer (dormant) so future env reload picks up
  assert.ok(handle === null || typeof handle === 'object');
  tracker.stopSchedule();
});
