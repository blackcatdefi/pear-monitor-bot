'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const cg = require('../src/compoundingGate');
const twap = require('../src/twapDetector');

const WALLET = '0xc7AE000000000000000000000000000000001505';

const POSITIONS_V6 = [
  { coin: 'BLUR', side: 'SHORT', size: -1000, markPrice: 5 },
  { coin: 'STRK', side: 'SHORT', size: -2000, markPrice: 1.2 },
  { coin: 'WLD', side: 'SHORT', size: -300, markPrice: 4 },
  { coin: 'AVAX', side: 'SHORT', size: -50, markPrice: 30 },
  { coin: 'ENA', side: 'SHORT', size: -3000, markPrice: 0.8 },
  { coin: 'ZRO', side: 'SHORT', size: -500, markPrice: 4.5 },
];

test.beforeEach(() => {
  twap._resetForTests();
  cg._resetForTests();
  process.env.COMPOUNDING_GATE_ENABLED = 'true';
  process.env.TWAP_AWARENESS_ENABLED = 'true';
  process.env.COMPOUNDING_GROWTH_THRESHOLD = '0.10';
});

test('first sighting takes snapshot, no compounding fired', () => {
  const r = cg.detectCompounding(WALLET, POSITIONS_V6, 20227, 5642);
  assert.equal(r.isCompounding, false);
  assert.equal(r.reason, 'NO_PREV_SNAPSHOT');
});

test('BLOCKS during active TWAP (apr30 false-positive scenario)', () => {
  cg.takeSnapshot(WALLET, POSITIONS_V6, 20227, 5642);
  twap.markTWAPStarted(WALLET);
  const r = cg.detectCompounding(WALLET, POSITIONS_V6, 22454, 5658);
  assert.equal(r.isCompounding, false);
  assert.equal(r.reason, 'TWAP_ACTIVE');
});

test('BLOCKS during post-TWAP cooldown', () => {
  cg.takeSnapshot(WALLET, POSITIONS_V6, 20227, 5642);
  cg.markTWAPCompletedAt(WALLET);
  // No TWAP active anymore, but cooldown still in effect
  const r = cg.detectCompounding(WALLET, POSITIONS_V6, 22454, 5658);
  assert.equal(r.isCompounding, false);
  assert.equal(r.reason, 'POST_TWAP_COOLDOWN');
});

test('BLOCKS when positions composition changed', () => {
  cg.takeSnapshot(WALLET, POSITIONS_V6, 20227, 5642);
  const newPositions = [...POSITIONS_V6, { coin: 'NEW', side: 'LONG', size: 1, markPrice: 100 }];
  const r = cg.detectCompounding(WALLET, newPositions, 22454, 5658);
  assert.equal(r.isCompounding, false);
  assert.equal(r.reason, 'POSITIONS_CHANGED');
});

test('BLOCKS when notional growth below 10% threshold', () => {
  cg.takeSnapshot(WALLET, POSITIONS_V6, 20000, 5000);
  const r = cg.detectCompounding(WALLET, POSITIONS_V6, 21000, 5500); // +5%
  assert.equal(r.isCompounding, false);
  assert.equal(r.reason, 'BELOW_THRESHOLD');
});

test('BLOCKS when notional grew but account value stayed flat', () => {
  cg.takeSnapshot(WALLET, POSITIONS_V6, 20000, 5000);
  // notional +12%, account +1% → margin shifted, not capital added
  const r = cg.detectCompounding(WALLET, POSITIONS_V6, 22500, 5050);
  assert.equal(r.isCompounding, false);
  assert.equal(r.reason, 'ACCOUNT_VALUE_NOT_GROWN');
});

test('FIRES on real compounding event (no TWAP, both notional & account grew)', () => {
  cg.takeSnapshot(WALLET, POSITIONS_V6, 20000, 5000);
  const r = cg.detectCompounding(WALLET, POSITIONS_V6, 22500, 5600); // +12.5% notional, +12% acct
  assert.equal(r.isCompounding, true);
  assert.equal(r.reason, 'OK');
  assert.ok(r.growthPct >= 10);
});

test('positionsToKey is order-independent and side-aware', () => {
  const k1 = cg.positionsToKey([
    { coin: 'BLUR', side: 'SHORT' },
    { coin: 'STRK', side: 'SHORT' },
  ]);
  const k2 = cg.positionsToKey([
    { coin: 'STRK', side: 'SHORT' },
    { coin: 'BLUR', side: 'SHORT' },
  ]);
  assert.equal(k1, k2);
  const k3 = cg.positionsToKey([
    { coin: 'BLUR', side: 'LONG' },
    { coin: 'STRK', side: 'SHORT' },
  ]);
  assert.notEqual(k1, k3);
});
