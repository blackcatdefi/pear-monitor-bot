'use strict';

/**
 * R-PUBLIC-START-NUCLEAR — regression guard for healthServer Telegram
 * polling telemetry. Future regressions of curl /health should fail this
 * test before they reach Railway.
 */

const test = require('node:test');
const assert = require('node:assert/strict');

const hs = require('../src/healthServer');

test('healthServer telegram telemetry — exposes new fields', () => {
  hs._resetForTests();
  const s1 = hs.getStatus();
  assert.ok(s1.telegram, 'telegram block present');
  assert.equal(s1.telegram.updates_lifetime, 0, 'lifetime starts at 0');
  assert.equal(s1.telegram.last_update_at, null, 'no updates yet');
  assert.equal(s1.telegram.last_start_command_at, null, 'no /start yet');
  assert.equal(s1.telegram.polling_started_at, null, 'polling not started');
  assert.deepEqual(s1.telegram.registered_handlers, [], 'no handlers yet');
});

test('healthServer telegram telemetry — recordTelegramUpdate increments', () => {
  hs._resetForTests();
  hs.recordTelegramUpdate({ message_id: 1 });
  hs.recordTelegramUpdate({ message_id: 2 });
  hs.recordTelegramUpdate({ message_id: 3 });
  const s = hs.getStatus();
  assert.equal(s.telegram.updates_lifetime, 3);
  assert.ok(s.telegram.last_update_at, 'timestamp set');
  assert.ok(s.telegram.last_update_age_ms < 1000, 'age fresh');
});

test('healthServer telegram telemetry — recordStartCommand stores user', () => {
  hs._resetForTests();
  hs.recordStartCommand(1901156709);
  const s = hs.getStatus();
  assert.equal(s.telegram.last_start_command_from_user_id, '1901156709');
  assert.ok(s.telegram.last_start_command_at, 'timestamp set');
});

test('healthServer telegram telemetry — recordPollingStarted', () => {
  hs._resetForTests();
  hs.recordPollingStarted();
  const s = hs.getStatus();
  assert.ok(s.telegram.polling_started_at, 'polling start recorded');
});

test('healthServer telegram telemetry — registerHandler dedup', () => {
  hs._resetForTests();
  hs.registerHandler('start');
  hs.registerHandler('track');
  hs.registerHandler('start'); // duplicate, should not double
  const s = hs.getStatus();
  assert.deepEqual(s.telegram.registered_handlers.sort(), ['start', 'track']);
  assert.equal(s.telegram.handlers_count, 2);
});

test('healthServer — no exception when called with null userId', () => {
  hs._resetForTests();
  assert.doesNotThrow(() => hs.recordStartCommand(null));
  const s = hs.getStatus();
  assert.equal(s.telegram.last_start_command_from_user_id, null);
});
