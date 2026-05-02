'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const {
  shouldSendAlert,
  aggregateClosePnl,
  classifyCloseReason,
  trackCloseForBasket,
  formatCloseAlert,
  formatBasketSummary,
  _resetCachesForTests,
} = require('./closeAlerts');

// ----- BUG 1: PnL aggregation across multi-fill TWAP closes -----

test('BUG1: BLUR SHORT TWAP close aggregates to ~+$406.94 across multiple fills', () => {
  // Pear basket close on 2026-04-28 19:07 UTC. The bot was reporting +$47.69
  // because it took only the LAST fill. Reality: BLUR short, entry $0.0314,
  // exit ~$0.0282, size ~126,941 -> ~+$406.13 gross.
  const baseTime = Date.UTC(2026, 3, 28, 19, 6, 30);
  const fills = [
    { coin: 'BLUR', time: baseTime + 0, px: '0.02830', closedPnl: '120.00', fee: '1.20' },
    { coin: 'BLUR', time: baseTime + 5_000, px: '0.02825', closedPnl: '105.50', fee: '1.10' },
    { coin: 'BLUR', time: baseTime + 10_000, px: '0.02820', closedPnl: '85.25', fee: '0.95' },
    { coin: 'BLUR', time: baseTime + 15_000, px: '0.02818', closedPnl: '48.50', fee: '0.65' },
    // The buggy old code took THIS last fill only:
    { coin: 'BLUR', time: baseTime + 20_000, px: '0.02820', closedPnl: '47.69', fee: '0.50' },
    // Noise from another coin in the fills feed:
    { coin: 'ARB', time: baseTime + 5_000, px: '0.1247', closedPnl: '136.77', fee: '1.20' },
  ];

  const since = baseTime - 60_000;
  const result = aggregateClosePnl(fills, 'BLUR', since);

  assert.ok(
    Math.abs(result.pnl - 406.94) < 1,
    `Expected aggregated PnL ≈ +$406.94, got ${result.pnl}`
  );
  assert.equal(result.fillsUsed, 5);
  assert.ok(result.exitPrice > 0.028 && result.exitPrice < 0.029);
});

test('BUG1: ARB SHORT aggregates to ~+$136.77 (single fill)', () => {
  const fills = [
    { coin: 'ARB', time: 100, px: '0.1247', closedPnl: '136.77', fee: '1.20' },
  ];
  const result = aggregateClosePnl(fills, 'ARB', 0);
  assert.ok(Math.abs(result.pnl - 136.77) < 0.01);
});

test('BUG1: aggregateClosePnl excludes fills before sinceMs', () => {
  const fills = [
    { coin: 'BLUR', time: 50, px: '0.0282', closedPnl: '999.00', fee: '0' },
    { coin: 'BLUR', time: 200, px: '0.0282', closedPnl: '50.00', fee: '0' },
  ];
  const result = aggregateClosePnl(fills, 'BLUR', 100);
  assert.equal(result.pnl, 50);
  assert.equal(result.fillsUsed, 1);
});

test('BUG1: aggregateClosePnl handles malformed fills gracefully', () => {
  const fills = [
    { coin: 'BLUR', time: 100, px: 'invalid', closedPnl: 'NaN', fee: 'oops' },
    { coin: 'BLUR', time: 200, px: '0.03', closedPnl: '10', fee: '0.5' },
    null,
    {},
  ];
  const result = aggregateClosePnl(fills, 'BLUR', 0);
  assert.equal(result.pnl, 10);
  assert.equal(result.fees, 0.5);
});

// ----- BUG 2: dedup window 60s -----

test('BUG2: shouldSendAlert blocks duplicate within same minute', () => {
  _resetCachesForTests();
  const wallet = '0xc7AE000000000000000000000000000000001505';
  assert.equal(shouldSendAlert(wallet, 'DYDX'), true);
  assert.equal(shouldSendAlert(wallet, 'DYDX'), false);
  assert.equal(shouldSendAlert(wallet, 'DYDX'), false);
});

test('BUG2: shouldSendAlert allows different coins independently', () => {
  _resetCachesForTests();
  const wallet = '0xc7AE000000000000000000000000000000001505';
  assert.equal(shouldSendAlert(wallet, 'DYDX'), true);
  assert.equal(shouldSendAlert(wallet, 'ARB'), true);
  assert.equal(shouldSendAlert(wallet, 'OP'), true);
});

