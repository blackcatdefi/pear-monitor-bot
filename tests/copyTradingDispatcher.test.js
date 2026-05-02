'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'copy-trading-disp-'));
process.env.COPY_TRADING_DB_DIR = TMP;
process.env.SIGNALS_SCRAPER_ENABLED = 'true';
process.env.COPY_AUTO_DEFAULT_CAPITAL = '100';

// Re-import store fresh against sandbox
delete require.cache[require.resolve('../src/copyTradingStore')];
const store = require('../src/copyTradingStore');

// Stub external HL fetcher BEFORE requiring copyTrading
const ext = require('../src/externalWalletTracker');
const realFetch = ext.fetchHyperliquidPositions;

let _mockBcdState = [];
const _mockCustomState = new Map();

ext.fetchHyperliquidPositions = async (addr) => {
  const a = String(addr).toLowerCase();
  if (a === store.BCD_WALLET) return _mockBcdState.slice();
  if (_mockCustomState.has(a)) return _mockCustomState.get(a).slice();
  return [];
};

delete require.cache[require.resolve('../src/copyTrading')];
const copyTrading = require('../src/copyTrading');

function _makeNotify() {
  const sent = [];
  const fn = async (chatId, text, opts) => sent.push({ chatId, text, opts });
  fn.sent = sent;
  return fn;
}

test.afterEach(() => {
  store._resetForTests();
  copyTrading._resetForTests();
  _mockBcdState = [];
  _mockCustomState.clear();
});

test('BCD wallet poller baselines on first cycle and emits no alerts', async () => {
  const notify = _makeNotify();
  copyTrading.attach(notify);
  store.setTarget(1, store.TYPE_BCD_WALLET, null, { enabled: true, capital_usdc: 100 });
  _mockBcdState = [{ coin: 'WLD', side: 'SHORT', size: 100, entryPx: 1.5 }];
  const r = await copyTrading.pollBcdWalletOnce();
  assert.equal(r.opens, 0);
  assert.equal(r.closes, 0);
  assert.equal(notify.sent.length, 0);
});

test('BCD wallet poller emits OPEN alert on second cycle when basket appears', async () => {
  const notify = _makeNotify();
  copyTrading.attach(notify);
  store.setTarget(2, store.TYPE_BCD_WALLET, null, { enabled: true, capital_usdc: 250 });
  _mockBcdState = [];
  await copyTrading.pollBcdWalletOnce(); // baseline empty
  _mockBcdState = [
    { coin: 'WLD', side: 'SHORT', size: 100, entryPx: 1.5 },
    { coin: 'STRK', side: 'SHORT', size: 50, entryPx: 0.4 },
  ];
  const r = await copyTrading.pollBcdWalletOnce();
  assert.equal(r.opens, 1);
  assert.equal(notify.sent.length, 1);
  assert.match(notify.sent[0].text, /NEW BASKET — BCD Wallet/);
  assert.match(notify.sent[0].text, /WLD SHORT/);
});

test('BCD wallet poller emits CLOSE when position disappears', async () => {
  const notify = _makeNotify();
  copyTrading.attach(notify);
  store.setTarget(3, store.TYPE_BCD_WALLET, null, { enabled: true, capital_usdc: 100 });
  _mockBcdState = [{ coin: 'ENA', side: 'SHORT', size: 50, entryPx: 0.3 }];
  await copyTrading.pollBcdWalletOnce();
  _mockBcdState = [];
  const r = await copyTrading.pollBcdWalletOnce();
  assert.equal(r.closes, 1);
  assert.match(notify.sent[notify.sent.length - 1].text, /BASKET CLOSED/);
});

test('BCD wallet poller fans out to multiple subscribers', async () => {
  const notify = _makeNotify();
  copyTrading.attach(notify);
  store.setTarget(10, store.TYPE_BCD_WALLET, null, { enabled: true, capital_usdc: 100 });
  store.setTarget(11, store.TYPE_BCD_WALLET, null, { enabled: true, capital_usdc: 200 });
  store.setTarget(12, store.TYPE_BCD_WALLET, null, { enabled: false }); // not subscribed
  _mockBcdState = [];
  await copyTrading.pollBcdWalletOnce();
  _mockBcdState = [{ coin: 'BTC', side: 'LONG', size: 1, entryPx: 65000 }];
  const r = await copyTrading.pollBcdWalletOnce();
  assert.equal(r.opens, 2);
  // Each subscriber sees their own capital in the message
  assert.ok(notify.sent.find((m) => /\$100/.test(m.text)));
  assert.ok(notify.sent.find((m) => /\$200/.test(m.text)));
});

