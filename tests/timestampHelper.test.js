'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const { formatTimestamp, withTimestamp, isEnabled, DAYS_EN, MONTHS_EN } = require('../src/timestampHelper');

test('formatTimestamp matches expected pattern (English day/month, UTC)', () => {
  const date = new Date(Date.UTC(2026, 3, 30, 12, 15)); // Apr 30 2026 12:15 UTC, Thursday
  const ts = formatTimestamp(date);
  assert.match(ts, /🕐 Thu 30 Apr 2026 - 12:15 UTC/);
});

test('formatTimestamp pads single-digit hours and minutes', () => {
  const date = new Date(Date.UTC(2026, 0, 5, 3, 7)); // 5 Jan 2026 03:07
  const ts = formatTimestamp(date);
  assert.match(ts, / 03:07 UTC$/);
});

test('withTimestamp at position=bottom (italic) when explicitly enabled', () => {
  process.env.TIMESTAMP_ON_MESSAGES = 'true';
  const out = withTimestamp('Hello world');
  assert.match(out, /^Hello world\n\n_🕐 .*UTC_$/);
  delete process.env.TIMESTAMP_ON_MESSAGES;
});

test('withTimestamp position=top puts stamp at the start (when enabled)', () => {
  process.env.TIMESTAMP_ON_MESSAGES = 'true';
  const out = withTimestamp('Body', 'top');
  assert.match(out, /^🕐 .*UTC\n\nBody$/);
  delete process.env.TIMESTAMP_ON_MESSAGES;
});

test('withTimestamp respects TIMESTAMP_ON_MESSAGES kill switch', () => {
  process.env.TIMESTAMP_ON_MESSAGES = 'false';
  const out = withTimestamp('Body');
  assert.equal(out, 'Body');
  delete process.env.TIMESTAMP_ON_MESSAGES;
});

test('withTimestamp passes through non-strings (when enabled)', () => {
  process.env.TIMESTAMP_ON_MESSAGES = 'true';
  const out = withTimestamp(42);
  assert.equal(out, 42);
  delete process.env.TIMESTAMP_ON_MESSAGES;
});

test('DAYS_EN and MONTHS_EN are 7 and 12 entries', () => {
  assert.equal(DAYS_EN.length, 7);
  assert.equal(MONTHS_EN.length, 12);
});

// R-BASKET (3 may 2026): default flipped to OFF — Telegram already shows
// the delivery time on every message, so the hand-rolled footer was pure
// clutter. Operators may opt-in via TIMESTAMP_ON_MESSAGES=true.
test('isEnabled defaults FALSE (R-BASKET footer kill switch)', () => {
  delete process.env.TIMESTAMP_ON_MESSAGES;
  assert.equal(isEnabled(), false);
});

test('isEnabled true when explicitly opted in', () => {
  process.env.TIMESTAMP_ON_MESSAGES = 'true';
  assert.equal(isEnabled(), true);
  delete process.env.TIMESTAMP_ON_MESSAGES;
});
