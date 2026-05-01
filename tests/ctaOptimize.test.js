'use strict';

/**
 * R-CTAOPTIMIZE — capital-aware hero label + quick-amount row + UTM tracking.
 *
 * Covers:
 *   pearUrlBuilder    — hashUserId determinism + opts query string emission
 *   alertButtons      — capital-aware label + quick-amount row + per-user UTM
 *   copyAlertBuilder  — copy-trade alert keyboard layout w/ pre-fill + UTM
 *   backward-compat   — old call sites without opts emit legacy URL shape
 */

// --- Pin env BEFORE requires so module-level env reads are deterministic ---
process.env.PEAR_REFERRAL_CODE = 'BlackCatDeFi';
process.env.PEAR_BASE_URL = 'https://app.pear.garden';
process.env.PEAR_MAX_COPY_TOKENS = '10';
process.env.PEAR_PREFILL_PARAMS = 'amount,size,capital';
process.env.PEAR_LEVERAGE_PARAM = 'leverage';
process.env.ANALYTICS_HASH_SALT = 'pear-monitor-bot-v1';
process.env.QUICK_AMOUNT_MULTIPLIERS = '0.5,1,2';
process.env.QUICK_AMOUNTS_ENABLED = 'true';
process.env.COPY_CTA_TEXT = '';

const test = require('node:test');
const assert = require('node:assert/strict');

const pearUrl = require('../src/pearUrlBuilder');
const buttons = require('../src/alertButtons');
const builder = require('../src/copyAlertBuilder');

// Common fixture — a single-side SHORT basket
function shortBasket() {
  return [
    { coin: 'ENA', side: 'SHORT', notional: 5000 },
    { coin: 'WLD', side: 'SHORT', notional: 3000 },
    { coin: 'STRK', side: 'SHORT', notional: 1500 },
  ];
}
function mixedBasket() {
  return [
    { coin: 'ENA', side: 'SHORT', notional: 5000 },
    { coin: 'BTC', side: 'LONG', notional: 4000 },
    { coin: 'WLD', side: 'SHORT', notional: 1500 },
  ];
}

// =============================================================
// pearUrlBuilder — hashUserId
// =============================================================

test('hashUserId is deterministic for the same input', () => {
  const a = pearUrl.hashUserId('user-123');
  const b = pearUrl.hashUserId('user-123');
  assert.equal(a, b);
  assert.equal(a.length, 8);
  assert.match(a, /^[0-9a-f]{8}$/);
});

test('hashUserId differs between distinct users', () => {
  const a = pearUrl.hashUserId('user-123');
  const b = pearUrl.hashUserId('user-456');
  assert.notEqual(a, b);
});

test('hashUserId returns null for empty inputs', () => {
  assert.equal(pearUrl.hashUserId(null), null);
  assert.equal(pearUrl.hashUserId(undefined), null);
  assert.equal(pearUrl.hashUserId(''), null);
});

test('hashUserId accepts numeric userId', () => {
  const h = pearUrl.hashUserId(123456789);
  assert.equal(h.length, 8);
  assert.match(h, /^[0-9a-f]{8}$/);
});

// =============================================================
// pearUrlBuilder — buildPearCopyUrl with opts
// =============================================================

test('buildPearCopyUrl backward-compat: no opts emits legacy URL shape', () => {
  const url = pearUrl.buildPearCopyUrl(shortBasket(), 'SHORT');
  assert.ok(url.startsWith('https://app.pear.garden/trade/hl/USDC-'));
  assert.ok(url.includes('referral=BlackCatDeFi'));
  // Legacy: no amount/size/capital params, no utm
  assert.ok(!url.includes('amount='));
  assert.ok(!url.includes('utm_id='));
  assert.ok(!url.includes('utm_source='));
  assert.ok(!url.includes('leverage='));
});

test('buildPearCopyUrl emits amount aliases when capital provided', () => {
  const url = pearUrl.buildPearCopyUrl(shortBasket(), 'SHORT', { capital: 500 });
  assert.ok(url.includes('amount=500'));
  assert.ok(url.includes('size=500'));
  assert.ok(url.includes('capital=500'));
  assert.ok(url.includes('referral=BlackCatDeFi'));
});

