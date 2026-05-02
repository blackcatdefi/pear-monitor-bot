'use strict';

/**
 * R-NOSPAM (2 may 2026) — sendNotification must forward `opts` to
 * Telegram Bot API. In particular, `disable_notification: true` is what
 * gives us silent push (no sound) for INFORMATIONAL-tier alerts like
 * HyperLend Borrow Available.
 *
 * Pre-R-NOSPAM `sendNotification` ignored `opts` entirely → all alerts
 * vibrated phones equally. These tests pin the contract via source-level
 * regression guards. We avoid loading node-telegram-bot-api directly so
 * the test suite runs in any sandbox (CI, local dev) without npm install
 * of the production-only deps.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');

const SRC_BOT = fs.readFileSync(
  path.join(__dirname, '..', 'src', 'bot.js'), 'utf8'
);
const SRC_MONITOR = fs.readFileSync(
  path.join(__dirname, '..', 'src', 'monitor.js'), 'utf8'
);
const SRC_HEARTBEAT = fs.readFileSync(
  path.join(__dirname, '..', 'src', 'heartbeat.js'), 'utf8'
);

test('R-NOSPAM sendNotification: signature accepts opts (third param)', () => {
  // Pre-R-NOSPAM: `async function sendNotification(chatId, message)`
  // Post-R-NOSPAM: `async function sendNotification(chatId, message, opts = {})`
  // The opts param is the gate that lets disable_notification flow through.
  assert.match(
    SRC_BOT,
    /async\s+function\s+sendNotification\s*\(\s*chatId\s*,\s*message\s*,\s*opts\s*=\s*\{\}\s*\)/,
    'sendNotification must declare `opts = {}` as third param'
  );
});

test('R-NOSPAM sendNotification: legacy `silent: true` is normalized to disable_notification', () => {
  // The heartbeat module historically passed { silent: true } (an internal
  // alias). Telegram Bot API's actual field is `disable_notification`.
  // The normalizer prevents future regressions where opts.silent
  // silently drops on the floor.
  assert.match(
    SRC_BOT,
    /merged\.silent\s*===?\s*true/,
    'sendNotification must check merged.silent === true'
  );
  assert.match(
    SRC_BOT,
    /merged\.disable_notification\s*=\s*true/,
    'sendNotification must set disable_notification = true when silent'
  );
  assert.match(
    SRC_BOT,
    /delete\s+merged\.silent/,
    'sendNotification must delete merged.silent before passing to Telegram'
  );
});

test('R-NOSPAM borrow alert path: monitor.js gates via shouldEmitBorrowAlert', () => {
  // The borrow-alert dispatch must include { disable_notification: true }
  // and route through the persistent gate. These regression guards prevent
  // a future round from silently removing the gate or the silent push flag.
  assert.match(
    SRC_MONITOR,
    /borrowAlertGate/,
    'monitor.js must import the borrowAlertGate'
  );
  assert.match(
    SRC_MONITOR,
    /shouldEmitBorrowAlert/,
    'monitor.js must gate borrow alerts via shouldEmitBorrowAlert'
  );
  assert.match(
    SRC_MONITOR,
    /markAlertEmitted/,
    'monitor.js must persist the alert via markAlertEmitted'
  );
  assert.match(
    SRC_MONITOR,
    /disable_notification:\s*true/,
    'monitor.js must pass disable_notification: true on borrow alerts'
  );
});

test('R-NOSPAM heartbeat: source defaults to disabled', () => {
  // Pin the literal default flip.
  assert.match(
    SRC_HEARTBEAT,
    /HEARTBEAT_ENABLED\s*\|\|\s*['"]false['"]/,
    'heartbeat must default HEARTBEAT_ENABLED to "false" string'
  );
  assert.match(
    SRC_HEARTBEAT,
    /R-NOSPAM/,
    'heartbeat header must reference R-NOSPAM postmortem'
  );
});