test('signal dispatcher fans out only to enabled BCD_SIGNALS subscribers', async () => {
  const notify = _makeNotify();
  copyTrading.attach(notify);
  store.setTarget(20, store.TYPE_BCD_SIGNALS, null, { enabled: true, capital_usdc: 300 });
  store.setTarget(21, store.TYPE_BCD_WALLET, null, { enabled: true }); // wrong type
  store.setTarget(22, store.TYPE_BCD_SIGNALS, null, { enabled: false });
  const sig = {
    messageId: 5,
    pearUrl: 'https://app.pear.garden/trade/hl/USDC-ENA?referral=BlackCatDeFi',
    longTokens: [],
    shortTokens: ['ENA'],
  };
  const n = await copyTrading.dispatchSignalToSubscribers(sig);
  assert.equal(n, 1);
  assert.equal(notify.sent.length, 1);
  assert.equal(notify.sent[0].chatId, 20);
});

test('custom wallets poller does 1 fetch + fan-out to multiple subscribers', async () => {
  const notify = _makeNotify();
  copyTrading.attach(notify);
  const shared = '0x' + 'a'.repeat(40);
  store.setTarget(30, store.TYPE_CUSTOM_WALLET, shared, {
    enabled: true,
    capital_usdc: 50,
    label: 'Whale 1',
  });
  store.setTarget(31, store.TYPE_CUSTOM_WALLET, shared, {
    enabled: true,
    capital_usdc: 75,
    label: 'My copy of Whale 1',
  });
  _mockCustomState.set(shared, []);
  await copyTrading.pollCustomWalletsOnce();
  _mockCustomState.set(shared, [{ coin: 'BTC', side: 'LONG', size: 1, entryPx: 65000 }]);
  const r = await copyTrading.pollCustomWalletsOnce();
  assert.equal(r.opens, 2);
  assert.equal(notify.sent.length, 2);
});

test('_diff identifies opens and closes', () => {
  const prev = [{ coin: 'A', side: 'SHORT' }, { coin: 'B', side: 'LONG' }];
  const curr = [{ coin: 'A', side: 'SHORT' }, { coin: 'C', side: 'LONG' }];
  const { opens, closes } = copyTrading._diff(prev, curr);
  assert.deepEqual(opens.map((p) => p.coin), ['C']);
  assert.deepEqual(closes.map((p) => p.coin), ['B']);
});

test('_splitSides separates by side', () => {
  const { longTokens, shortTokens } = copyTrading._splitSides([
    { coin: 'A', side: 'LONG' },
    { coin: 'B', side: 'SHORT' },
    { coin: 'C', side: 'SHORT' },
  ]);
  assert.deepEqual(longTokens, ['A']);
  assert.deepEqual(shortTokens, ['B', 'C']);
});

test('_normalizePos derives side from size sign when missing', () => {
  const out = copyTrading._normalizePos([
    { coin: 'X', size: -5, entryPx: 1 },
    { coin: 'Y', size: 5, entryPx: 1 },
  ]);
  assert.equal(out.find((p) => p.coin === 'X').side, 'SHORT');
  assert.equal(out.find((p) => p.coin === 'Y').side, 'LONG');
});

test('no fan-out when no subscribers enabled', async () => {
  const notify = _makeNotify();
  copyTrading.attach(notify);
  // no users
  _mockBcdState = [];
  await copyTrading.pollBcdWalletOnce();
  _mockBcdState = [{ coin: 'BTC', side: 'LONG', size: 1, entryPx: 65000 }];
  const r = await copyTrading.pollBcdWalletOnce();
  assert.equal(r.opens, 0);
  assert.equal(notify.sent.length, 0);
});

test('teardown restores real fetch fn', () => {
  // sanity — restore so other tests in suite can use the real fn if needed
  ext.fetchHyperliquidPositions = realFetch;
  assert.equal(typeof ext.fetchHyperliquidPositions, 'function');
});
