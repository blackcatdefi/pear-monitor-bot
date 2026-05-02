'use strict';

const test = require('node:test');
const assert = require('node:assert');
const builder = require('../src/copyAlertBuilder');

test('source label maps types to human names', () => {
  assert.equal(builder._sourceLabel('BCD_WALLET'), 'BCD Wallet');
  assert.equal(builder._sourceLabel('BCD_SIGNALS'), 'BCD Signals');
  assert.equal(builder._sourceLabel('CUSTOM_WALLET'), 'Custom');
  assert.equal(builder._sourceLabel('XYZ'), 'XYZ');
});

test('OPEN alert renders source + composition + capital + risk', () => {
  const { text, keyboard } = builder.buildAlert({
    source: 'BCD_WALLET',
    userId: 1,
    capital: 250,
    mode: 'MANUAL',
    sl_pct: 50,
    trailing_pct: 10,
    trailing_activation_pct: 30,
    positions: [
      { coin: 'WLD', side: 'SHORT' },
      { coin: 'STRK', side: 'SHORT' },
    ],
    event: 'OPEN',
  });
  assert.match(text, /NEW BASKET — BCD Wallet/);
  assert.match(text, /WLD SHORT/);
  assert.match(text, /STRK SHORT/);
  assert.match(text, /\$250/);
  assert.match(text, /SL 50%/);
  // hero pear button — R-CTAOPTIMIZE format: "🍐 Copy $250 on Pear"
  // (label may carry amount if capital > 0; backward-compat falls back to
  // "🍐 Copy on Pear" when capital is 0).
  const allButtons = keyboard.inline_keyboard.flat();
  assert.ok(allButtons.find((b) => /Copy.*on Pear/i.test(b.text)));
});

test('CLOSE alert renders close text and no Pear button', () => {
  const { text, keyboard } = builder.buildAlert({
    source: 'BCD_SIGNALS',
    userId: 2,
    capital: 100,
    sl_pct: 50,
    trailing_pct: 10,
    trailing_activation_pct: 30,
    positions: [{ coin: 'ENA', side: 'SHORT' }],
    event: 'CLOSE',
  });
  assert.match(text, /BASKET CLOSED — BCD Signals/);
  const allButtons = keyboard.inline_keyboard.flat();
  // No Pear hero button on close
  assert.ok(!allButtons.find((b) => b.url && /pear\.garden/.test(b.url)));
});

test('AUTO mode adds the AUTO wording', () => {
  const { text } = builder.buildAlert({
    source: 'BCD_WALLET',
    userId: 1,
    capital: 100,
    mode: 'AUTO',
    sl_pct: 50,
    trailing_pct: 10,
    trailing_activation_pct: 30,
    positions: [{ coin: 'BTC', side: 'LONG' }],
    event: 'OPEN',
  });
  assert.match(text, /AUTO mode/i);
});

test('uses provided pearUrl when given (signals path)', () => {
  const url =
    'https://app.pear.garden/trade/hl/USDC-WLD+STRK?referral=BlackCatDeFi';
  const { keyboard } = builder.buildAlert({
    source: 'BCD_SIGNALS',
    userId: 1,
    capital: 200,
    sl_pct: 50,
    trailing_pct: 10,
    trailing_activation_pct: 30,
    positions: [],
    pearUrl: url,
    event: 'OPEN',
  });
  const allButtons = keyboard.inline_keyboard.flat();
  const pear = allButtons.find((b) => b.url && /pear\.garden/.test(b.url));
  assert.equal(pear.url, url);
});

test('builds Pear URL from positions when not provided', () => {
  const { keyboard } = builder.buildAlert({
    source: 'BCD_WALLET',
    userId: 1,
    capital: 100,
    sl_pct: 50,
    trailing_pct: 10,
    trailing_activation_pct: 30,
    positions: [
      { coin: 'ENA', side: 'SHORT' },
      { coin: 'WLD', side: 'SHORT' },
    ],
    event: 'OPEN',
  });
  const allButtons = keyboard.inline_keyboard.flat();
  const pear = allButtons.find((b) => b.url && /pear\.garden/.test(b.url));
  assert.match(pear.url, /USDC-ENA\+WLD\?referral=BlackCatDeFi/);
});

test('custom source uses sourceLabel override (e.g. user-provided wallet label)', () => {
  const { text } = builder.buildAlert({
    source: 'CUSTOM_WALLET',
    sourceLabel: 'Whale 1',
    userId: 1,
    capital: 100,
    sl_pct: 50,
    trailing_pct: 10,
    trailing_activation_pct: 30,
    positions: [{ coin: 'BTC', side: 'LONG' }],
    event: 'OPEN',
  });
  assert.match(text, /NEW BASKET — Whale 1/);
});

test('keyboard contains Skip + Config callbacks', () => {
  const { keyboard } = builder.buildAlert({
    source: 'BCD_WALLET',
    userId: 1,
    capital: 100,
    sl_pct: 50,
    trailing_pct: 10,
    trailing_activation_pct: 30,
    positions: [{ coin: 'BTC', side: 'LONG' }],
    event: 'OPEN',
  });
  const allButtons = keyboard.inline_keyboard.flat();
  assert.ok(allButtons.find((b) => b.callback_data === 'copytrade:skip'));
  assert.ok(allButtons.find((b) => b.callback_data === 'copytrade:menu'));
});

test('renders local time stamp', () => {
  const { text } = builder.buildAlert({
    source: 'BCD_WALLET',
    userId: 999,
    capital: 100,
    sl_pct: 50,
    trailing_pct: 10,
    trailing_activation_pct: 30,
    positions: [{ coin: 'BTC', side: 'LONG' }],
    event: 'OPEN',
  });
  assert.match(text, /🕐/);
});
