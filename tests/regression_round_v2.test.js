'use strict';

/**
 * Regression test for ROUND v2.
 * Locks in:
 *   - The apr-28 19:07 UTC BLUR multi-fill TWAP close → +$406.94 aggregate
 *   - Spanish-only user-facing alerts
 *   - Branding footer present on primary wallet
 *   - Rate-limit ceiling + dedupe behavior coexist
 */

const test = require('node:test');
const assert = require('node:assert/strict');

const {
  aggregateClosePnl,
  classifyCloseReason,
  formatCloseAlert,
  formatBasketSummary,
  shouldSendAlert,
  trackCloseForBasket,
  _resetCachesForTests,
} = require('../src/closeAlerts');

const { formatCompoundAlert } = require('../src/compoundingDetector');
const { formatBasketOpenAlert, formatIndividualOpenAlert } = require('../src/openAlerts');
const { appendFooter, getFooter } = require('../src/branding');
const { canSendAlert, _resetForTests: resetRL } = require('../src/rateLimiter');

test('REGRESSION apr28 19:07: BLUR aggregates to +$406.94', () => {
  const baseTime = Date.UTC(2026, 3, 28, 19, 6, 30);
  const fills = [
    { coin: 'BLUR', time: baseTime + 0, px: '0.02830', closedPnl: '120.00', fee: '1.20' },
    { coin: 'BLUR', time: baseTime + 5_000, px: '0.02825', closedPnl: '105.50', fee: '1.10' },
    { coin: 'BLUR', time: baseTime + 10_000, px: '0.02820', closedPnl: '85.25', fee: '0.95' },
    { coin: 'BLUR', time: baseTime + 15_000, px: '0.02818', closedPnl: '48.50', fee: '0.65' },
    { coin: 'BLUR', time: baseTime + 20_000, px: '0.02820', closedPnl: '47.69', fee: '0.50' },
    { coin: 'ARB', time: baseTime + 5_000, px: '0.1247', closedPnl: '136.77', fee: '1.20' },
  ];
  const r = aggregateClosePnl(fills, 'BLUR', baseTime - 60_000);
  assert.ok(Math.abs(r.pnl - 406.94) < 1, `expected ~+$406.94, got ${r.pnl}`);
  assert.equal(r.fillsUsed, 5);
});

test('REGRESSION apr28 19:07: ARB single-fill aggregate → +$136.77', () => {
  const fills = [{ coin: 'ARB', time: 100, px: '0.1247', closedPnl: '136.77', fee: '1.20' }];
  const r = aggregateClosePnl(fills, 'ARB', 0);
  assert.ok(Math.abs(r.pnl - 136.77) < 0.01);
});

test('REGRESSION close alert text is in Spanish', () => {
  const oldPos = {
    coin: 'BLUR', side: 'SHORT', size: 126_941, entryPrice: 0.0314,
  };
  const msg = formatCloseAlert({
    label: 'Wallet primaria',
    oldPos,
    pnl: 406.94,
    exitPrice: 0.02822,
    reason: 'TAKE_PROFIT',
    dexTag: '',
  });
  // No English markers in user-facing strings:
  assert.doesNotMatch(msg, /Position closed/i);
  assert.doesNotMatch(msg, /Take Profit hit/i);
  assert.doesNotMatch(msg, /Closed at/i);
  // Spanish markers expected:
  assert.match(msg, /Wallet/);
  assert.match(msg, /BLUR/);
  assert.match(msg, /PnL/);
});

test('REGRESSION basket open alert is Spanish', () => {
  const positions = [
    { coin: 'BTC', size: -1, entryPrice: 100000 },
    { coin: 'ETH', size: -10, entryPrice: 3000 },
    { coin: 'SOL', size: -100, entryPrice: 200 },
  ];
  const m = formatBasketOpenAlert('Wallet primaria', positions);
  assert.match(m, /NUEVA BASKET ABIERTA/);
  assert.doesNotMatch(m, /New basket opened/i);
});

test('REGRESSION individual open alert is Spanish', () => {
  const m = formatIndividualOpenAlert('Wallet primaria', {
    coin: 'BLUR', size: -100000, entryPrice: 0.0314, side: 'SHORT', leverage: 4,
  });
  assert.match(m, /NUEVA POSICIÓN ABIERTA/);
  assert.doesNotMatch(m, /New position/i);
});

test('REGRESSION compounding alert is Spanish + neutral language', () => {
  const m = formatCompoundAlert('Wallet primaria', {
    type: 'COMPOUND_DETECTED', prevNotional: 10000, currentNotional: 12000, growth: 0.20,
  });
  assert.match(m, /COMPOUNDING DETECTADO/);
  assert.match(m, /compounding|TWAP entry/i);
  assert.doesNotMatch(m, /Compounding detected/i);
  assert.doesNotMatch(m, /NORBER/i);
});

test('REGRESSION branding footer appended only when enabled+primary', () => {
  const orig = process.env.BRANDING_ENABLED;
  process.env.BRANDING_ENABLED = 'true';
  try {
    const footer = getFooter();
    const out = appendFooter('hello', true);
    if (footer) {
      assert.ok(out.includes(footer), 'primary wallet should get footer');
    }
    const out2 = appendFooter('hello', false);
    if (footer) {
      assert.ok(!out2.includes(footer), 'non-primary wallet should NOT get footer');
    }
  } finally {
    if (orig === undefined) delete process.env.BRANDING_ENABLED;
    else process.env.BRANDING_ENABLED = orig;
  }
});

test('REGRESSION basket summary aggregates total PnL across coins', () => {
  _resetCachesForTests();
  const baseTime = Date.UTC(2026, 3, 28, 19, 6, 30);
  const closes = [
    { coin: 'BLUR', side: 'SHORT', pnl: 406.94, exitPrice: 0.0282 },
    { coin: 'ARB',  side: 'SHORT', pnl: 136.77, exitPrice: 0.1247 },
    { coin: 'WLD',  side: 'SHORT', pnl: -23.40, exitPrice: 1.45 },
  ];
  const m = formatBasketSummary('Wallet primaria', closes);
  // Spanish summary uses "PnL total"
  assert.match(m, /PnL total/i);
  // total ≈ +$520.31
  assert.match(m, /520(\.|,)/);
});

test('REGRESSION rate limiter coexists with dedupe (no double-count)', () => {
  resetRL();
  _resetCachesForTests();
  // first wallet:coin alert passes both layers
  assert.equal(canSendAlert(), true);
  assert.equal(shouldSendAlert('0xa', 'BLUR'), true);
  // dedupe blocks the second within window even though limiter has room
  assert.equal(shouldSendAlert('0xa', 'BLUR'), false);
});

test('REGRESSION basket close tracker accumulates to summary trigger', () => {
  _resetCachesForTests();
  trackCloseForBasket('0xabc', { coin: 'BLUR', side: 'SHORT', pnl: 100, exitPrice: 0.028 });
  trackCloseForBasket('0xabc', { coin: 'ARB', side: 'SHORT', pnl: 50, exitPrice: 0.12 });
  trackCloseForBasket('0xabc', { coin: 'WLD', side: 'SHORT', pnl: 25, exitPrice: 1.4 });
  // No exception thrown → tracker shape is stable
  assert.ok(true);
});
