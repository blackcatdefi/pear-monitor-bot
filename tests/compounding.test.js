'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const {
  checkForCompounding,
  formatCompoundAlert,
  GROWTH_THRESHOLD,
  _resetForTests,
} = require('../src/compoundingDetector');

test('checkForCompounding: empty positions → NONE', () => {
  _resetForTests();
  assert.equal(checkForCompounding('cid', '0xa', []).type, 'NONE');
});

test('checkForCompounding: first call seeds snapshot, returns NONE', () => {
  _resetForTests();
  const r = checkForCompounding('cid', '0xa', [
    { coin: 'BTC', size: 1, markPrice: 100000, side: 'LONG' },
  ]);
  assert.equal(r.type, 'NONE');
});

test('checkForCompounding: same basket grows ≥10% → COMPOUND_DETECTED', () => {
  _resetForTests();
  const t1 = [
    { coin: 'BTC', size: 1, markPrice: 100000, side: 'LONG' },
    { coin: 'ETH', size: -10, markPrice: 3000, side: 'SHORT' },
  ];
  checkForCompounding('cid', '0xa', t1); // seed
  // 15% growth
  const t2 = [
    { coin: 'BTC', size: 1.15, markPrice: 100000, side: 'LONG' },
    { coin: 'ETH', size: -11.5, markPrice: 3000, side: 'SHORT' },
  ];
  const r = checkForCompounding('cid', '0xa', t2);
  assert.equal(r.type, 'COMPOUND_DETECTED');
  assert.ok(r.growth >= GROWTH_THRESHOLD);
});

test('checkForCompounding: small growth (<10%) → NONE', () => {
  _resetForTests();
  const t1 = [{ coin: 'BTC', size: 1, markPrice: 100000, side: 'LONG' }];
  checkForCompounding('cid', '0xa', t1);
  const t2 = [{ coin: 'BTC', size: 1.05, markPrice: 100000, side: 'LONG' }];
  assert.equal(checkForCompounding('cid', '0xa', t2).type, 'NONE');
});

test('checkForCompounding: different basket composition → NONE (snapshot reset)', () => {
  _resetForTests();
  checkForCompounding('cid', '0xa', [
    { coin: 'BTC', size: 1, markPrice: 100000, side: 'LONG' },
  ]);
  // different coin set — should reset, not fire
  const r = checkForCompounding('cid', '0xa', [
    { coin: 'ETH', size: 100, markPrice: 3000, side: 'LONG' }, // notional huge
  ]);
  assert.equal(r.type, 'NONE');
});

test('checkForCompounding: post-fire snapshot updates, no double fire', () => {
  _resetForTests();
  const t1 = [{ coin: 'BTC', size: 1, markPrice: 100000, side: 'LONG' }];
  checkForCompounding('cid', '0xa', t1);
  const t2 = [{ coin: 'BTC', size: 1.20, markPrice: 100000, side: 'LONG' }];
  assert.equal(checkForCompounding('cid', '0xa', t2).type, 'COMPOUND_DETECTED');
  // Same again immediately — no further growth — should NOT fire
  assert.equal(checkForCompounding('cid', '0xa', t2).type, 'NONE');
});

test('formatCompoundAlert: English output with growth %', () => {
  const msg = formatCompoundAlert('Primary wallet', {
    type: 'COMPOUND_DETECTED',
    prevNotional: 10000,
    currentNotional: 11500,
    growth: 0.15,
  });
  assert.match(msg, /COMPOUNDING DETECTED/i);
  assert.match(msg, /\+15\.0%/);
  assert.match(msg, /compounding|TWAP entry/i);
});