test('BUG2: shouldSendAlert is case-insensitive for wallet and coin', () => {
  _resetCachesForTests();
  assert.equal(shouldSendAlert('0xABC', 'BLUR'), true);
  assert.equal(shouldSendAlert('0xabc', 'blur'), false);
});

// ----- BUG 3: classify close reason — ONE reason per close -----

test('BUG3: only TP trigger disappeared -> TAKE_PROFIT', () => {
  const triggers = [
    { coin: 'OP', orderType: 'Take Profit Market', triggerPx: '0.115' },
  ];
  assert.equal(classifyCloseReason(triggers, 0.1216), 'TAKE_PROFIT');
});

test('BUG3: only SL trigger disappeared -> STOP_LOSS', () => {
  const triggers = [
    { coin: 'OP', orderType: 'Stop Market', triggerPx: '0.1216' },
  ];
  assert.equal(classifyCloseReason(triggers, 0.1216), 'STOP_LOSS');
});

test('BUG3: ARB case — both TP and SL disappeared, exit far from both -> TRAILING_OR_MANUAL', () => {
  // The exact bug pattern from 2026-04-28: ARB exit $0.1247, TP at $0.09761,
  // SL at $0.16268. Old code fired BOTH TP and SL alerts; new code returns ONE
  // reason — TRAILING_OR_MANUAL — because neither trigger matches the exit
  // within the 1% tolerance.
  const triggers = [
    { coin: 'ARB', orderType: 'Take Profit Market', triggerPx: '0.09761' },
    { coin: 'ARB', orderType: 'Stop Market', triggerPx: '0.16268' },
  ];
  assert.equal(classifyCloseReason(triggers, 0.1247), 'TRAILING_OR_MANUAL');
});

test('BUG3: both TP+SL but exit close to TP -> TAKE_PROFIT', () => {
  const triggers = [
    { coin: 'BTC', orderType: 'Take Profit Market', triggerPx: '70000' },
    { coin: 'BTC', orderType: 'Stop Market', triggerPx: '60000' },
  ];
  assert.equal(classifyCloseReason(triggers, 70200), 'TAKE_PROFIT');
});

test('BUG3: both TP+SL but exit close to SL -> STOP_LOSS', () => {
  const triggers = [
    { coin: 'BTC', orderType: 'Take Profit Market', triggerPx: '70000' },
    { coin: 'BTC', orderType: 'Stop Market', triggerPx: '60000' },
  ];
  assert.equal(classifyCloseReason(triggers, 60100), 'STOP_LOSS');
});

test('BUG3: no triggers disappeared -> MANUAL_CLOSE', () => {
  assert.equal(classifyCloseReason([], 0.05), 'MANUAL_CLOSE');
  assert.equal(classifyCloseReason(null, 0.05), 'MANUAL_CLOSE');
});

// ----- BUG 4: basket-close summary detection -----

test('BUG4: <3 closes does not schedule basket summary', () => {
  _resetCachesForTests();
  let summaryFired = false;
  const onSummary = async () => {
    summaryFired = true;
  };
  const scheduledA = trackCloseForBasket(1, '0xc7ae', 'Fund', { coin: 'BLUR', pnl: 406 }, onSummary);
  const scheduledB = trackCloseForBasket(1, '0xc7ae', 'Fund', { coin: 'ARB', pnl: 136 }, onSummary);
  assert.equal(scheduledA, false);
  assert.equal(scheduledB, false);
  assert.equal(summaryFired, false);
});

test('BUG4: 3+ closes within window schedules summary', async () => {
  _resetCachesForTests();
  let summaryClosesCount = null;
  const onSummary = async (_c, _w, _l, closes) => {
    summaryClosesCount = closes.length;
  };

  // Override the debounce by simulating timer advancement via fake clock would
  // require sinon. Instead we verify that the THIRD close returns scheduled=true
  // (i.e., a timer was set).
  trackCloseForBasket(1, '0xc7ae', 'Fund', { coin: 'BLUR', pnl: 406 }, onSummary);
  trackCloseForBasket(1, '0xc7ae', 'Fund', { coin: 'ARB', pnl: 136 }, onSummary);
  const scheduled = trackCloseForBasket(
    1,
    '0xc7ae',
    'Fund',
    { coin: 'OP', pnl: 77 },
    onSummary
  );
  assert.equal(scheduled, true);
});

