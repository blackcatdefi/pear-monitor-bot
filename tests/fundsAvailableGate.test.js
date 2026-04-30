'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const gate = require('../src/fundsAvailableGate');
const twap = require('../src/twapDetector');

const WALLET = '0xc7AE000000000000000000000000000000001505';

test.beforeEach(() => {
  twap._resetForTests();
  gate._resetForTests();
  process.env.FUNDS_AVAILABLE_GATE_ENABLED = 'true';
  process.env.TWAP_AWARENESS_ENABLED = 'true';
});

test('BLOCKS when TWAP active', () => {
  twap.markTWAPStarted(WALLET);
  const result = gate.shouldFireFundsAvailable(WALLET, 5642);
  assert.equal(result.shouldFire, false);
  assert.equal(result.reason, 'TWAP_ACTIVE');
});

test('BLOCKS micro-amounts below $200 threshold', () => {
  const r1 = gate.shouldFireFundsAvailable(WALLET, 73.63);
  assert.equal(r1.shouldFire, false);
  assert.match(r1.reason, /^BELOW_THRESHOLD_/);
  const r2 = gate.shouldFireFundsAvailable(WALLET, 50.40);
  assert.equal(r2.shouldFire, false);
  assert.match(r2.reason, /^BELOW_THRESHOLD_/);
});

test('FIRES when no TWAP and amount >= $200', () => {
  const result = gate.shouldFireFundsAvailable(WALLET, 5642);
  assert.equal(result.shouldFire, true);
  assert.equal(result.reason, 'OK');
});

test('DEDUPES same wallet+bucket within 1h window', () => {
  const r1 = gate.shouldFireFundsAvailable(WALLET, 5642);
  assert.equal(r1.shouldFire, true);
  const r2 = gate.shouldFireFundsAvailable(WALLET, 5650);
  assert.equal(r2.shouldFire, false);
  assert.equal(r2.reason, 'RECENTLY_ALERTED');
});

test('does NOT dedupe different buckets ($100 granularity)', () => {
  const r1 = gate.shouldFireFundsAvailable(WALLET, 5642); // bucket 5600
  assert.equal(r1.shouldFire, true);
  const r2 = gate.shouldFireFundsAvailable(WALLET, 7100); // bucket 7100
  assert.equal(r2.shouldFire, true);
});

test('GATE_DISABLED bypass when env kill-switch set', () => {
  process.env.FUNDS_AVAILABLE_GATE_ENABLED = 'false';
  const result = gate.shouldFireFundsAvailable(WALLET, 50);
  assert.equal(result.shouldFire, true);
  assert.equal(result.reason, 'GATE_DISABLED');
  process.env.FUNDS_AVAILABLE_GATE_ENABLED = 'true';
});

test('REGRESSION apr30 12:15 UTC: 5 micro-spam values all suppressed', () => {
  // Exact values from BCD's screenshots
  twap.markTWAPStarted(WALLET);
  for (const amt of [73.63, 96.98, 50.40, 53.15, 58.94]) {
    const r = gate.shouldFireFundsAvailable(WALLET, amt);
    assert.equal(r.shouldFire, false, `amt=${amt} should be suppressed`);
    // Should be TWAP_ACTIVE (preferred) or BELOW_THRESHOLD; both are valid
    assert.ok(
      r.reason === 'TWAP_ACTIVE' || r.reason.startsWith('BELOW_THRESHOLD_'),
      `unexpected reason: ${r.reason}`
    );
  }
});
