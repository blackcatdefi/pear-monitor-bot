'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const { formatTimestamp, withTimestamp, isEnabled, DAYS_ES, MONTHS_ES } = require('../src/timestampHelper');

test('formatTimestamp matches expected pattern (Spanish day/month, UTC)', () => {
  const date = new Date(Date.UTC(2026, 3, 30, 12, 15)); // Apr 30 2026 12:15 UTC, jueves
  const ts = formatTimestamp(date);
  assert.match(ts, /🕐 jue 30 abr 2026 - 12:15 UTC/);
});

test('formatTimestamp pads single-digit hours and minutes', () => {
  const date = new Date(Date.UTC(2026, 0, 5, 3, 7)); // 5 ene 2026 03:07
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

test('DAYS_ES and MONTHS_ES are 7 and 12 entries', () => {
  assert.equal(DAYS_ES.length, 7);
  assert.equal(MONTHS_ES.length, 12);
});

test('isEnabled defaults true', () => {
  delete process.env.TIMESTAMP_ON_MESSAGES;
  assert.equal(isEnabled(), true);
});