test('buildPearCopyUrl emits leverage param when provided', () => {
  const url = pearUrl.buildPearCopyUrl(shortBasket(), 'SHORT', {
    capital: 500,
    leverage: 5,
  });
  assert.ok(url.includes('leverage=5'));
});

test('buildPearCopyUrl emits anonymized utm_id (not raw userId)', () => {
  const url = pearUrl.buildPearCopyUrl(shortBasket(), 'SHORT', {
    capital: 1000,
    userId: 'user-123',
    source: 'tg-alert',
  });
  const expectedHash = pearUrl.hashUserId('user-123');
  assert.ok(url.includes(`utm_id=${expectedHash}`));
  assert.ok(url.includes('utm_source=tg-alert'));
  // Raw userId must NEVER appear
  assert.ok(!url.includes('user-123'));
});

test('buildPearCopyUrl tokens preserved in path; query carries opts', () => {
  const url = pearUrl.buildPearCopyUrl(shortBasket(), 'SHORT', { capital: 250 });
  // Tokens in path (sorted by descending notional)
  assert.ok(url.includes('USDC-ENA+WLD+STRK'));
});

test('buildPearCopyUrl returns null when no positions match side', () => {
  const url = pearUrl.buildPearCopyUrl(shortBasket(), 'LONG', { capital: 500 });
  assert.equal(url, null);
});

test('buildPearCopyUrl handles fractional capital (rounds to 2 decimals)', () => {
  const url = pearUrl.buildPearCopyUrl(shortBasket(), 'SHORT', { capital: 123.456 });
  assert.ok(url.includes('amount=123.46'));
});

// =============================================================
// alertButtons.formatAmount
// =============================================================

test('formatAmount: under 1k → "$N"', () => {
  assert.equal(buttons.formatAmount(500), '$500');
  assert.equal(buttons.formatAmount(750), '$750');
});

test('formatAmount: 1k+ → "$Nk"', () => {
  assert.equal(buttons.formatAmount(1000), '$1k');
  assert.equal(buttons.formatAmount(5000), '$5k');
  assert.equal(buttons.formatAmount(1500), '$1.5k');
});

test('formatAmount: 1M+ → "$NM"', () => {
  assert.equal(buttons.formatAmount(1_000_000), '$1M');
  assert.equal(buttons.formatAmount(2_500_000), '$2.5M');
});

test('formatAmount: invalid input → null', () => {
  assert.equal(buttons.formatAmount(0), null);
  assert.equal(buttons.formatAmount(-100), null);
  assert.equal(buttons.formatAmount(NaN), null);
  assert.equal(buttons.formatAmount(null), null);
});

// =============================================================
// alertButtons.buildAlertKeyboard — capital-aware hero
// =============================================================

test('buildAlertKeyboard backward-compat: no opts emits hero without amount', () => {
  const kb = buttons.buildAlertKeyboard(shortBasket(), 'open');
  assert.ok(kb && Array.isArray(kb.inline_keyboard));
  assert.equal(kb.inline_keyboard[0][0].text, '🍐 Copiar en Pear');
  // URL should be legacy (no pre-fill)
  assert.ok(!kb.inline_keyboard[0][0].url.includes('amount='));
});

test('buildAlertKeyboard with capital: hero label includes amount', () => {
  const kb = buttons.buildAlertKeyboard(shortBasket(), 'open', { capital: 500 });
  assert.equal(kb.inline_keyboard[0][0].text, '🍐 Copiar $500 en Pear');
  assert.ok(kb.inline_keyboard[0][0].url.includes('amount=500'));
});

test('buildAlertKeyboard with capital: emits quick-amount row [0.5x, 1x, 2x]', () => {
  const kb = buttons.buildAlertKeyboard(shortBasket(), 'open', { capital: 1000 });
  // Expect: row 0 = hero, row 1 = quick amounts
  assert.ok(kb.inline_keyboard.length >= 2);
  const quickRow = kb.inline_keyboard[1];
  assert.equal(quickRow.length, 3);
  assert.match(quickRow[0].text, /0\.5x/);
  assert.match(quickRow[1].text, /1x/);
  assert.match(quickRow[2].text, /2x/);
  // amounts must be different across the 3 buttons
  assert.notEqual(quickRow[0].url, quickRow[1].url);
  assert.notEqual(quickRow[1].url, quickRow[2].url);
});

