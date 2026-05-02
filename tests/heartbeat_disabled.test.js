'use strict';

const test = require('node:test');
const assert = require('node:assert');

// R-NOSPAM (2 may 2026): heartbeat must default to DISABLED so a fresh
// deploy never wakes a public-bot user with a "✅ Pear Alerts Bot online"
// broadcast. Operators can still opt in via HEARTBEAT_ENABLED=true.

function freshRequire() {
  delete require.cache[require.resolve('../src/heartbeat')];
  return require('../src/heartbeat');
}

test('R-NOSPAM heartbeat: disabled by default with no env vars', () => {
  delete process.env.HEARTBEAT_ENABLED;
  const heartbeat = freshRequire();
  assert.strictEqual(heartbeat.isEnabled(), false,
    'heartbeat must be disabled by default per R-NOSPAM');
});

test('R-NOSPAM heartbeat: HEARTBEAT_ENABLED=false stays disabled', () => {
  process.env.HEARTBEAT_ENABLED = 'false';
  const heartbeat = freshRequire();
  assert.strictEqual(heartbeat.isEnabled(), false);
  delete process.env.HEARTBEAT_ENABLED;
});

test('R-NOSPAM heartbeat: HEARTBEAT_ENABLED=true opts in (operator override)', () => {
  process.env.HEARTBEAT_ENABLED = 'true';
  const heartbeat = freshRequire();
  assert.strictEqual(heartbeat.isEnabled(), true);
  delete process.env.HEARTBEAT_ENABLED;
});

test('R-NOSPAM heartbeat: startSchedule returns null when disabled (no setInterval)', () => {
  delete process.env.HEARTBEAT_ENABLED;
  const heartbeat = freshRequire();
  let called = false;
  const fakeNotify = async () => { called = true; };
  const timer = heartbeat.startSchedule(fakeNotify, 12345);
  assert.strictEqual(timer, null,
    'startSchedule must return null when heartbeat is disabled');
  assert.strictEqual(called, false,
    'notifier must not be invoked when heartbeat is disabled');
});

test('R-NOSPAM heartbeat: sendHeartbeat short-circuits when disabled (no notifier call)', async () => {
  delete process.env.HEARTBEAT_ENABLED;
  const heartbeat = freshRequire();
  let called = false;
  const fakeNotify = async () => { called = true; return true; };
  const result = await heartbeat.sendHeartbeat(fakeNotify, 12345);
  assert.strictEqual(result, false,
    'sendHeartbeat must return false when disabled');
  assert.strictEqual(called, false,
    'notifier must not be invoked when heartbeat is disabled');
});

test('R-NOSPAM heartbeat: when explicitly enabled, sendHeartbeat passes silent flag', async () => {
  process.env.HEARTBEAT_ENABLED = 'true';
  const heartbeat = freshRequire();
  let receivedOpts = null;
  const fakeNotify = async (chatId, msg, opts) => {
    receivedOpts = opts;
    return true;
  };
  const result = await heartbeat.sendHeartbeat(fakeNotify, 12345);
  assert.strictEqual(result, true);
  assert.ok(receivedOpts, 'opts must be passed');
  assert.strictEqual(receivedOpts.silent, true,
    'opts.silent must be true');
  assert.strictEqual(receivedOpts.disable_notification, true,
    'opts.disable_notification must be true so even opt-in heartbeats do not wake users');
  delete process.env.HEARTBEAT_ENABLED;
});
