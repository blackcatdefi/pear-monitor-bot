'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const twap = require('../src/twapDetector');
const fag = require('../src/fundsAvailableGate');
const cg = require('../src/compoundingGate');

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
  fag._resetForTests();
  cg._resetForTests();
  process.env.TWAP_AWARENESS_ENABLED = 'true';
  process.env.FUNDS_AVAILABLE_GATE_ENABLED = 'true';
  process.env.COMPOUNDING_GATE_ENABLED = 'true';
});

/**
 * REGRESSION — Apr 30 2026 11:55–12:13 UTC.
 *
 * BCD's wallet 0xc7AE...1505 was mid-TWAP for v6 basket (started 29 abr
 * 21:45 UTC, 14h, 30 bullets). During the fill BCD got:
 *   1. 5 spam "Funds available to trade" alerts ($73, $96, $50, $53, $58)
 *   2. 1 false-positive "🔄 COMPOUNDING DETECTADO" ($20,227 → $22,454, +11%)
 *
 * R(v3) must suppress all 6 alerts.
 */
test('REGRESSION: 5 micro-spam funds-available alerts all suppressed during TWAP', () => {
  // Simulate basket open: 6 distinct coins detected → TWAP marked active
  for (const p of POSITIONS_V6) {
    twap.recordOpenEvent(WALLET, p.coin);
  }
  assert.equal(twap.isTWAPActive(WALLET), true, 'TWAP should be active after 6 distinct coins');

  // The exact 5 micro-amounts BCD saw between 11:58 and 12:13 UTC
  const noiseAmounts = [73.63, 96.98, 50.40, 53.15, 58.94];
  for (const amt of noiseAmounts) {
    const r = fag.shouldFireFundsAvailable(WALLET, amt);
    assert.equal(r.shouldFire, false, `amt=${amt} must be suppressed`);
  }
});

test('REGRESSION: compounding false-positive ($20,227 → $22,454) suppressed during TWAP', () => {
  // Trigger TWAP detection
  for (const p of POSITIONS_V6) twap.recordOpenEvent(WALLET, p.coin);
  assert.equal(twap.isTWAPActive(WALLET), true);

  // Snapshot at 11:50 (pre-spike)
  cg.takeSnapshot(WALLET, POSITIONS_V6, 20227, 5642);

  // Re-evaluate at 11:55 with notional grown — must be suppressed by gate
  const result = cg.detectCompounding(WALLET, POSITIONS_V6, 22454, 5658);
  assert.equal(result.isCompounding, false);
  assert.equal(result.reason, 'TWAP_ACTIVE');
});

test('REGRESSION: when TWAP completes, post-cooldown blocks immediate compounding', () => {
  for (const p of POSITIONS_V6) twap.recordOpenEvent(WALLET, p.coin);
  assert.equal(twap.isTWAPActive(WALLET), true);
  cg.takeSnapshot(WALLET, POSITIONS_V6, 20227, 5642);

  // TWAP completes (e.g. 30 bullets reached)
  twap.markTWAPCompleted(WALLET);
  cg.markTWAPCompletedAt(WALLET);

  // Even though TWAP no longer active, cooldown blocks for 30min
  const r = cg.detectCompounding(WALLET, POSITIONS_V6, 22454, 5658);
  assert.equal(r.isCompounding, false);
  assert.equal(r.reason, 'POST_TWAP_COOLDOWN');
});

test('REGRESSION: real funds-available (post-TWAP, > $200) DOES fire', () => {
  // No TWAP, no recent dedupe, $5K available → must fire
  const r = fag.shouldFireFundsAvailable(WALLET, 5642);
  assert.equal(r.shouldFire, true);
  assert.equal(r.reason, 'OK');
});

test('REGRESSION: real compounding (post-cooldown, capital actually added) fires', () => {
  // BCD 24h+ later: TWAP done, cooldown elapsed → snapshot baseline
  cg.takeSnapshot(WALLET, POSITIONS_V6, 20000, 5000);

  // BCD adds capital: notional +12.5%, account_value also +12%
  const r = cg.detectCompounding(WALLET, POSITIONS_V6, 22500, 5600);
  assert.equal(r.isCompounding, true);
  assert.equal(r.reason, 'OK');
  assert.ok(r.growthPct >= 10);
});
