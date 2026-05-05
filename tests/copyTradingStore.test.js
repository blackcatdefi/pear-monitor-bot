'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');

// Sandbox the persistent paths before requiring the module.
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'copy-trading-store-'));
process.env.COPY_TRADING_DB_DIR = TMP;
process.env.COPY_AUTO_MIN_CAPITAL = '10';
process.env.COPY_AUTO_MAX_CAPITAL = '50000';
process.env.COPY_AUTO_DEFAULT_CAPITAL = '100';
// R-PUBLIC-V4-COPYMENU — custom-wallet cap moved to its own env var (3),
// separate from /track cap. Tests pin to 3 to match production default.
process.env.COPY_TRADING_MAX_CUSTOM_PER_USER = '3';

const store = require('../src/copyTradingStore');

test('constants exposed', () => {
  assert.equal(store.BCD_WALLET, '0xc7ae23316b47f7e75f455f53ad37873a18351505');
  assert.equal(store.REFERRAL_CODE, 'BlackCatDeFi');
  assert.equal(store.MIN_CAPITAL, 10);
  assert.equal(store.MAX_CAPITAL, 50000);
  assert.equal(store.DEFAULT_CAPITAL, 100);
  assert.equal(store.MAX_CUSTOM_PER_USER, 3);
  assert.deepEqual(store.VALID_TYPES, ['BCD_WALLET', 'CUSTOM_WALLET']);
  // V4: BCD_SIGNALS surface is gone
  assert.equal(store.TYPE_BCD_SIGNALS, undefined);
  assert.equal(store.BCD_SIGNALS_CHANNEL, undefined);
});

test('getTargets returns empty slot for unknown user', () => {
  store._resetForTests();
  const t = store.getTargets(999);
  assert.equal(t.BCD_WALLET, null);
  assert.equal(t.BCD_SIGNALS, undefined); // surface removed
  assert.deepEqual(t.CUSTOM_WALLET, []);
});

test('setTarget BCD_WALLET upsert + enabled flag persists', () => {
  store._resetForTests();
  const r = store.setTarget(1001, store.TYPE_BCD_WALLET, null, {
    enabled: true,
    capital_usdc: 250,
    mode: 'AUTO',
  });
  assert.equal(r.enabled, 1);
  assert.equal(r.capital_usdc, 250);
  assert.equal(r.mode, 'AUTO');
  assert.equal(r.target_type, 'BCD_WALLET');
  assert.equal(r.target_ref, store.BCD_WALLET);
  const got = store.getTarget(1001, store.TYPE_BCD_WALLET);
  assert.equal(got.capital_usdc, 250);
});

// R-PUBLIC-V4-COPYMENU — BCD_SIGNALS removed; rejecting it is the regression
test('setTarget BCD_SIGNALS is now rejected as invalid type', () => {
  store._resetForTests();
  assert.throws(() =>
    store.setTarget(1002, 'BCD_SIGNALS', null, { enabled: true, capital_usdc: 300 })
  );
});

test('setTarget CUSTOM_WALLET requires valid 0x address', () => {
  store._resetForTests();
  assert.throws(() => store.setTarget(2, store.TYPE_CUSTOM_WALLET, 'notanaddress'));
  assert.throws(() => store.setTarget(2, store.TYPE_CUSTOM_WALLET, '0xabc'));
});

test('setTarget CUSTOM_WALLET upsert by lowercase address', () => {
  store._resetForTests();
  const a = '0x' + 'A'.repeat(40);
  store.setTarget(3, store.TYPE_CUSTOM_WALLET, a, { enabled: true, capital_usdc: 50 });
  const t = store.getTargets(3);
  assert.equal(t.CUSTOM_WALLET.length, 1);
  assert.equal(t.CUSTOM_WALLET[0].ref, a.toLowerCase());
  // upsert (no dup)
  store.setTarget(3, store.TYPE_CUSTOM_WALLET, a.toLowerCase(), { capital_usdc: 75 });
  const t2 = store.getTargets(3);
  assert.equal(t2.CUSTOM_WALLET.length, 1);
  assert.equal(t2.CUSTOM_WALLET[0].capital_usdc, 75);
});

test('setTarget CUSTOM_WALLET enforces MAX_CUSTOM_PER_USER (V4 cap = 3)', () => {
  store._resetForTests();
  // V4: cap is 3 per user (down from 10 in R-AUTOCOPY-MENU). Add 3 then
  // assert the 4th throws.
  for (let i = 0; i < 3; i++) {
    const addr = '0x' + String(i).padStart(40, '0');
    store.setTarget(99, store.TYPE_CUSTOM_WALLET, addr, { enabled: true });
  }
  assert.throws(() =>
    store.setTarget(99, store.TYPE_CUSTOM_WALLET, '0x' + 'b'.repeat(40), { enabled: true })
  );
});

test('capital validation clamps invalid values', () => {
  store._resetForTests();
  assert.throws(() => store.setTarget(5, store.TYPE_BCD_WALLET, null, { capital_usdc: '0' }));
  assert.throws(() => store.setTarget(5, store.TYPE_BCD_WALLET, null, { capital_usdc: '5' }));
  assert.throws(() => store.setTarget(5, store.TYPE_BCD_WALLET, null, { capital_usdc: '99999' }));
});

