'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const {
  isEnabled,
  validatePnlBeforeAlert,
  ABS_THRESHOLD_USD,
  PCT_THRESHOLD,
  DIFF_TOLERANCE,
  _isSuspicious,
} = require('../src/pnlCrossValidation');

test('_isSuspicious: |pnl|>$1000 → true', () => {
  assert.equal(_isSuspicious(1500, 50000), true);
});

test('_isSuspicious: |pnl|/notional>50% → true', () => {
  assert.equal(_isSuspicious(800, 1000), true); // 80%
});

test('_isSuspicious: small pnl + small ratio → false', () => {
  assert.equal(_isSuspicious(50, 10000), false);
});

test('_isSuspicious: NaN → false', () => {
  assert.equal(_isSuspicious(NaN, 10000), false);
});

test('validatePnlBeforeAlert: not suspicious → returns bot value untouched', async () => {
  const r = await validatePnlBeforeAlert({
    wallet: '0xa', coin: 'BTC', calculatedPnl: 50, notional: 10000,
  });
  assert.equal(r.valid, true);
  assert.equal(r.pnl, 50);
  assert.equal(r.source, 'bot');
});

test('validatePnlBeforeAlert: disabled flag bypasses entirely', async () => {
  const orig = process.env.PNL_CROSS_VALIDATION_ENABLED;
  process.env.PNL_CROSS_VALIDATION_ENABLED = 'false';
  try {
    assert.equal(isEnabled(), false);
    const r = await validatePnlBeforeAlert({
      wallet: '0xa', coin: 'BTC', calculatedPnl: 999_999, notional: 1,
    });
    assert.equal(r.source, 'bot');
    assert.equal(r.pnl, 999_999);
  } finally {
    if (orig === undefined) delete process.env.PNL_CROSS_VALIDATION_ENABLED;
    else process.env.PNL_CROSS_VALIDATION_ENABLED = orig;
  }
});

test('validatePnlBeforeAlert: suspicious + Pear unreachable → flagged but valid', async () => {
  // Force unreachable Pear endpoint
  const origBase = process.env.PEAR_API_BASE;
  process.env.PEAR_API_BASE = 'http://127.0.0.1:1';
  try {
    const r = await validatePnlBeforeAlert({
      wallet: '0xa', coin: 'BTC', calculatedPnl: 5000, notional: 50000,
    });
    assert.equal(r.valid, true);
    assert.equal(r.source, 'bot');
    assert.equal(r.flagged, true);
    assert.match(r.note, /Pear API/);
  } finally {
    if (origBase === undefined) delete process.env.PEAR_API_BASE;
    else process.env.PEAR_API_BASE = origBase;
  }
});

test('Thresholds match env or defaults', () => {
  assert.ok(ABS_THRESHOLD_USD > 0);
  assert.ok(PCT_THRESHOLD > 0 && PCT_THRESHOLD < 1);
  assert.ok(DIFF_TOLERANCE > 0 && DIFF_TOLERANCE < 1);
});
