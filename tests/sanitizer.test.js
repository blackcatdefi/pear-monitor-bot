'use strict';

const test = require('node:test');
const assert = require('node:assert');

// Set referral env so branding renders the allowed `BlackCatDeFi` string
process.env.PEAR_REFERRAL_CODE = 'BlackCatDeFi';

const sanitizer = require('../src/sanitizer');

test('No NORBER references in any user-facing string', () => {
  const all = sanitizer.collectAllUserFacingStrings();
  const violators = all.filter((s) => /norber/i.test(s));
  if (violators.length > 0) {
    console.error('NORBER violations:', violators);
  }
  assert.strictEqual(violators.length, 0);
});

test('No "tesis del fondo" / "fondo black cat" leakage', () => {
  const all = sanitizer.collectAllUserFacingStrings();
  const tesis = all.filter((s) => /tesis del fondo/i.test(s));
  const fondoBC = all.filter((s) => /fondo black\s*cat/i.test(s));
  assert.strictEqual(tesis.length, 0);
  assert.strictEqual(fondoBC.length, 0);
});

test('BlackCatDeFi only present as referral code in URLs / env names', () => {
  const all = sanitizer.collectAllUserFacingStrings();
  const personalRefs = all.filter(
    (s) => /black\s*cat/i.test(s) && !sanitizer.isAllowedBlackCat(s)
  );
  if (personalRefs.length > 0) {
    console.error('BlackCat-as-persona violations:', personalRefs);
  }
  assert.strictEqual(personalRefs.length, 0);
});

test('No mentions of geopolitical / fund-private terms', () => {
  const all = sanitizer.collectAllUserFacingStrings();
  const banned = [
    /hormuz/i,
    /\biran\b/i,
    /\bbrent\b/i,
    /stage\s*6/i,
    /\bdalio\b/i,
    /druckenmiller/i,
    /\bthiel\b/i,
    /\bruarte\b/i,
    /lady\s*market/i,
    /\blmec\b/i,
    /supergrok/i,
    /\bcowork\b/i,
    /modus operandi/i,
    /war trade/i,
    /\baipear\b/i,
  ];
  for (const re of banned) {
    const hits = all.filter((s) => re.test(s));
    if (hits.length > 0) {
      console.error('Banned term', re, 'hits:', hits);
    }
    assert.strictEqual(hits.length, 0, `Banned term leaked: ${re}`);
  }
});

test('Branding footer still renders the referral code', () => {
  const branding = require('../src/branding');
  const footer = branding.getFooter();
  assert.match(footer, /BlackCatDeFi/);
  assert.match(footer, /referral=BlackCatDeFi/);
});

test('Open-alert basket message renders TWAP entry (not NORBER)', () => {
  const open = require('../src/openAlerts');
  const msg = open.formatBasketOpenAlert('Test wallet', [
    { coin: 'BTC', side: 'SHORT', size: -1, entryPrice: 50000, leverage: 4 },
    { coin: 'ETH', side: 'SHORT', size: -10, entryPrice: 3000, leverage: 4 },
    { coin: 'SOL', side: 'SHORT', size: -100, entryPrice: 150, leverage: 4 },
  ]);
  assert.match(msg, /TWAP entry/);
  assert.doesNotMatch(msg, /NORBER/i);
});

test('External wallet alerts do not mention "fondo" or "BCD"', () => {
  const ext = require('../src/externalWalletTracker');
  const cfg = { address: '0x' + 'a'.repeat(40), label: 'Whale 1' };
  const pos = {
    coin: 'BTC',
    side: 'LONG',
    size: 1,
    entryPx: 50000,
    notional: 50000,
    unrealizedPnl: 100,
  };
  const open = ext.formatExternalOpenAlert(cfg, pos);
  const close = ext.formatExternalCloseAlert(cfg, pos);
  for (const m of [open, close]) {
    assert.doesNotMatch(m, /tesis del fondo/i);
    assert.doesNotMatch(m, /\bfondo\b/i);
    assert.doesNotMatch(m, /\bBCD\b/);
  }
});

test('Compounding alert uses neutral language', () => {
  const cd = require('../src/compoundingDetector');
  const m = cd.formatCompoundAlert('Wallet primaria', {
    type: 'COMPOUND_DETECTED',
    prevNotional: 10000,
    currentNotional: 12000,
    growth: 0.20,
  });
  assert.doesNotMatch(m, /NORBER/i);
  assert.doesNotMatch(m, /\bBCD\b/);
});

test('findForbiddenInString catches violations', () => {
  assert.deepStrictEqual(sanitizer.findForbiddenInString('todo bien'), []);
  assert.ok(
    sanitizer.findForbiddenInString('hola NORBER WAY tu padre').length > 0
  );
  // "Black Cat" inside a URL is allowed
  assert.deepStrictEqual(
    sanitizer.findForbiddenInString(
      'https://app.pear.garden/?referral=BlackCatDeFi'
    ),
    []
  );
});
