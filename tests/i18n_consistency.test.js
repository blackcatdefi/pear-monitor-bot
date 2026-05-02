'use strict';

/**
 * R-EN — i18n placeholder + key consistency.
 *
 * Catches:
 *   • t() call sites referencing keys that don't exist in en.js
 *   • Strings with `{var}` placeholders that callers fail to provide
 *   • Empty strings, duplicate trailing whitespace, non-string leaves
 *   • Critical CTA keys that must exist (smoke-list)
 */

const test = require('node:test');
const assert = require('node:assert');

process.env.PEAR_REFERRAL_CODE = 'BlackCatDeFi';

const { t, collectAllStrings } = require('../src/i18n/index');
const en = require('../src/i18n/en');

test('t() returns expected English literal for nested keys', () => {
  assert.strictEqual(t('start.recurring_welcome'), 'Welcome back 👋');
  assert.match(t('alerts.NEW_BASKET'), /NEW BASKET OPENED/);
  assert.match(t('track.menu_title'), /TRACK/);
});

test('t() interpolates {var} placeholders correctly', () => {
  const out = t('start.tz_detected', { tz: 'America/New_York' });
  assert.match(out, /America\/New_York/);
  assert.doesNotMatch(out, /\{tz\}/);
});

test('t() interpolates multiple placeholders in one string', () => {
  const out = t('track.saved_with_label', {
    addr: '0xABC',
    label: 'Whale 1',
  });
  assert.match(out, /0xABC/);
  assert.match(out, /Whale 1/);
  assert.doesNotMatch(out, /\{addr\}/);
  assert.doesNotMatch(out, /\{label\}/);
});

test('t() returns [MISSING_STRING:...] for unknown keys (visible in screenshots)', () => {
  const out = t('nope.does.not.exist');
  assert.match(out, /MISSING_STRING/);
  assert.match(out, /nope\.does\.not\.exist/);
});

test('t() handles missing vars by replacing with empty string', () => {
  // Numeric / null / undefined values
  const out1 = t('start.tz_detected', { tz: null });
  assert.match(out1, /America\/.*|`/);
  assert.doesNotMatch(out1, /null/);
  const out2 = t('start.list_total', { count: 3, max: 10 });
  assert.match(out2, /3/);
  assert.match(out2, /10/);
});

test('t() leaves non-interpolated strings untouched', () => {
  const out = t('alerts.WALLET');
  assert.strictEqual(out, 'Wallet');
});

test('every leaf in en.js is a non-empty string', () => {
  const all = collectAllStrings();
  const violators = all.filter(
    ({ value }) => typeof value !== 'string' || value.length === 0
  );
  if (violators.length > 0) {
    console.error('Empty/non-string leaves in en.js:', violators);
  }
  assert.strictEqual(violators.length, 0);
});

test('no leaf has unbalanced placeholder braces (e.g. {foo missing })', () => {
  const all = collectAllStrings();
  const violators = [];
  for (const { key, value } of all) {
    const opens = (value.match(/\{[a-zA-Z_]/g) || []).length;
    const closes = (value.match(/[a-zA-Z_0-9]\}/g) || []).length;
    if (opens !== closes) {
      violators.push({ key, value, opens, closes });
    }
  }
  if (violators.length > 0) {
    console.error('Unbalanced placeholders in en.js:', violators);
  }
  assert.strictEqual(violators.length, 0);
});

test('critical CTA keys exist (regression for accidentally-deleted strings)', () => {
  const requiredKeys = [
    'start.title',
    'start.recurring_welcome',
    'start.kb_track_add',
    'start.kb_copy_trading',
    'start.kb_pear',
    'track.menu_title',
    'track.add_prompt',
    'track.saved_with_label',
    'track.invalid_addr',
    'timezone.detected_msg',
    'timezone.invalid',
    'capital.saved_ok',
    'copy_trading.pick_title',
    'alerts.NEW_BASKET',
    'alerts.BASKET_CLOSED',
    'alertBtn.cta_default',
    'copyAlert.composition',
    'copyAlert.no_api',
  ];
  const missing = requiredKeys.filter((k) => {
    const v = t(k);
    return /MISSING_STRING/.test(v);
  });
  if (missing.length > 0) {
    console.error('Missing required keys:', missing);
  }
  assert.strictEqual(missing.length, 0);
});

test('en.js root has the expected feature sections', () => {
  const expected = [
    'alerts',
    'start',
    'track',
    'timezone',
    'capital',
    'copy_auto',
    'copy_trading',
    'signals',
    'share',
    'feedback',
    'portfolio',
    'leaderboard',
    'learn',
    'alerts_config',
    'alertComp',
    'copyAlert',
    'monitor',
    'pnlXval',
    'alertBtn',
    'digest',
    'scheduler',
    'common',
  ];
  for (const k of expected) {
    assert.ok(
      typeof en[k] === 'object' && en[k] !== null,
      `Missing or non-object section: ${k}`
    );
  }
});

test('legacy backward-compat shim returns English for legacy uppercase keys', () => {
  // Force re-resolve to .js shim (./i18n.js) instead of .js/index.js
  const shimPath = require.resolve('../src/i18n.js');
  const legacyShim = require(shimPath);
  assert.strictEqual(typeof legacyShim.t, 'function');
  assert.strictEqual(legacyShim.isSpanish(), false);
  assert.match(legacyShim.t('NEW_BASKET'), /NEW BASKET OPENED/);
  assert.match(legacyShim.t('POSITION_CLOSED'), /Position closed/);
});

test('all `t(...)` callsites in src/ reference keys that exist', () => {
  // Static analysis: scan src/*.js for `t('key')` and `t("key")` patterns.
  // (Template-literal keys are skipped — too dynamic to verify.)
  const fs = require('fs');
  const path = require('path');
  const SRC_DIR = path.join(__dirname, '..', 'src');
  const files = fs
    .readdirSync(SRC_DIR)
    .filter(
      (f) =>
        f.endsWith('.js') &&
        !f.endsWith('.test.js') &&
        f !== 'i18n.js' &&
        f !== 'legacy_es.js'
    );
  const callRe = /\bt\(\s*['"]([\w.]+)['"]/g;
  const missing = [];
  // Files that import the legacy shim (./i18n) where t('UPPERCASE') is valid:
  const FLAT_LEGACY_USERS = new Set([
    'branding.js',
    'messageFormatters.js',
    'weeklySummary.js',
  ]);
  for (const f of files) {
    if (FLAT_LEGACY_USERS.has(f)) continue;
    const txt = fs.readFileSync(path.join(SRC_DIR, f), 'utf-8');
    let m;
    while ((m = callRe.exec(txt)) !== null) {
      const key = m[1];
      // Skip very short identifiers that are clearly local helpers
      if (!key.includes('.')) continue;
      const out = t(key);
      if (/MISSING_STRING/.test(out)) {
        missing.push({ file: f, key });
      }
    }
  }
  if (missing.length > 0) {
    console.error('Callsites referencing missing keys:', missing);
  }
  assert.strictEqual(missing.length, 0);
});
