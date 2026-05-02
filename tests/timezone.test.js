'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');

// Sandbox the JSON store under a tmp dir per-run.
const TMP_DIR = fs.mkdtempSync(path.join(os.tmpdir(), 'tz-test-'));
process.env.USER_TZ_DB_PATH = path.join(TMP_DIR, 'user_timezones.json');
process.env.DEFAULT_TZ = 'UTC';

const tz = require('../src/timezoneManager');

test.beforeEach(() => tz._resetForTests());

test('default TZ when user not set is UTC', () => {
  assert.strictEqual(tz.getUserTz(99999), 'UTC');
});

test('setUserTz persists and getUserTz returns it', () => {
  tz.setUserTz(123, 'America/Argentina/Buenos_Aires');
  assert.strictEqual(tz.getUserTz(123), 'America/Argentina/Buenos_Aires');
});

test('setUserTz rejects invalid tz', () => {
  assert.throws(() => tz.setUserTz(1, 'Mars/Olympus_Mons'), /Invalid timezone/i);
});

test('isValidTimezone gates correctly', () => {
  assert.ok(tz.isValidTimezone('UTC'));
  assert.ok(tz.isValidTimezone('America/Sao_Paulo'));
  assert.ok(!tz.isValidTimezone('Atlantis/Lost_City'));
  assert.ok(!tz.isValidTimezone(null));
  assert.ok(!tz.isValidTimezone(''));
});

test('detectFromLangCode maps es-AR to Buenos_Aires', () => {
  assert.strictEqual(
    tz.detectFromLangCode('es-AR'),
    'America/Argentina/Buenos_Aires'
  );
});

test('detectFromLangCode maps pt-BR to Sao_Paulo', () => {
  assert.strictEqual(tz.detectFromLangCode('pt-BR'), 'America/Sao_Paulo');
});

test('detectFromLangCode falls back to base lang then UTC', () => {
  // Unknown variant → base lang
  assert.strictEqual(tz.detectFromLangCode('es-DO'), 'America/Argentina/Buenos_Aires');
  // Totally unknown → UTC default
  assert.strictEqual(tz.detectFromLangCode('xx-YY'), 'UTC');
  assert.strictEqual(tz.detectFromLangCode(null), 'UTC');
});

test('formatLocalTime converts UTC to ART/AR offset (UTC-3)', () => {
  const utc = '2026-05-01T17:50:00Z';
  // 17:50 UTC → 14:50 in Buenos_Aires
  const out = tz.formatLocalTime(
    1, utc, 'America/Argentina/Buenos_Aires'
  );
  assert.match(out, /14:50/);
  assert.match(out, /01/);
  assert.match(out, /may|May/);
});

test('formatLocalTime renders English weekday + month abbrev', () => {
  // 2026-05-01 is a Friday
  const out = tz.formatLocalTime(1, '2026-05-01T12:00:00Z', 'UTC');
  assert.match(out, /Fri/);
  assert.match(out, /May/);
  assert.match(out, /UTC/);
});

test('formatLocalTime uses stored TZ when no override', () => {
  tz.setUserTz(7, 'America/Sao_Paulo');
  const out = tz.formatLocalTime(7, '2026-05-01T17:50:00Z');
  // BRT = UTC-3 in May (no DST since 2019); 17:50 UTC → 14:50 BRT
  assert.match(out, /14:50/);
});

test('clearUserTz reverts to default', () => {
  tz.setUserTz(8, 'Europe/London');
  tz.clearUserTz(8);
  assert.strictEqual(tz.getUserTz(8), 'UTC');
});

test('stampMessage adds clock + idempotent', () => {
  const m = 'Hola';
  const stamped = tz.stampMessage(1, m, '2026-05-01T17:50:00Z');
  assert.match(stamped, /🕐/);
  // Already stamped → no double stamp
  const again = tz.stampMessage(1, stamped, '2026-05-01T17:50:00Z');
  assert.strictEqual(stamped, again);
});

test('persists across module loads (file-based)', () => {
  tz.setUserTz(42, 'Europe/Madrid');
  // simulate restart by reloading the module
  delete require.cache[require.resolve('../src/timezoneManager')];
  const fresh = require('../src/timezoneManager');
  assert.strictEqual(fresh.getUserTz(42), 'Europe/Madrid');
});
