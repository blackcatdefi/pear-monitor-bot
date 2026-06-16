'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const wp = require('../src/weeklyPnl');

function fill(coin, px, sz, closedPnl, fee, dir) {
  return { coin, px: String(px), sz: String(sz), closedPnl: String(closedPnl), fee: String(fee), dir };
}

test('aggregateFills: net_pnl = Σ closedPnl − Σ fee', () => {
  const fills = [
    fill('BTC', 66599, 0.7, 2034.1, 47.5, 'Close Long'),
    fill('BTC', 66599, 0.25, 746.2, 17.4, 'Close Long'),
    fill('ETH', 3000, 1, -300, 5, 'Close Short'),
  ];
  const s = wp.aggregateFills(fills);
  assert.ok(Math.abs(s.gross_pnl - (2034.1 + 746.2 - 300)) < 1e-6);
  assert.ok(Math.abs(s.total_fees - (47.5 + 17.4 + 5)) < 1e-6);
  assert.ok(Math.abs(s.net_pnl - (2034.1 + 746.2 - 300 - 69.9)) < 1e-6);
});

test('aggregateFills: W/L counted on closedPnl≠0, breakeven excluded', () => {
  const fills = [
    fill('BTC', 100, 1, 50, 0.1, 'Close Long'), // win
    fill('ETH', 100, 1, -20, 0.1, 'Close Short'), // loss
    fill('SOL', 100, 1, 0, 0.1, 'Close Long'), // breakeven close
    fill('ARB', 100, 1, 0, 0.1, 'Open Long'), // opening fill (not a close)
  ];
  const s = wp.aggregateFills(fills);
  assert.equal(s.wins, 1);
  assert.equal(s.losses, 1);
  assert.equal(s.breakeven, 1); // only the SOL close, not the ARB open
  assert.equal(s.realized_closes, 3);
  assert.equal(s.fills, 4);
});

test('aggregateFills: win_rate is null (n/d) when no decided closes', () => {
  const fills = [fill('BTC', 100, 1, 0, 0.1, 'Open Long')];
  const s = wp.aggregateFills(fills);
  assert.equal(s.win_rate_pct, null);
});

test('aggregateFills: win_rate excludes breakeven from denominator', () => {
  const fills = [
    fill('BTC', 100, 1, 50, 0, 'Close Long'),
    fill('ETH', 100, 1, -50, 0, 'Close Short'),
    fill('SOL', 100, 1, 0, 0, 'Close Long'),
  ];
  const s = wp.aggregateFills(fills);
  assert.equal(s.win_rate_pct, 50); // 1 / (1+1), breakeven not counted
});

test('aggregateFills: best/worst by aggregated per-coin closedPnl', () => {
  const fills = [
    fill('BTC', 100, 1, 100, 0, 'Close Long'),
    fill('BTC', 100, 1, 50, 0, 'Close Long'), // BTC total +150
    fill('ETH', 100, 1, -200, 0, 'Close Short'),
  ];
  const s = wp.aggregateFills(fills);
  assert.equal(s.best.coin, 'BTC');
  assert.ok(Math.abs(s.best.pnl - 150) < 1e-6);
  assert.equal(s.worst.coin, 'ETH');
  assert.ok(Math.abs(s.worst.pnl - -200) < 1e-6);
});

test('aggregateFills: volume = Σ |px·sz| across ALL fills', () => {
  const fills = [
    fill('BTC', 100, 2, 0, 0, 'Open Long'),
    fill('BTC', 100, 2, 10, 0, 'Close Long'),
  ];
  const s = wp.aggregateFills(fills);
  assert.equal(s.volume, 400);
});