test('buildAlertKeyboard quick-amount row carries correct pre-fill amounts', () => {
  const kb = buttons.buildAlertKeyboard(shortBasket(), 'open', { capital: 1000 });
  const quickRow = kb.inline_keyboard[1];
  assert.ok(quickRow[0].url.includes('amount=500'));   // 0.5 * 1000
  assert.ok(quickRow[1].url.includes('amount=1000'));  // 1.0 * 1000
  assert.ok(quickRow[2].url.includes('amount=2000'));  // 2.0 * 1000
});

test('buildAlertKeyboard skips quick-amount row when capital is 0', () => {
  const kb = buttons.buildAlertKeyboard(shortBasket(), 'open');
  // Hero only, no quick-amount row
  // Could be just 1 row (hero) or 2 rows (hero + mute) — but never have a quick row
  for (const row of kb.inline_keyboard) {
    for (const btn of row) {
      assert.ok(!/^[0-9.]+x \(/.test(btn.text || ''), `unexpected quick btn: ${btn.text}`);
    }
  }
});

test('buildAlertKeyboard mixed-side basket: 2 hero rows, no quick amounts', () => {
  const kb = buttons.buildAlertKeyboard(mixedBasket(), 'open', { capital: 500 });
  // Mixed-side → hero row per side, no quick row
  assert.equal(kb.inline_keyboard[0][0].text, '🍐 Copiar SHORTs $500 en Pear');
  assert.equal(kb.inline_keyboard[1][0].text, '🍐 Copiar LONGs $500 en Pear');
});

test('buildAlertKeyboard close events: no hero, no quick amounts', () => {
  const kb = buttons.buildAlertKeyboard(shortBasket(), 'close', {
    capital: 500,
    wallet: '0xabc1234567890abcdef0',
  });
  // Only mute button row (no hero, no quick amounts) — wallet present
  assert.ok(kb && Array.isArray(kb.inline_keyboard));
  for (const row of kb.inline_keyboard) {
    for (const btn of row) {
      assert.ok(!/Copiar/.test(btn.text || ''), `unexpected hero on close: ${btn.text}`);
    }
  }
});

test('buildAlertKeyboard forwards userId → URL contains anonymized utm_id', () => {
  const kb = buttons.buildAlertKeyboard(shortBasket(), 'open', {
    capital: 500,
    userId: 'user-xyz',
    source: 'tg-track',
  });
  const url = kb.inline_keyboard[0][0].url;
  const expectedHash = pearUrl.hashUserId('user-xyz');
  assert.ok(url.includes(`utm_id=${expectedHash}`));
  assert.ok(url.includes('utm_source=tg-track'));
  // Raw userId must NEVER appear in URL
  assert.ok(!url.includes('user-xyz'));
});

// =============================================================
// alertButtons._roundQuickAmount
// =============================================================

test('_roundQuickAmount rounds large amounts cleanly', () => {
  assert.equal(buttons._roundQuickAmount(11_234), 11_200);
  assert.equal(buttons._roundQuickAmount(1_234), 1_230);
  assert.equal(buttons._roundQuickAmount(123), 125);
  assert.equal(buttons._roundQuickAmount(50), 50);
});

// =============================================================
// copyAlertBuilder — buildAlert keyboard
// =============================================================

test('copyAlertBuilder OPEN with capital → hero shows amount + quick row', () => {
  const result = builder.buildAlert({
    source: 'BCD_WALLET',
    userId: 12345,
    capital: 500,
    mode: 'MANUAL',
    positions: shortBasket(),
    event: 'OPEN',
  });
  const kb = result.keyboard.inline_keyboard;
  assert.equal(kb[0][0].text, '🍐 Copiar $500 en Pear');
  assert.ok(kb[0][0].url.includes('amount=500'));
  // Quick-amount row is row[1] (or row[2] if hero is split)
  const quickRow = kb[1];
  assert.equal(quickRow.length, 3);
  assert.match(quickRow[0].text, /0\.5x/);
});

test('copyAlertBuilder forwards userId → utm_id present in hero URL', () => {
  const result = builder.buildAlert({
    source: 'BCD_SIGNALS',
    userId: 67890,
    capital: 500,
    mode: 'MANUAL',
    positions: shortBasket(),
    event: 'OPEN',
  });
  const url = result.keyboard.inline_keyboard[0][0].url;
  const expected = pearUrl.hashUserId(67890);
  assert.ok(url.includes(`utm_id=${expected}`));
  assert.ok(url.includes('utm_source=tg-signal-channel'));
});

test('copyAlertBuilder utm_source maps source key correctly', () => {
  const wallet = builder.buildAlert({
    source: 'BCD_WALLET', userId: 1, capital: 100, mode: 'MANUAL',
    positions: shortBasket(), event: 'OPEN',
  });
  const signals = builder.buildAlert({
    source: 'BCD_SIGNALS', userId: 1, capital: 100, mode: 'MANUAL',
    positions: shortBasket(), event: 'OPEN',
  });
  const custom = builder.buildAlert({
    source: 'CUSTOM_WALLET', userId: 1, capital: 100, mode: 'MANUAL',
    positions: shortBasket(), event: 'OPEN',
  });
  assert.ok(wallet.keyboard.inline_keyboard[0][0].url.includes('utm_source=tg-alert-bcd-wallet'));
  assert.ok(signals.keyboard.inline_keyboard[0][0].url.includes('utm_source=tg-signal-channel'));
  assert.ok(custom.keyboard.inline_keyboard[0][0].url.includes('utm_source=tg-alert-custom'));
});

test('copyAlertBuilder CLOSE event: no hero, no quick amounts', () => {
  const result = builder.buildAlert({
    source: 'BCD_WALLET',
    userId: 12345,
    capital: 500,
    mode: 'MANUAL',
    positions: shortBasket(),
    event: 'CLOSE',
  });
  const kb = result.keyboard.inline_keyboard;
  for (const row of kb) {
    for (const btn of row) {
      assert.ok(!/Copiar/.test(btn.text || ''), 'no Copiar button on CLOSE');
      assert.ok(!/^[0-9.]+x \(/.test(btn.text || ''), 'no quick-amount on CLOSE');
    }
  }
});

test('copyAlertBuilder backward-compat: zero capital still produces URL (no pre-fill)', () => {
  const result = builder.buildAlert({
    source: 'BCD_WALLET',
    userId: 12345,
    capital: 0,
    mode: 'MANUAL',
    positions: shortBasket(),
    event: 'OPEN',
  });
  const url = result.keyboard.inline_keyboard[0][0].url;
  assert.ok(url.includes('referral=BlackCatDeFi'));
  // Without capital, URL should NOT carry amount pre-fill
  assert.ok(!url.includes('amount='));
});

test('copyAlertBuilder always includes Skip + Config row at the bottom', () => {
  const result = builder.buildAlert({
    source: 'BCD_WALLET',
    userId: 12345,
    capital: 500,
    mode: 'MANUAL',
    positions: shortBasket(),
    event: 'OPEN',
  });
  const last = result.keyboard.inline_keyboard[result.keyboard.inline_keyboard.length - 1];
  assert.equal(last.length, 2);
  assert.match(last[0].text, /Skip/);
  assert.match(last[1].text, /Config/);
});

// =============================================================
// alertButtons.getHeroUrl — surfacing the URL outside the keyboard
// =============================================================

test('getHeroUrl forwards opts (capital + userId + source) to URL', () => {
  const url = buttons.getHeroUrl(shortBasket(), 'SHORT', {
    capital: 1500,
    userId: 'user-AAA',
    source: 'tg-alert',
  });
  assert.ok(url.includes('amount=1500'));
  assert.ok(url.includes(`utm_id=${pearUrl.hashUserId('user-AAA')}`));
  assert.ok(url.includes('utm_source=tg-alert'));
});

// =============================================================
// utm_source sanitization — long sources truncated
// =============================================================

test('utm_source is truncated to 32 chars', () => {
  const long = 'x'.repeat(200);
  const url = pearUrl.buildPearCopyUrl(shortBasket(), 'SHORT', {
    capital: 100,
    source: long,
  });
  // URLSearchParams encodes; we look for the truncated substring
  const m = url.match(/utm_source=([^&]+)/);
  assert.ok(m);
  // each "x" is 1 char in URL — total 32 (no encoding needed for x)
  assert.equal(m[1].length, 32);
});
