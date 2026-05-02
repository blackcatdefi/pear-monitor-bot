'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');

const TMP_DIR = fs.mkdtempSync(path.join(os.tmpdir(), 'wt-test-'));
process.env.TRACK_DB_PATH = path.join(TMP_DIR, 'tracked_wallets.json');
process.env.TRACK_MAX_WALLETS_PER_USER = '10';

const wt = require('../src/walletTracker');

test.beforeEach(() => wt._resetForTests());

const ADDR_A = '0x' + 'a'.repeat(40);
const ADDR_B = '0x' + 'b'.repeat(40);
const ADDR_INVALID = '0xINVALID';

test('isValidAddress accepts proper 0x+40hex', () => {
  assert.ok(wt.isValidAddress(ADDR_A));
  assert.ok(wt.isValidAddress('0xc7AE9550A37e72fed7B40dCC95Bd17e5BB1F1505'));
});

test('isValidAddress rejects bad input', () => {
  assert.ok(!wt.isValidAddress(ADDR_INVALID));
  assert.ok(!wt.isValidAddress('0xabc'));
  assert.ok(!wt.isValidAddress(null));
  assert.ok(!wt.isValidAddress('not_an_address'));
});

test('addWallet persists and getUserWallets returns it', () => {
  wt.addWallet(1, ADDR_A, 'Whale 1');
  const ws = wt.getUserWallets(1);
  assert.strictEqual(ws.length, 1);
  assert.strictEqual(ws[0].address, ADDR_A);
  assert.strictEqual(ws[0].label, 'Whale 1');
});

test('addWallet rejects invalid address', () => {
  assert.throws(() => wt.addWallet(1, ADDR_INVALID, 'Bad'), /Invalid address/i);
});

test('addWallet rejects duplicate per same user', () => {
  wt.addWallet(1, ADDR_A, 'A');
  assert.throws(() => wt.addWallet(1, ADDR_A, 'A2'), /already tracking/i);
});

test('Same address by different users → independent records', () => {
  wt.addWallet(3, ADDR_A, 'A');
  wt.addWallet(4, ADDR_A, 'B');
  assert.strictEqual(wt.getUserWallets(3).length, 1);
  assert.strictEqual(wt.getUserWallets(4).length, 1);
});

test('Max wallets per user enforced', () => {
  for (let i = 0; i < 10; i++) {
    const a =
      '0x' +
      i.toString(16).padStart(2, '0').repeat(20);
    wt.addWallet(2, a, null);
  }
  assert.throws(
    () => wt.addWallet(2, ADDR_B, null),
    /wallet limit/i
  );
});

test('removeWallet returns count removed', () => {
  wt.addWallet(5, ADDR_A, 'A');
  assert.strictEqual(wt.removeWallet(5, ADDR_A), 1);
  assert.strictEqual(wt.getUserWallets(5).length, 0);
  // removing again is a no-op
  assert.strictEqual(wt.removeWallet(5, ADDR_A), 0);
});

test('hasWallet returns true after add', () => {
  wt.addWallet(6, ADDR_A, null);
  assert.ok(wt.hasWallet(6, ADDR_A));
  assert.ok(wt.hasWallet(6, ADDR_A.toUpperCase())); // case-insensitive
  assert.ok(!wt.hasWallet(6, ADDR_B));
});

test('getAllUniqueAddresses dedupes and returns subscribers', () => {
  wt.addWallet(10, ADDR_A, 'L1');
  wt.addWallet(11, ADDR_A, 'L2');
  wt.addWallet(11, ADDR_B, 'B11');
  const all = wt.getAllUniqueAddresses();
  assert.strictEqual(all.length, 2);
  const aRecord = all.find((a) => a.address.toLowerCase() === ADDR_A.toLowerCase());
  assert.strictEqual(aRecord.subscribers.length, 2);
});

test('getSubscribersForAddress', () => {
  wt.addWallet(20, ADDR_A, 'X');
  wt.addWallet(21, ADDR_A, 'Y');
  const subs = wt.getSubscribersForAddress(ADDR_A);
  assert.strictEqual(subs.length, 2);
  assert.ok(subs.some((s) => s.userId === '20'));
  assert.ok(subs.some((s) => s.userId === '21'));
});

test('snapshot persistence works', () => {
  wt.setLastSnapshot(ADDR_A, [{ coin: 'BTC', side: 'SHORT' }]);
  const got = wt.getLastSnapshot(ADDR_A);
  assert.strictEqual(got.length, 1);
  assert.strictEqual(got[0].coin, 'BTC');
});

test('persists across module reload', () => {
  wt.addWallet(99, ADDR_A, 'survivor');
  delete require.cache[require.resolve('../src/walletTracker')];
  const fresh = require('../src/walletTracker');
  const ws = fresh.getUserWallets(99);
  assert.strictEqual(ws.length, 1);
  assert.strictEqual(ws[0].address, ADDR_A);
});

test('label trimmed and capped at 64 chars', () => {
  const longLabel = 'x'.repeat(200);
  wt.addWallet(50, ADDR_A, longLabel);
  const ws = wt.getUserWallets(50);
  assert.ok(ws[0].label.length <= 64);
});
