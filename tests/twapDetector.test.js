'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const twap = require('../src/twapDetector');

const WALLET = '0xc7AE000000000000000000000000000000001505';

test.beforeEach(() => {
  twap._resetForTests();
  process.env.TWAP_AWARENESS_ENABLED = 'true';
});

test('isTWAPActive returns false on cold start', () => {
  assert.equal(twap.isTWAPActive(WALLET), false);
});

test('markTWAPStarted flips isTWAPActive to true', () => {
  twap.markTWAPStarted(WALLET);
  assert.equal(twap.isTWAPActive(WALLET), true);
});

test('markTWAPCompleted flips isTWAPActive back to false', () => {
  twap.markTWAPStarted(WALLET);
  assert.equal(twap.isTWAPActive(WALLET), true);
  twap.markTWAPCompleted(WALLET);
  assert.equal(twap.isTWAPActive(WALLET), false);
});

test('recordOpenEvent triggers TWAP after 3 distinct coins in window', () => {
  twap.recordOpenEvent(WALLET, 'BLUR');
  assert.equal(twap.isTWAPActive(WALLET), false);
  twap.recordOpenEvent(WALLET, 'STRK');
  assert.equal(twap.isTWAPActive(WALLET), false);
  twap.recordOpenEvent(WALLET, 'WLD');
  assert.equal(twap.isTWAPActive(WALLET), true, '3rd distinct coin triggers TWAP');
});

test('recordOpenEvent does NOT trigger when same coin repeats', () => {
  twap.recordOpenEvent(WALLET, 'BLUR');
  twap.recordOpenEvent(WALLET, 'BLUR');
  twap.recordOpenEvent(WALLET, 'BLUR');
  assert.equal(twap.isTWAPActive(WALLET), false);
});

test('recordOpenEvent is wallet-scoped (no cross-wallet contamination)', () => {
  const otherWallet = '0xdeadbeef0000000000000000000000000000beef';
  twap.recordOpenEvent(WALLET, 'BLUR');
  twap.recordOpenEvent(WALLET, 'STRK');
  twap.recordOpenEvent(otherWallet, 'WLD');
  assert.equal(twap.isTWAPActive(WALLET), false);
  assert.equal(twap.isTWAPActive(otherWallet), false);
});

test('detectTWAPFromFills marks TWAP when 3+ distinct coins inside window', () => {
  const now = Date.now();
  twap.detectTWAPFromFills(
    [
      { wallet: WALLET, coin: 'BLUR', timestamp: now },
      { wallet: WALLET, coin: 'STRK', timestamp: now },
      { wallet: WALLET, coin: 'WLD', timestamp: now },
    ],
    5
  );
  assert.equal(twap.isTWAPActive(WALLET), true);
});

test('detectTWAPFromFills ignores fills outside the window', () => {
  const old = Date.now() - 60 * 60 * 1000; // 1h ago
  twap.detectTWAPFromFills(
    [
      { wallet: WALLET, coin: 'BLUR', timestamp: old },
      { wallet: WALLET, coin: 'STRK', timestamp: old },
      { wallet: WALLET, coin: 'WLD', timestamp: old },
    ],
    5
  );
  assert.equal(twap.isTWAPActive(WALLET), false);
});

test('isEnabled honours kill switch', () => {
  process.env.TWAP_AWARENESS_ENABLED = 'false';
  assert.equal(twap.isEnabled(), false);
  twap.markTWAPStarted(WALLET);
  assert.equal(twap.isTWAPActive(WALLET), false, 'kill switch suppresses isTWAPActive');
  process.env.TWAP_AWARENESS_ENABLED = 'true';
});

test('getTWAPInfo returns metadata for active TWAPs', () => {
  twap.markTWAPStarted(WALLET, 'basket-v6');
  const info = twap.getTWAPInfo(WALLET);
  assert.ok(info, 'info present');
  assert.equal(info.basketId, 'basket-v6');
  assert.ok(info.expiresAt > info.startedAt);
});
