'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const {
  canSendAlert,
  getStats,
  MAX_ALERTS_PER_MINUTE,
  _resetForTests,
} = require('../src/rateLimiter');

test('canSendAlert: allows up to MAX_ALERTS_PER_MINUTE', () => {
  _resetForTests();
  for (let i = 0; i < MAX_ALERTS_PER_MINUTE; i += 1) {
    assert.equal(canSendAlert(), true, `alert ${i + 1} should pass`);
  }
});

test('canSendAlert: drops on the (MAX+1)-th in the same minute', () => {
  _resetForTests();
  for (let i = 0; i < MAX_ALERTS_PER_MINUTE; i += 1) canSendAlert();
  const restoreWarn = console.warn;
  console.warn = () => {}; // silence the limiter warning during test
  try {
    assert.equal(canSendAlert(), false);
  } finally {
    console.warn = restoreWarn;
  }
});

test('getStats reports current count and lifetime drops', () => {
  _resetForTests();
  for (let i = 0; i < 5; i += 1) canSendAlert();
  const s = getStats();
  assert.equal(s.alerts_last_60s, 5);
  assert.equal(s.cap_per_minute, MAX_ALERTS_PER_MINUTE);
  assert.ok(s.total_dropped_lifetime >= 0);
});

test('canSendAlert: 50 burst → exactly MAX pass, rest dropped', () => {
  _resetForTests();
  const restoreWarn = console.warn;
  console.warn = () => {};
  let passed = 0, dropped = 0;
  try {
    for (let i = 0; i < 50; i += 1) {
      if (canSendAlert()) passed += 1; else dropped += 1;
    }
  } finally {
    console.warn = restoreWarn;
  }
  assert.equal(passed, MAX_ALERTS_PER_MINUTE);
  assert.equal(dropped, 50 - MAX_ALERTS_PER_MINUTE);
});

test('MAX_ALERTS_PER_MINUTE matches env or default 20', () => {
  const expected = parseInt(process.env.RATE_LIMIT_ALERTS_PER_MINUTE || '20', 10);
  assert.equal(MAX_ALERTS_PER_MINUTE, expected);
});
