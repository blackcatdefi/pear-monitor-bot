'use strict';

/**
 * R-EN — Spanish-leak detector.
 *
 * Walks src/i18n/en.js and asserts that no string in the canonical English
 * dictionary contains Spanish characters or idiomatic markers.
 *
 * Also walks every collected user-facing string emitted by the running bot
 * (via sanitizer.collectAllUserFacingStrings) and asserts the same.
 *
 * Excluded from the scan:
 *   • src/legacy_es.js (kept for reference only — never imported)
 *   • src/i18n.js (backward-compat shim)
 *   • Lines inside JSDoc / // comments
 *   • The bilingual signal parser regexes
 */

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

process.env.PEAR_REFERRAL_CODE = 'BlackCatDeFi';

const { strictSpanishRegex, collectAllStrings } = require('../src/i18n/index');
const sanitizer = require('../src/sanitizer');

test('en.js dictionary has zero Spanish-character leaks', () => {
  const all = collectAllStrings();
  const violators = [];
  const re = strictSpanishRegex();
  for (const { key, value } of all) {
    if (re.test(value)) {
      violators.push({ key, value });
    }
  }
  if (violators.length > 0) {
    console.error('Spanish leaks in en.js:', violators);
  }
  assert.strictEqual(violators.length, 0);
});

test('Runtime user-facing strings have zero Spanish leaks', () => {
  const all = sanitizer.collectAllUserFacingStrings();
  const re = strictSpanishRegex();
  const violators = all.filter((s) => re.test(s));
  if (violators.length > 0) {
    // Print first 25 to avoid flooding logs
    console.error('Spanish leaks in runtime strings:', violators.slice(0, 25));
  }
  assert.strictEqual(violators.length, 0);
});

test('Source files (excluding legacy/comment/regex/test) have no Spanish characters in user-facing literals', () => {
  // We scan source-level string LITERALS (single, double, template-tick).
  // Comment lines are stripped first. legacy_es.js + i18n.js (shim) +
  // signalsParser.js (bilingual regex by design) excluded.
  const SRC_DIR = path.join(__dirname, '..', 'src');
  const EXCLUDE_FILES = new Set([
    'legacy_es.js',
    'i18n.js',
    'signalsParser.js', // bilingual regex literal "activaci[oó]n" is intentional
    'sanitizer.js',     // contains spanish in BlackCat detection comments
  ]);
  const files = fs
    .readdirSync(SRC_DIR)
    .filter(
      (f) =>
        f.endsWith('.js') &&
        !f.endsWith('.test.js') &&
        !EXCLUDE_FILES.has(f)
    );
  // Pure Spanish-character regex (no English false-positives like "configurable")
  const SPANISH_CHARS = /[áéíóúñ¿¡]/;
  // Common Spanish idioms used inside strings
  const SPANISH_WORDS = /\b(usá|tocá|querés|podés|recibís|tenés|escribí|elegí|setear|próximo|último|Posición|Posiciones|Wallets propias|Bienvenido|Bienvenida)\b/;

  const violators = [];
  for (const f of files) {
    const txt = fs.readFileSync(path.join(SRC_DIR, f), 'utf-8');
    // Strip block comments + line comments
    const noComments = txt
      .replace(/\/\*[\s\S]*?\*\//g, '')
      .replace(/(^|[^:])\/\/.*$/gm, '$1');
    const regexes = [
      /"([^"\\\n]*(?:\\.[^"\\\n]*)*)"/g,
      /'([^'\\\n]*(?:\\.[^'\\\n]*)*)'/g,
      /`([^`\\]*(?:\\.[^`\\]*)*)`/g,
    ];
    for (const re of regexes) {
      let m;
      while ((m = re.exec(noComments)) !== null) {
        const lit = m[1];
        if (lit.length < 3) continue;
        if (SPANISH_CHARS.test(lit) || SPANISH_WORDS.test(lit)) {
          violators.push({ file: f, lit });
        }
      }
    }
  }
  if (violators.length > 0) {
    console.error('Spanish-literal leaks in src/:', violators.slice(0, 25));
  }
  assert.strictEqual(violators.length, 0);
});

test('legacy_es.js is NOT required by any active src file', () => {
  const SRC_DIR = path.join(__dirname, '..', 'src');
  const files = fs
    .readdirSync(SRC_DIR)
    .filter((f) => f.endsWith('.js') && !f.endsWith('.test.js') && f !== 'legacy_es.js');
  const violators = [];
  for (const f of files) {
    const txt = fs.readFileSync(path.join(SRC_DIR, f), 'utf-8');
    if (/require\s*\(\s*['"`]\.\/legacy_es['"`]\s*\)/.test(txt)) {
      violators.push(f);
    }
  }
  if (violators.length > 0) {
    console.error('Files that still import legacy_es.js:', violators);
  }
  assert.strictEqual(violators.length, 0);
});

test('i18n shim (src/i18n.js) does NOT require legacy_es', () => {
  const txt = fs.readFileSync(path.join(__dirname, '..', 'src', 'i18n.js'), 'utf-8');
  // Only check for executable require() — comments mentioning the file name
  // are fine (and helpful as historical context).
  assert.doesNotMatch(txt, /require\s*\(\s*['"`]\.\/legacy_es['"`]/);
});

test('en.js exports nested-object structure (not a flat string-keyed dict)', () => {
  const en = require('../src/i18n/en');
  // Spot check: nested keys must exist
  assert.strictEqual(typeof en.start, 'object');
  assert.strictEqual(typeof en.track, 'object');
  assert.strictEqual(typeof en.timezone, 'object');
  assert.strictEqual(typeof en.alerts, 'object');
  // Sentinel English string must be in the nested map
  assert.match(en.start.recurring_welcome, /Welcome back/i);
  assert.match(en.alerts.NEW_BASKET, /NEW BASKET OPENED/i);
});

test('strictSpanishRegex catches obvious Spanish but not "configurable"/"position"', () => {
  const re = strictSpanishRegex();
  // Should catch
  assert.ok(re.test('Conectá tu wallet'));
  assert.ok(re.test('Bienvenido a Pear'));
  assert.ok(re.test('podés copiar este trade'));
  assert.ok(re.test('configuración rápida'));
  // Should NOT catch (English with similar substrings)
  assert.ok(!re.test('Configurable via env var'));
  assert.ok(!re.test('Position closed'));
  assert.ok(!re.test('Connect your wallet'));
  assert.ok(!re.test('1 position opened'));
});
