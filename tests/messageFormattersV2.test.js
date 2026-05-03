'use strict';

/**
 * R-BASKET (3 may 2026) — messageFormattersV2 render tests.
 *
 * Verifies the compact OPEN/CLOSE templates and the sanity gate that refuses
 * to dispatch a "$0.00 manual close" CLOSE message.
 */

const test = require('node:test');
const assert = require('node:assert/strict');

const fmt = require('../src/messageFormattersV2');

// ---------------------------------------------------------------------------
// Single-leg OPEN
// ---------------------------------------------------------------------------

test('renderBasketOpen: single-leg uses "NEW TRADE" header and one leg row', () => {
  const out = fmt.renderBasketOpen({
    traderLabel: 'huf.eth',
    traderAddr: '0x1234567890abcdef1234567890abcdef12345678',
    legs: [{ coin: 'BTC', side: 'LONG', size: 0.5, entryPrice: 60000 }],
  });
  assert.match(out, /^🍐 NEW TRADE — huf\.eth$/m);
  assert.match(out, /🟢 LONG BTC/);
  assert.match(out, /\$30k/); // 0.5 * 60000 = 30k
  assert.match(out, /pear\.garden\/\?referral=BlackCatDeFi/);
});

test('renderBasketOpen: single-leg falls back to short address when no label', () => {
  const out = fmt.renderBasketOpen({
    traderLabel: null,
    traderAddr: '0x1234567890abcdef1234567890abcdef12345678',
    legs: [{ coin: 'ETH', side: 'SHORT', size: -2, entryPrice: 3000 }],
  });
  assert.match(out, /0x1234\.\.\.5678/);
  assert.match(out, /🔴 SHORT ETH/);
});

// ---------------------------------------------------------------------------
// Pair-trade (2 legs) — compact pair card
// ---------------------------------------------------------------------------

test('renderBasketOpen: 2 legs use "NEW PAIR TRADE" header', () => {
  const out = fmt.renderBasketOpen({
    traderLabel: 'pair.guy',
    traderAddr: '0xaa',
    legs: [
      { coin: 'BTC', side: 'LONG', size: 0.5, entryPrice: 60000 },
      { coin: 'ETH', side: 'SHORT', size: -5, entryPrice: 3000 },
    ],
  });
  assert.match(out, /^🍐 NEW PAIR TRADE — pair\.guy$/m);
  assert.match(out, /Δ-neutral/);
  // Both legs rendered
  assert.match(out, /BTC/);
  assert.match(out, /ETH/);
});

// ---------------------------------------------------------------------------
// Expanded (3+ legs) — basket layout
// ---------------------------------------------------------------------------

test('renderBasketOpen: 3+ legs trigger expanded layout with "NEW BASKET" header', () => {
  const legs = [
    { coin: 'BTC', side: 'LONG', size: 0.5, entryPrice: 60000 },
    { coin: 'ETH', side: 'LONG', size: 5, entryPrice: 3000 },
    { coin: 'WLD', side: 'SHORT', size: -1000, entryPrice: 4 },
  ];
  const out = fmt.renderBasketOpen({
    traderLabel: 'whale',
    traderAddr: '0xbb',
    legs,
  });
  assert.match(out, /^🍐 NEW BASKET — whale$/m);
  assert.match(out, /🆔 3 legs/);
  assert.match(out, /Gross/);
});

test('renderBasketOpen: 2 legs but huge notional escalates to expanded', () => {
  const out = fmt.renderBasketOpen({
    traderLabel: 'whale',
    traderAddr: '0xbb',
    legs: [
      { coin: 'BTC', side: 'LONG', size: 1, entryPrice: 60000 },
      { coin: 'ETH', side: 'SHORT', size: -10, entryPrice: 3000 },
    ],
  });
  // 60k + 30k = 90k > 50k threshold → expanded
  assert.match(out, /^🍐 NEW BASKET/m);
});

// ---------------------------------------------------------------------------
// CLOSE — win and loss variants
// ---------------------------------------------------------------------------

test('renderBasketClose: win uses ✅ and shows held duration', () => {
  const out = fmt.renderBasketClose({
    traderLabel: 'whale',
    traderAddr: '0xbb',
    legs: [
      { coin: 'BTC', side: 'LONG', entryPrice: 60000, exitPrice: 63000 },
    ],
    pnl: { realized: 1500, fees: 15, marginUsed: 5000 },
    heldMs: 3 * 3600_000, // 3 hours
  });
  assert.match(out, /✅/);
  assert.match(out, /\+\$1500/);
  assert.match(out, /3h 0m/);
});

test('renderBasketClose: loss uses ❌ and compact 5-line variant for 2+ legs', () => {
  const out = fmt.renderBasketClose({
    traderLabel: 'unlucky',
    traderAddr: '0xcc',
    legs: [
      { coin: 'BTC', side: 'LONG', entryPrice: 60000, exitPrice: 58000 },
      { coin: 'ETH', side: 'SHORT', entryPrice: 3000, exitPrice: 3100 },
    ],
    pnl: { realized: -250, fees: 10, marginUsed: 2000 },
    heldMs: 30 * 60_000,
  });
  assert.match(out, /❌/);
  assert.match(out, /-\$250/);
  // L:BTC / S:ETH compact line
  assert.match(out, /L:BTC \/ S:ETH/);
});

// ---------------------------------------------------------------------------
// Sanity gate
// ---------------------------------------------------------------------------

test('isCloseEmittable: refuses pnl with both realized=0 AND fees=0 (legacy "$0.00" bug)', () => {
  assert.equal(fmt.isCloseEmittable({ realized: 0, fees: 0 }), false);
});

test('isCloseEmittable: accepts non-zero realized', () => {
  assert.equal(fmt.isCloseEmittable({ realized: 10, fees: 0 }), true);
  assert.equal(fmt.isCloseEmittable({ realized: -10, fees: 0 }), true);
});

test('isCloseEmittable: accepts non-zero fees alone (close fee always present)', () => {
  assert.equal(fmt.isCloseEmittable({ realized: 0, fees: 0.5 }), true);
});

test('isCloseEmittable: refuses null/undefined/garbage', () => {
  assert.equal(fmt.isCloseEmittable(null), false);
  assert.equal(fmt.isCloseEmittable(undefined), false);
  assert.equal(fmt.isCloseEmittable({}), false);
  assert.equal(fmt.isCloseEmittable({ realized: 'oops', fees: 'oops' }), false);
});

// ---------------------------------------------------------------------------
// Helpers — only the publicly-exported ones to keep coupling honest.
// ---------------------------------------------------------------------------

test('_shortAddr collapses long hex addresses', () => {
  assert.equal(fmt._shortAddr('0x1234567890abcdef1234567890abcdef12345678'), '0x1234...5678');
});

test('_shortAddr returns short input untouched', () => {
  assert.equal(fmt._shortAddr('huf.eth'), 'huf.eth');
});

test('_fmtUsdK rounds to k for large values', () => {
  assert.equal(fmt._fmtUsdK(60000), '$60k');
  assert.equal(fmt._fmtUsdK(1500), '$1.5k');
});

test('_fmtUsdK returns plain dollars below 1k', () => {
  assert.equal(fmt._fmtUsdK(500), '$500');
});

test('_fmtPnl formats positive vs negative with sign', () => {
  assert.equal(fmt._fmtPnl(150), '+$150');
  assert.equal(fmt._fmtPnl(-150), '-$150');
});