test('mode validation: only MANUAL/AUTO accepted, default MANUAL', () => {
  store._resetForTests();
  const r = store.setTarget(6, store.TYPE_BCD_WALLET, null, { mode: 'weird' });
  assert.equal(r.mode, 'MANUAL');
  const r2 = store.setTarget(6, store.TYPE_BCD_WALLET, null, { mode: 'auto' });
  assert.equal(r2.mode, 'AUTO');
});

test('removeTarget BCD_WALLET zeroes the slot', () => {
  store._resetForTests();
  store.setTarget(7, store.TYPE_BCD_WALLET, null, { enabled: true });
  assert.ok(store.getTarget(7, store.TYPE_BCD_WALLET));
  store.removeTarget(7, store.TYPE_BCD_WALLET);
  assert.equal(store.getTarget(7, store.TYPE_BCD_WALLET), null);
});

test('removeTarget CUSTOM_WALLET removes by address', () => {
  store._resetForTests();
  const a = '0x' + 'c'.repeat(40);
  const b = '0x' + 'd'.repeat(40);
  store.setTarget(8, store.TYPE_CUSTOM_WALLET, a, { enabled: true });
  store.setTarget(8, store.TYPE_CUSTOM_WALLET, b, { enabled: true });
  store.removeTarget(8, store.TYPE_CUSTOM_WALLET, a);
  const t = store.getTargets(8);
  assert.equal(t.CUSTOM_WALLET.length, 1);
  assert.equal(t.CUSTOM_WALLET[0].ref, b);
});

test('listEnabledByType filters disabled entries (V4: BCD_WALLET only)', () => {
  store._resetForTests();
  store.setTarget(10, store.TYPE_BCD_WALLET, null, { enabled: true });
  store.setTarget(11, store.TYPE_BCD_WALLET, null, { enabled: false });
  const ws = store.listEnabledByType(store.TYPE_BCD_WALLET);
  assert.equal(ws.length, 1);
  assert.equal(ws[0].userId, '10');
});

test('listAllCustomAddresses dedupes addresses across users', () => {
  store._resetForTests();
  const shared = '0x' + 'e'.repeat(40);
  store.setTarget(20, store.TYPE_CUSTOM_WALLET, shared, { enabled: true, capital_usdc: 100 });
  store.setTarget(21, store.TYPE_CUSTOM_WALLET, shared, { enabled: true, capital_usdc: 200 });
  store.setTarget(22, store.TYPE_CUSTOM_WALLET, '0x' + 'f'.repeat(40), { enabled: true });
  const list = store.listAllCustomAddresses();
  assert.equal(list.length, 2);
  const sharedEntry = list.find((g) => g.address === shared);
  assert.ok(sharedEntry);
  assert.equal(sharedEntry.subscribers.length, 2);
});

test('listAllCustomAddresses excludes disabled subscribers', () => {
  store._resetForTests();
  const a = '0x' + '1'.repeat(40);
  store.setTarget(30, store.TYPE_CUSTOM_WALLET, a, { enabled: true });
  store.setTarget(31, store.TYPE_CUSTOM_WALLET, a, { enabled: false });
  const list = store.listAllCustomAddresses();
  assert.equal(list.length, 1);
  assert.equal(list[0].subscribers.length, 1);
  assert.equal(list[0].subscribers[0].userId, '30');
});

// R-PUBLIC-V4-COPYMENU — signal seen tracker REMOVED (no scraper, no
// channel-message dedup). Replaced with regression that proves the surface
// is gone.
test('R-PUBLIC-V4 — signal seen tracker is removed from store', () => {
  assert.equal(typeof store.hasSignalBeenSeen, 'undefined');
  assert.equal(typeof store.markSignalSeen, 'undefined');
});

test('persistence: state survives store reload', () => {
  store._resetForTests();
  store.setTarget(40, store.TYPE_BCD_WALLET, null, { enabled: true, capital_usdc: 500 });
  // Force re-load by re-requiring with a fresh module cache
  delete require.cache[require.resolve('../src/copyTradingStore')];
  const fresh = require('../src/copyTradingStore');
  // load from file
  const got = fresh.getTarget(40, fresh.TYPE_BCD_WALLET);
  assert.ok(got);
  assert.equal(got.capital_usdc, 500);
  assert.equal(got.enabled, 1);
});

test('label trimmed and capped at 64 chars', () => {
  // re-require fresh module after persistence test
  delete require.cache[require.resolve('../src/copyTradingStore')];
  const s = require('../src/copyTradingStore');
  s._resetForTests();
  const a = '0x' + '2'.repeat(40);
  s.setTarget(50, s.TYPE_CUSTOM_WALLET, a, {
    enabled: true,
    label: '   ' + 'x'.repeat(200) + '   ',
  });
  const t = s.getTargets(50);
  assert.ok(t.CUSTOM_WALLET[0].label.length <= 64);
});

test('toggle enabled flag flips correctly', () => {
  delete require.cache[require.resolve('../src/copyTradingStore')];
  const s = require('../src/copyTradingStore');
  s._resetForTests();
  s.setTarget(60, s.TYPE_BCD_WALLET, null, { enabled: true });
  s.setTarget(60, s.TYPE_BCD_WALLET, null, { enabled: false });
  const got = s.getTarget(60, s.TYPE_BCD_WALLET);
  assert.equal(got.enabled, 0);
});