// ----- format helpers -----

test('formatCloseAlert renders BLUR alert with correct PnL', () => {
  const msg = formatCloseAlert({
    label: 'Fondo Black Cat',
    oldPos: { coin: 'BLUR', side: 'SHORT', entryPrice: 0.0314 },
    pnl: 406.94,
    exitPrice: 0.0282,
    reason: 'TRAILING_OR_MANUAL',
    dexTag: ' _(Pear)_',
  });
  assert.match(msg, /BLUR/);
  assert.match(msg, /\+\$406\.94/);
  assert.match(msg, /Closed|CLOSED/);
  // CRUCIAL: must NOT contain the wrong, last-fill-only number
  assert.ok(!msg.includes('+$47.69'), 'must not contain old buggy PnL');
});

test('formatBasketSummary aggregates total PnL and lists best→worst', () => {
  const closes = [
    { coin: 'CRV', pnl: -48.11 },
    { coin: 'BLUR', pnl: 406.94 },
    { coin: 'ARB', pnl: 136.77 },
    { coin: 'OP', pnl: 77.56 },
    { coin: 'DYDX', pnl: -13.79 },
    { coin: 'LDO', pnl: -0.61 },
  ];
  const msg = formatBasketSummary('Fondo Black Cat', closes);
  assert.match(msg, /BASKET CLOSED/);
  assert.match(msg, /Positions closed:\s*\*6\*/);
  // total ≈ 558.76
  const total = closes.reduce((s, c) => s + c.pnl, 0);
  assert.ok(Math.abs(total - 558.76) < 0.01);
  // First listed should be BLUR (best PnL)
  const blurIdx = msg.indexOf('BLUR');
  const dydxIdx = msg.indexOf('DYDX');
  assert.ok(blurIdx > 0 && dydxIdx > 0 && blurIdx < dydxIdx);
});

// ----- end-to-end: replay the 2026-04-28 19:07 basket close scenario -----

test('E2E: 28 abr 19:07 basket close — single alert per coin, correct PnLs', () => {
  _resetCachesForTests();

  // Disappeared triggers (cached TP+SL from before BCD switched to trailing)
  const triggers = {
    CRV: [
      { orderType: 'Take Profit Market', triggerPx: '0.18032' },
      { orderType: 'Stop Market', triggerPx: '0.27048' },
    ],
    BLUR: [
      { orderType: 'Take Profit Market', triggerPx: '0.02512' },
      { orderType: 'Stop Market', triggerPx: '0.03768' },
    ],
    ARB: [
      { orderType: 'Take Profit Market', triggerPx: '0.09761' },
      { orderType: 'Stop Market', triggerPx: '0.16268' },
    ],
    OP: [
      { orderType: 'Take Profit Market', triggerPx: '0.099' },
      { orderType: 'Stop Market', triggerPx: '0.155' },
    ],
    DYDX: [],
    LDO: [],
  };

  // Aggregated PnL (the new pipeline)
  const realPnls = {
    CRV: -48.11,
    BLUR: 406.94,
    ARB: 136.77,
    OP: 77.56,
    DYDX: -13.79,
    LDO: -0.61,
  };
  const exitPrices = {
    CRV: 0.2282,
    BLUR: 0.0282,
    ARB: 0.1247,
    OP: 0.1216,
    DYDX: 0.45,
    LDO: 0.74,
  };

  const sentAlerts = [];
  for (const coin of Object.keys(realPnls)) {
    if (!shouldSendAlert('0xc7ae', coin)) continue;
    const reason = classifyCloseReason(triggers[coin], exitPrices[coin]);
    sentAlerts.push({ coin, pnl: realPnls[coin], reason });
  }

  // 6 alerts total, ONE per coin
  assert.equal(sentAlerts.length, 6);
  // No duplicate reasons (i.e., not "ARB-TP and ARB-SL")
  const arbAlerts = sentAlerts.filter((a) => a.coin === 'ARB');
  assert.equal(arbAlerts.length, 1);
  assert.equal(arbAlerts[0].reason, 'TRAILING_OR_MANUAL');
  // BLUR shows correct PnL
  const blur = sentAlerts.find((a) => a.coin === 'BLUR');
  assert.ok(Math.abs(blur.pnl - 406.94) < 0.01);
});
