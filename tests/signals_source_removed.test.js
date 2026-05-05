'use strict';

/**
 * R-PUBLIC-V4-COPYMENU — surface-removal regression suite.
 *
 * Once the V4 copy menu shipped, the BCD_SIGNALS source path was killed
 * for good. This test prevents the dead surface from being re-introduced
 * by future refactors / merges. It is a structural test — it does not
 * exercise runtime behavior, just the absence of the deleted modules
 * and exports.
 *
 * Asserts:
 *   1. Source files for the legacy signals path are gone:
 *        src/signalsChannel.js
 *        src/signalsChannelScraper.js
 *        src/signalsParser.js
 *        src/scraperFetch.js
 *        src/gramjsBackend.js
 *        src/commandsSignals.js
 *   2. copyTradingStore.VALID_TYPES is exactly ['BCD_WALLET','CUSTOM_WALLET']
 *      and TYPE_BCD_SIGNALS is undefined.
 *   3. copyTrading does NOT export dispatchSignalToSubscribers or
 *      onSignalParsed (the legacy hooks).
 *   4. extensions.js does not require any of the deleted signals modules.
 *   5. bot.js setMyCommands does not include a 'signals' command.
 *   6. No remaining src file imports a deleted signals module.
 */

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const SRC_DIR = path.join(__dirname, '..', 'src');

const REMOVED_FILES = [
  'signalsChannel.js',
  'signalsChannelScraper.js',
  'signalsParser.js',
  'scraperFetch.js',
  'gramjsBackend.js',
  'commandsSignals.js',
];

// 1. All deleted-source files must NOT exist.
test('R-PUBLIC-V4 — deleted signals source files do not exist', () => {
  for (const f of REMOVED_FILES) {
    const p = path.join(SRC_DIR, f);
    assert.strictEqual(
      fs.existsSync(p),
      false,
      `Removed module reappeared: ${f}`
    );
  }
});

// 2. copyTradingStore exposes only the 2 surviving types.
test('R-PUBLIC-V4 — VALID_TYPES has exactly BCD_WALLET + CUSTOM_WALLET', () => {
  const store = require('../src/copyTradingStore');
  assert.deepStrictEqual(
    [...store.VALID_TYPES].sort(),
    ['BCD_WALLET', 'CUSTOM_WALLET']
  );
  assert.strictEqual(store.TYPE_BCD_SIGNALS, undefined);
});

// 3. copyTrading does NOT export legacy signals dispatch hooks.
test('R-PUBLIC-V4 — copyTrading has no dispatchSignalToSubscribers', () => {
  const ct = require('../src/copyTrading');
  assert.strictEqual(typeof ct.dispatchSignalToSubscribers, 'undefined');
  assert.strictEqual(typeof ct.onSignalParsed, 'undefined');
});

// 4. extensions.js: no require of any deleted module.
test('R-PUBLIC-V4 — extensions.js does not import any deleted signals module', () => {
  const txt = fs.readFileSync(path.join(SRC_DIR, 'extensions.js'), 'utf-8');
  for (const f of REMOVED_FILES) {
    const stem = f.replace(/\.js$/, '');
    const re = new RegExp(`require\\s*\\(\\s*['"\`]\\.\/${stem}['"\`]`, 'i');
    assert.doesNotMatch(txt, re, `extensions.js still imports ${f}`);
  }
});

// 5. bot.js setMyCommands does not include 'signals'.
test('R-PUBLIC-V4 — bot.js setMyCommands does not include a "signals" command', () => {
  const txt = fs.readFileSync(path.join(SRC_DIR, 'bot.js'), 'utf-8');
  // Strip block + line comments first so historical references in JSDoc
  // are not flagged.
  const noComments = txt
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:])\/\/.*$/gm, '$1');
  assert.doesNotMatch(
    noComments,
    /command:\s*['"]signals['"]/,
    'bot.js still registers a /signals command'
  );
});

// 6. Sweep the whole src/ tree to make sure nothing imports deleted modules.
test('R-PUBLIC-V4 — no active src file imports a deleted signals module', () => {
  const files = fs
    .readdirSync(SRC_DIR)
    .filter((f) => f.endsWith('.js') && !f.endsWith('.test.js'));
  const violators = [];
  for (const f of files) {
    const txt = fs.readFileSync(path.join(SRC_DIR, f), 'utf-8');
    const noComments = txt
      .replace(/\/\*[\s\S]*?\*\//g, '')
      .replace(/(^|[^:])\/\/.*$/gm, '$1');
    for (const removed of REMOVED_FILES) {
      const stem = removed.replace(/\.js$/, '');
      const re = new RegExp(
        `require\\s*\\(\\s*['"\`]\\.\/${stem}['"\`]`,
        'i'
      );
      if (re.test(noComments)) {
        violators.push({ file: f, deletedImport: stem });
      }
    }
  }
  assert.deepStrictEqual(
    violators,
    [],
    `Active src files still import deleted modules: ${JSON.stringify(violators)}`
  );
});

// 7. /copy_trading top menu copy must not mention signals scraping.
test('R-PUBLIC-V4 — top menu copy never says "signals" except informational', () => {
  // Defensive: make sure nothing in the V4 menu render path references
  // the legacy scraper.
  const cmd = require('../src/commandsCopyTrading');
  const m = cmd._renderTopMenu(123456);
  assert.ok(!/signals?\s+(channel|scraper|posts?|parser)/i.test(m.text),
    `top-menu copy must not reference signals scraping: ${m.text}`);
});