test('FIX 3: Week-24 regression — 4117 fills, $44.6M vol, all closedPnl=0 → calc_failure', () => {
  // Reproduce the production bug: heavy churn, real volume, but the PnL field
  // came back zero. This must be flagged, never rendered as a flat week.
  const fills = [];
  for (let i = 0; i < 4117; i++) {
    // notional sums to ~$44.6M; closedPnl deliberately 0 (the bug condition)
    fills.push(fill('xyz:GOOGL', 10845, 1, 0, 0, 'Open Long'));
  }
  const s = wp.aggregateFills(fills);
  assert.equal(s.fills, 4117);
  assert.ok(s.volume > 44_000_000);
  assert.equal(s.wins, 0);
  assert.equal(s.losses, 0);
  assert.equal(s.win_rate_pct, null); // n/d, NOT 0.0%
  assert.equal(s.calc_failure, true);
});

test('FIX 3: a genuine flat week (no fills) is NOT a calc_failure', () => {
  const s = wp.aggregateFills([]);
  assert.equal(s.calc_failure, false);
  assert.equal(s.win_rate_pct, null);
});

test('a real week with realized closes is NOT a calc_failure', () => {
  const fills = [
    fill('BTC', 100, 1, 0, 0, 'Open Long'),
    fill('BTC', 110, 1, 10, 0.5, 'Close Long'),
  ];
  const s = wp.aggregateFills(fills);
  assert.equal(s.calc_failure, false);
  assert.equal(s.wins, 1);
  assert.ok(Math.abs(s.net_pnl - 9.5) < 1e-6);
});

test('weeklyWalletAddresses defaults to canonical BCD wallet', () => {
  const saved = {
    a: process.env.WEEKLY_SUMMARY_WALLETS,
    b: process.env.PRIMARY_WALLET_ADDRESS,
    c: process.env.BCD_WALLET_ADDRESS,
    d: process.env.BCD_WALLET,
  };
  delete process.env.WEEKLY_SUMMARY_WALLETS;
  delete process.env.PRIMARY_WALLET_ADDRESS;
  delete process.env.BCD_WALLET_ADDRESS;
  delete process.env.BCD_WALLET;
  const w = wp.weeklyWalletAddresses();
  assert.deepEqual(w, ['0xc7ae23316b47f7e75f455f53ad37873a18351505']);
  if (saved.a !== undefined) process.env.WEEKLY_SUMMARY_WALLETS = saved.a;
  if (saved.b !== undefined) process.env.PRIMARY_WALLET_ADDRESS = saved.b;
  if (saved.c !== undefined) process.env.BCD_WALLET_ADDRESS = saved.c;
  if (saved.d !== undefined) process.env.BCD_WALLET = saved.d;
});

test('buildWeekly: hard fetch failure surfaces fetchError (no fabrication)', async () => {
  const stubApi = { getUserFillsByTime: async () => null };
  const res = await wp.buildWeekly(stubApi, { now: new Date('2026-06-15T12:00:00Z') });
  assert.equal(res.fetchError, true);
  assert.equal(res.summary, null);
});

test('buildWeekly: zero fills returns null (skip the message)', async () => {
  const stubApi = { getUserFillsByTime: async () => [] };
  const res = await wp.buildWeekly(stubApi, { now: new Date('2026-06-15T12:00:00Z') });
  assert.equal(res, null);
});

test('buildWeekly: aggregates fetched fills within the week window', async () => {
  const now = new Date('2026-06-15T12:00:00Z'); // a Monday
  const startMs = wp.startOfWeekUTC(now).getTime();
  const stubApi = {
    getUserFillsByTime: async () => [
      { coin: 'BTC', px: '100', sz: '1', closedPnl: '25', fee: '1', dir: 'Close Long', time: startMs + 1000 },
      { coin: 'ETH', px: '100', sz: '1', closedPnl: '-5', fee: '1', dir: 'Close Short', time: startMs + 2000 },
    ],
  };
  const res = await wp.buildWeekly(stubApi, { now });
  assert.equal(res.fetchError, false);
  assert.equal(res.summary.wins, 1);
  assert.equal(res.summary.losses, 1);
  assert.ok(Math.abs(res.summary.net_pnl - (25 - 5 - 2)) < 1e-6);
});
