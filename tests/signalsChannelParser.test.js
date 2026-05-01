'use strict';

const test = require('node:test');
const assert = require('node:assert');
const parser = require('../src/signalsChannelParser');

test('parses USDC-collateral SHORT basket', () => {
  const r = parser.extractFromPearUrl(
    'https://app.pear.garden/trade/hl/USDC-WLD+STRK+ENA+TIA+ARB?referral=BlackCatDeFi'
  );
  assert.deepEqual(r.shortTokens, ['WLD', 'STRK', 'ENA', 'TIA', 'ARB']);
  assert.deepEqual(r.longTokens, []);
  assert.equal(r.collateral, 'USDC');
  assert.match(r.urlWithReferral, /referral=BlackCatDeFi$/);
});

test('parses LONG-vs-SHORT basket without USDC collateral', () => {
  const r = parser.extractFromPearUrl(
    'https://app.pear.garden/trade/hl/HYPE+LIT-WLFI?referral=BlackCatDeFi'
  );
  assert.deepEqual(r.longTokens, ['HYPE', 'LIT']);
  assert.deepEqual(r.shortTokens, ['WLFI']);
  assert.equal(r.collateral, null);
});

test('parses mixed basket with longs vs shorts', () => {
  const r = parser.extractFromPearUrl(
    'https://app.pear.garden/trade/hl/USDC-BLUR+DYDX+ARB+OP+CRV+LDO?referral=BlackCatDeFi'
  );
  assert.equal(r.shortTokens.length, 6);
  assert.equal(r.longTokens.length, 0);
});

test('parses LONG basket vs USDC on right', () => {
  const r = parser.extractFromPearUrl(
    'https://app.pear.garden/trade/hl/HYPE+LIT-USDC?referral=BlackCatDeFi'
  );
  assert.deepEqual(r.longTokens, ['HYPE', 'LIT']);
  assert.deepEqual(r.shortTokens, []);
});

test('forces referral=BlackCatDeFi when incoming has different referral', () => {
  const r = parser.extractFromPearUrl(
    'https://app.pear.garden/trade/hl/USDC-ENA?referral=somecompetitor'
  );
  assert.match(r.urlWithReferral, /referral=BlackCatDeFi$/);
});

test('forces referral when there is no referral param', () => {
  const r = parser.extractFromPearUrl('https://app.pear.garden/trade/hl/USDC-ENA');
  assert.match(r.urlWithReferral, /referral=BlackCatDeFi$/);
});

test('returns null on non-pear URL', () => {
  assert.equal(parser.extractFromPearUrl('https://twitter.com/blackcat'), null);
});

test('returns null on malformed Pear URL', () => {
  assert.equal(parser.extractFromPearUrl('https://app.pear.garden/'), null);
  assert.equal(parser.extractFromPearUrl('https://app.pear.garden/trade/'), null);
  assert.equal(parser.extractFromPearUrl('not a url at all'), null);
});

test('handles tokens with digits like ZRO/USDT0', () => {
  const r = parser.extractFromPearUrl(
    'https://app.pear.garden/trade/hl/USDC-WLD+STRK+ZRO+AVAX+ENA?referral=BlackCatDeFi'
  );
  assert.deepEqual(r.shortTokens, ['WLD', 'STRK', 'ZRO', 'AVAX', 'ENA']);
});

test('buildPearUrlFromSides with LONG only', () => {
  const u = parser.buildPearUrlFromSides({ longTokens: ['HYPE', 'LIT'] });
  assert.match(u, /HYPE\+LIT-USDC\?referral=BlackCatDeFi/);
});

test('buildPearUrlFromSides with SHORT only', () => {
  const u = parser.buildPearUrlFromSides({ shortTokens: ['ENA', 'WLD'] });
  assert.match(u, /USDC-ENA\+WLD\?referral=BlackCatDeFi/);
});

test('buildPearUrlFromSides with mixed sides', () => {
  const u = parser.buildPearUrlFromSides({
    longTokens: ['HYPE'],
    shortTokens: ['WLFI'],
  });
  assert.match(u, /HYPE-WLFI\?referral=BlackCatDeFi/);
});

test('buildPearUrlFromSides returns null on empty input', () => {
  assert.equal(parser.buildPearUrlFromSides({ longTokens: [], shortTokens: [] }), null);
});

test('_splitTokens filters out non-uppercase / invalid', () => {
  assert.deepEqual(parser._splitTokens('AAA+bbb+11+--'), ['AAA', 'BBB', '11']);
});
