'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const dc = require('../src/deployableCapital');

test.beforeEach(() => {
  delete process.env.FUNDS_PERP_SPOT_ADDITIVE;
  delete process.env.FUNDS_STABLE_SYMBOLS;
});

// The exact production snapshot: three stablecoins showing ~2,606 each are the
// SAME single withdrawable pool, not three balances. Must land on ~2,606.
const SPOT_2606 = [
  { coin: 'USDC', total: 2606.11, hold: 0 },
  { coin: 'USDT0', total: 2606.62, hold: 0 },
  { coin: 'USDH', total: 2606.13, hold: 0 },
  { coin: 'HYPE', total: 2279.2, hold: 1874.4 }, // non-stable, ignored
];

test('single pool = MAX across stables, NEVER the sum (2606 not 7818)', () => {
  const r = dc.computeSpotPool(SPOT_2606);
  assert.ok(Math.abs(r.pool - 2606.62) < 0.01, `pool=${r.pool}`);
  assert.ok(r.pool < 3000, 'pool must not approach the summed 7.8K');
  // sanity: the naive sum would have tripled it
  assert.ok(r.sumOfStables > 7800);
});

test('computeDeployable lands on ~2606 (not 535, not 212, not 7.8K)', () => {
  const r = dc.computeDeployable({
    spotBalances: SPOT_2606,
    perp: { accountValue: 17200, marginUsed: 16700, withdrawable: 535.04 },
  });
  assert.equal(r.error, false);
  assert.ok(Math.abs(r.totalDeployable - 2606.62) < 0.01, `total=${r.totalDeployable}`);
  assert.ok(r.totalDeployable > 2500 && r.totalDeployable < 2700);
});

test('perp free margin is NOT added by default (unified Portfolio Margin)', () => {
  const r = dc.computeDeployable({
    spotBalances: SPOT_2606,
    perp: { accountValue: 17200, marginUsed: 16700, withdrawable: 535.04 },
  });
  // 2606 only, NOT 2606+535=3141
  assert.ok(r.totalDeployable < 2700);
  assert.equal(r.additive, false);
});

test('additive override sums spot pool + perp free margin', () => {
  process.env.FUNDS_PERP_SPOT_ADDITIVE = 'true';
  const r = dc.computeDeployable({
    spotBalances: SPOT_2606,
    perp: { accountValue: 17200, marginUsed: 16700, withdrawable: 535.04 },
  });
  assert.equal(r.additive, true);
  assert.ok(Math.abs(r.totalDeployable - (2606.62 + 535.04)) < 0.01);
});

test('over-max-borrow flag when marginUsed >= accountValue', () => {
  // Live snapshot 2026-06-15: accountValue 8610 < marginUsed 8616, withdrawable 0
  const r = dc.computeDeployable({
    spotBalances: [
      { coin: 'USDC', total: -54300.66, hold: -67888.79 }, // drawn borrow
      { coin: 'USDT0', total: 0.0136, hold: -13595.74 },
      { coin: 'USDH', total: 0.0157, hold: -13588.13 },
    ],
    perp: { accountValue: 8610.93, marginUsed: 8616.77, withdrawable: 0 },
  });
  assert.equal(r.overMaxBorrow, true);
  assert.ok(r.borrowUtilizationPct >= 100);
  // negative stable totals clamp to 0 → no phantom dry powder
  assert.ok(r.totalDeployable < 1, `total=${r.totalDeployable}`);
});

test('negative stable total clamps to 0 (no phantom from borrow)', () => {
  const r = dc.computeSpotPool([{ coin: 'USDC', total: -54300, hold: -67888 }]);
  assert.equal(r.pool, 0);
  assert.equal(r.anyNegativeStable, true);
});

test('hold reserved in open orders reduces withdrawable', () => {
  const r = dc.computeSpotPool([{ coin: 'USDC', total: 1000, hold: 400 }]);
  assert.equal(r.pool, 600); // 1000 - 400
});

test('FIX 3: both fetches failed → error:true (render fetch error, not $0)', () => {
  const r = dc.computeDeployable({ spotBalances: null, perp: null });
  assert.equal(r.error, true);
  const lines = dc.formatDeployableLines(r);
  assert.ok(lines.join('\n').toLowerCase().includes('fetch error'));
});

test('spot fetch failed but perp present → falls back to perp free margin', () => {
  const r = dc.computeDeployable({
    spotBalances: null,
    perp: { accountValue: 1000, marginUsed: 200, withdrawable: 800 },
  });
  assert.equal(r.error, false);
  assert.equal(r.spotFetched, false);
  assert.equal(r.totalDeployable, 800);
});

test('formatDeployableLines: itemized, mentions single-pool + breakdown', () => {
  const r = dc.computeDeployable({
    spotBalances: SPOT_2606,
    perp: { accountValue: 17200, marginUsed: 16700, withdrawable: 535.04 },
  });
  const txt = dc.formatDeployableLines(r).join('\n');
  assert.ok(txt.includes('Withdrawable (single pool'));
  assert.ok(txt.includes('TOTAL deployable'));
  assert.ok(txt.includes('not all three at once'));
});

test('over-borrow block labels figure as drawn dry powder, not headroom', () => {
  const r = dc.computeDeployable({
    spotBalances: [{ coin: 'USDC', total: -100, hold: -200 }],
    perp: { accountValue: 8610, marginUsed: 8616, withdrawable: 0 },
  });
  const txt = dc.formatDeployableLines(r).join('\n');
  assert.ok(txt.includes('OVER MAX-BORROW'));
  assert.ok(txt.toLowerCase().includes('drawn dry powder'));
  assert.ok(!txt.toLowerCase().includes('headroom'));
});

test('extra stable symbol via env is recognized', () => {
  process.env.FUNDS_STABLE_SYMBOLS = 'MYUSD';
  const r = dc.computeSpotPool([{ coin: 'MYUSD', total: 500, hold: 0 }]);
  assert.equal(r.pool, 500);
});
