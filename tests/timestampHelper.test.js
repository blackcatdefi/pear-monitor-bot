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

test('withTimestamp default position is bottom (italic)', () => {
  const out = withTimestamp('Hello world');
  assert.match(out, /^Hello world\n\n_🕐 .*UTC_$/);
});

test('withTimestamp position=top puts stamp at the start', () => {
  const out = withTimestamp('Body', 'top');
  assert.match(out, /^🕐 .*UTC\n\nBody$/);
});

test('withTimestamp respects TIMESTAMP_ON_MESSAGES kill switch', () => {
  process.env.TIMESTAMP_ON_MESSAGES = 'false';
  const out = withTimestamp('Body');
  assert.equal(out, 'Body');
  process.env.TIMESTAMP_ON_MESSAGES = 'true';
});

test('withTimestamp passes through non-strings', () => {
  process.env.TIMESTAMP_ON_MESSAGES = 'true';
  const out = withTimestamp(42);
  assert.equal(out, 42);
});

test('DAYS_EN and MONTHS_EN are 7 and 12 entries', () => {
  assert.equal(DAYS_EN.length, 7);
  assert.equal(MONTHS_EN.length, 12);
});

test('isEnabled defaults true', () => {
  delete process.env.TIMESTAMP_ON_MESSAGES;
  assert.equal(isEnabled(), true);
});
