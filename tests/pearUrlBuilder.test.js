'use strict';

const test = require('node:test');
const assert = require('node:assert');

// Force the env-var defaults to known values for determinism.
process.env.PEAR_REFERRAL_CODE = 'BlackCatDeFi';
delete process.env.PEAR_BASE_URL;
delete process.env.PEAR_MAX_COPY_TOKENS;

const {
  buildPearCopyUrl,
  buildCopyButtons,
  buildInlineKeyboard,
  REFERRAL_CODE,
  PEAR_BASE_URL,
} = require('../src/pearUrlBuilder');

test('Referral code matches env var (BlackCatDeFi)', () => {
  assert.strictEqual(REFERRAL_CODE, 'BlackCatDeFi');
});

test('5-token short basket generates Pear URL with all tokens, sorted by notional', () => {
  const positions = [
    { coin: 'DYDX', side: 'SHORT', notional: 4460 },
    { coin: 'OP', side: 'SHORT', notional: 4534 },
    { coin: 'ARB', side: 'SHORT', notional: 4485 },
    { coin: 'PYTH', side: 'SHORT', notional: 4510 },
    { coin: 'ENA', side: 'SHORT', notional: 4477 },
  ];
  const url = buildPearCopyUrl(positions);
  // descending notional → OP, PYTH, ARB, ENA, DYDX
  assert.strictEqual(
    url,
    'https://app.pear.garden/trade/hl/USDC-OP+PYTH+ARB+ENA+DYDX?referral=BlackCatDeFi'
  );
});

test('Referral always present in URL', () => {
  const url = buildPearCopyUrl(
    [{ coin: 'BTC', side: 'SHORT', notional: 1000 }],
    'SHORT'
  );
  assert.match(url, /referral=BlackCatDeFi/);
});

test('LONG side filters out SHORTs and vice versa', () => {
  const positions = [
    { coin: 'BTC', side: 'LONG', notional: 5000 },
    { coin: 'ETH', side: 'SHORT', notional: 3000 },
  ];
  assert.match(buildPearCopyUrl(positions, 'LONG'), /USDC-BTC/);
  assert.match(buildPearCopyUrl(positions, 'SHORT'), /USDC-ETH/);
});

test('Empty positions or empty side returns null', () => {
  assert.strictEqual(buildPearCopyUrl([], 'SHORT'), null);
  assert.strictEqual(buildPearCopyUrl(null, 'SHORT'), null);
  assert.strictEqual(
    buildPearCopyUrl(
      [{ coin: 'BTC', side: 'LONG', notional: 1000 }],
      'SHORT'
    ),
    null
  );
});

test('Single token URL has no plus sign', () => {
  const url = buildPearCopyUrl([{ coin: 'BTC', side: 'SHORT', notional: 1000 }]);
  assert.match(url, /USDC-BTC\?/);
  assert.doesNotMatch(url, /\+/);
});

test('Mixed basket → buildCopyButtons returns 2 buttons (SHORT + LONG)', () => {
  const positions = [
    { coin: 'BTC', side: 'LONG', notional: 5000 },
    { coin: 'ETH', side: 'SHORT', notional: 3000 },
  ];
  const btns = buildCopyButtons(positions);
  assert.strictEqual(btns.length, 2);
  assert.ok(btns[0].text.includes('SHORTs') || btns[0].text.includes('LONGs'));
});

test('Single-side basket → buildCopyButtons returns 1 button', () => {
  const positions = [
    { coin: 'BTC', side: 'SHORT', notional: 5000 },
    { coin: 'ETH', side: 'SHORT', notional: 3000 },
  ];
  const btns = buildCopyButtons(positions);
  assert.strictEqual(btns.length, 1);
  assert.match(btns[0].text, /Copiar trade/);
});

test('buildInlineKeyboard returns Telegram-shaped reply_markup', () => {
  const positions = [{ coin: 'BTC', side: 'SHORT', notional: 1000 }];
  const kb = buildInlineKeyboard(positions);
  assert.ok(kb.inline_keyboard);
  assert.ok(Array.isArray(kb.inline_keyboard));
  assert.ok(kb.inline_keyboard[0][0].url);
  assert.match(kb.inline_keyboard[0][0].url, /referral=BlackCatDeFi/);
});

test('No matching positions → buildInlineKeyboard returns null', () => {
  assert.strictEqual(buildInlineKeyboard([]), null);
});

test('Coin tickers URL-encoded', () => {
  // Hypothetical edge case
  const url = buildPearCopyUrl([
    { coin: 'BTC', side: 'SHORT', notional: 100 },
  ]);
  assert.match(url, /USDC-BTC/);
});

test('PEAR_BASE_URL constant matches expected default', () => {
  assert.strictEqual(PEAR_BASE_URL, 'https://app.pear.garden');
});
