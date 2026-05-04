'use strict';

/**
 * R-PUBLIC-BASKET-SPAM-NUCLEAR (4 may 2026) regression suite.
 *
 * Root incident: BCD opened a single Pear basket of 18 legs at 11:32 UTC and
 * the public bot emitted 31 messages in 5 minutes (18 legacy `📈 New position
 * opened` per-leg + 1 correct `🚀 NEW BASKET OPENED` + 1 duplicate DYDX +
 * 9 phantom `📋 Manual close $0.00` + 1 phantom basket re-open + 1 phantom
 * `🐱‍⬛ BASKET CLOSED $0.00`).
 *
 * Single-handler enforcement: monitor.js MUST NOT contain any legacy emit
 * pattern. The only OPEN emitter is extensions.js → openAlerts.js. The only
 * CLOSE emitter is basketEngine → walletTrackerScheduler / externalWalletTracker
 * → messageFormattersV2 (with `isCloseEmittable` phantom-zero gate).
 *
 * If any of these greps come back non-empty against monitor.js, the spam
 * regression is back.
 */

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');

const MONITOR_PATH = path.join(__dirname, '..', 'src', 'monitor.js');

function readMonitor() {
  return fs.readFileSync(MONITOR_PATH, 'utf8');
}

function countMatchesInExecutableLines(src, regex) {
  // Strip block comments and line comments before matching, so commented-out
  // mentions of the legacy strings (in the post-fix forensic comment) do
  // NOT trigger a false positive. This function is intentionally simple
  // and not a full parser — fine for our shapes.
  const stripped = src
    // /* ... */ (single-line and multi-line)
    .replace(/\/\*[\s\S]*?\*\//g, '')
    // // ... to end-of-line
    .replace(/(^|[^:])\/\/[^\n]*/g, '$1');
  const m = stripped.match(regex);
  return m ? m.length : 0;
}

test('monitor.js: no legacy "📈 New position opened" emit (Bug A)', () => {
  const src = readMonitor();
  // Match the exact legacy emoji + label combination that produced the 18
  // per-leg messages on 4 may. Allow it to appear inside comments only.
  const count = countMatchesInExecutableLines(
    src,
    /📈\s*\*?New position opened/g
  );
  assert.strictEqual(
    count,
    0,
    `monitor.js still contains ${count} executable reference(s) to the legacy "📈 New position opened" alert. ` +
      `OPEN alerts must be emitted exclusively from extensions.js → openAlerts.js.`
  );
});

test('monitor.js: no legacy formatCloseAlert call', () => {
  const src = readMonitor();
  const count = countMatchesInExecutableLines(src, /formatCloseAlert\s*\(/g);
  assert.strictEqual(
    count,
    0,
    `monitor.js still calls formatCloseAlert(). CLOSE alerts must come from ` +
      `basketEngine → messageFormattersV2 with the isCloseEmittable phantom-zero gate.`
  );
});

test('monitor.js: no legacy trackCloseForBasket call', () => {
  const src = readMonitor();
  const count = countMatchesInExecutableLines(
    src,
    /trackCloseForBasket\s*\(/g
  );
  assert.strictEqual(
    count,
    0,
    `monitor.js still calls trackCloseForBasket(). The basket summary path is ` +
      `now exclusively basketEngine.processSnapshot → BASKET_CLOSE.`
  );
});

test('monitor.js: no legacy formatBasketSummary call', () => {
  const src = readMonitor();
  const count = countMatchesInExecutableLines(
    src,
    /formatBasketSummary\s*\(/g
  );
  assert.strictEqual(
    count,
    0,
    `monitor.js still calls formatBasketSummary(). The synthesized "🐱‍⬛ BASKET CLOSED $0.00" ` +
      `phantom message comes from this codepath — must remain absent.`
  );
});

test('monitor.js: no `notify(` call in the new-position loop', () => {
  // Cheap proxy: there should be NO occurrences of `await this.notify(` or
  // `this.notify(` inside `for (const pos of allPositions)` blocks. The only
  // surviving notify in monitor.js is the funds-available alert (gated by
  // shouldFireFundsAvailable) and the borrow alert (gated by borrowAlertGate).
  const src = readMonitor();
  // Strip comments first so commented examples don't count.
  const stripped = src
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:])\/\/[^\n]*/g, '$1');
  // Find the for-loop body for `for (const pos of allPositions)`.
  // The `if (!ws.positions[key])` block is what used to wrap the legacy
  // notify. Verify no `notify(` appears inside that block.
  const m = stripped.match(
    /if \(!ws\.positions\[key\]\) \{[\s\S]*?\n\s*\}/
  );
  if (!m) {
    // Block shape changed enough that we can't isolate it. Don't fail on
    // refactor — but flag in CI so a human sees this and decides if the
    // greps above still cover it.
    console.warn(
      '[regression] could not isolate ws.positions[key] block; ' +
        'static greps above are still authoritative'
    );
    return;
  }
  assert.ok(
    !/this\.notify\s*\(/.test(m[0]),
    'monitor.js: a notify() call has reappeared inside the new-position branch. ' +
      'OPEN alerts must come from extensions.js → openAlerts.js, not monitor.js.'
  );
});

test('monitor.js: closeAlerts.js helpers are NOT imported', () => {
  const src = readMonitor();
  // Must not require('./closeAlerts') in any executable form.
  const stripped = src
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:])\/\/[^\n]*/g, '$1');
  assert.ok(
    !/require\s*\(\s*['"]\.\/closeAlerts['"]\s*\)/.test(stripped),
    "monitor.js must not require('./closeAlerts') — those helpers are " +
      'the legacy emit path that produced the spam.'
  );
});
