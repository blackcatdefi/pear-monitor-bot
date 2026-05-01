'use strict';

/**
 * R-START — alertButtons.buildAlertKeyboard tests.
 *
 * Validates layout (hero CTA in row 1), referral hidden (only in URL),
 * and configurable copy invitation text.
 */

const test = require('node:test');
const assert = require('node:assert');

process.env.PEAR_REFERRAL_CODE = 'BlackCatDeFi';
process.env.PEAR_BASE_URL = 'https://app.pear.garden';

const alertButtons = require('../src/alertButtons');

const sampleShortBasket = [
  { coin: 'DYDX', side: 'SHORT', notional: 4000 },
  { coin: 'OP', side: 'SHORT', notional: 3000 },
  { coin: 'ARB', side: 'SHORT', notional: 1500 },
];

const sampleMixedBasket = [
  { coin: 'BTC', side: 'LONG', notional: 5000 },
  { coin: 'DYDX', side: 'SHORT', notional: 4000 },
];

test('alert keyboard tiene CTA Pear como primer botón en SHORT-only basket', () => {
  const kb = alertButtons.buildAlertKeyboard(sampleShortBasket, 'open');
  assert.ok(kb, 'keyboard should not be null');
  const firstBtn = kb.inline_keyboard[0][0];
  assert.match(firstBtn.text, /Copiar en Pear/);
  assert.match(firstBtn.url, /referral=BlackCatDeFi/);
});

test('mixed-side basket emite 2 hero buttons (SHORTs + LONGs)', () => {
  const kb = alertButtons.buildAlertKeyboard(sampleMixedBasket, 'open');
  assert.ok(kb);
  // Two hero rows when basket has both sides.
  const labels = kb.inline_keyboard.flat().map((b) => b.text);
  assert.ok(labels.some((l) => /SHORTs en Pear/.test(l)));
  assert.ok(labels.some((l) => /LONGs en Pear/.test(l)));
});

test('hero button labels NUNCA mencionan referral o BlackCat en texto', () => {
  const kb = alertButtons.buildAlertKeyboard(sampleShortBasket, 'open');
  for (const row of kb.inline_keyboard) {
    for (const btn of row) {
      assert.doesNotMatch(btn.text, /referral/i);
      assert.doesNotMatch(btn.text, /BlackCat/i);
    }
  }
});

test('mute callback aparece en row 2 cuando se pasa wallet', () => {
  const wallet = '0xABCDEF0123456789ABCDEF0123456789ABCDEF01';
  const kb = alertButtons.buildAlertKeyboard(sampleShortBasket, 'open', {
    wallet,
  });
  // Find the mute button (callback_data starting with 'mute:')
  const muteBtn = kb.inline_keyboard
    .flat()
    .find(
      (b) => b.callback_data && b.callback_data.startsWith('mute:')
    );
  assert.ok(muteBtn, 'mute button missing');
  assert.match(muteBtn.text, /Silenciar wallet/);
  assert.strictEqual(
    muteBtn.callback_data,
    `mute:${wallet.toLowerCase()}`
  );
});

test('keyboard sin positions retorna null', () => {
  const kb = alertButtons.buildAlertKeyboard([], 'open');
  assert.strictEqual(kb, null);
});

test('close-event keyboard NO incluye copy CTA (solo silenciar)', () => {
  const wallet = '0x' + '1'.repeat(40);
  const kb = alertButtons.buildAlertKeyboard(sampleShortBasket, 'close', {
    wallet,
  });
  assert.ok(kb);
  const labels = kb.inline_keyboard.flat().map((b) => b.text);
  assert.ok(!labels.some((l) => /Copiar en Pear/.test(l)));
  assert.ok(labels.some((l) => /Silenciar wallet/.test(l)));
});

test('getCopyCtaText respeta env override', () => {
  const original = process.env.COPY_CTA_TEXT;
  process.env.COPY_CTA_TEXT = '🔥 Copiá esto ya:';
  assert.strictEqual(alertButtons.getCopyCtaText(), '🔥 Copiá esto ya:');
  if (original == null) delete process.env.COPY_CTA_TEXT;
  else process.env.COPY_CTA_TEXT = original;
});

test('getCopyCtaText default cuando env no seteada', () => {
  const original = process.env.COPY_CTA_TEXT;
  delete process.env.COPY_CTA_TEXT;
  assert.strictEqual(
    alertButtons.getCopyCtaText(),
    alertButtons.DEFAULT_COPY_CTA
  );
  if (original != null) process.env.COPY_CTA_TEXT = original;
});

test('hero URL configurable via PEAR_BASE_URL', () => {
  // Pear URL builder reads its base URL from env at import-time, so we
  // just check the buildAlertKeyboard output uses it correctly.
  const kb = alertButtons.buildAlertKeyboard(sampleShortBasket, 'open');
  const heroBtn = kb.inline_keyboard[0][0];
  assert.match(heroBtn.url, /^https:\/\/app\.pear\.garden\/trade\/hl\/USDC-/);
});
