'use strict';

/**
 * R-PUBLIC-FUNDS — Hysteresis + opt-in store + scheduler behaviour.
 *
 * Acceptance criteria covered here:
 *   • PM fixture crossing −$2.8K → +$2.5K headroom fires exactly ONE alert
 *   • hysteresis suppresses the immediate repeat
 *   • re-arm after falling below 50% of threshold
 *   • re-arm after 12h cooldown
 *   • rate limiting / batching with N simulated users
 */

const test = require('node:test');
const assert = require('node:assert');
const os = require('os');
const path = require('path');
const fs = require('fs');

// Isolate persistence into a temp dir BEFORE modules load.
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'funds-alert-test-'));
process.env.FUNDS_ALERT_DB_PATH = path.join(TMP, 'funds_alerts.json');
process.env.TRACK_DB_PATH = path.join(TMP, 'tracked_wallets.json');
process.env.PEAR_REFERRAL_CODE = 'BlackCatDeFi';

const store = require('../src/fundsAlertStore');
const scheduler = require('../src/fundsAlertScheduler');
const walletTracker = require('../src/walletTracker');
const { computeUniversalDeployable } = require('../src/fundsEngine');

const PRICES = { HYPE: 40, USDC: 1 };
const W1 = '0x' + '1'.repeat(40);

function pmFixture(hypeTokens, debt) {
  return computeUniversalDeployable({
    spotBalances: [
      { coin: 'HYPE', total: hypeTokens, hold: 0 },
      { coin: 'USDC', total: -debt, hold: 0 },
    ],
    perp: { accountValue: 0, marginUsed: 0, withdrawable: 0 },
    prices: PRICES,
  });
}

function fresh() {
  store._resetForTests();
  scheduler._resetForTests();
  walletTracker._resetForTests();
}

// ───────────────────────────── store basics ─────────────────────────────

test('opt-in default threshold is $500; off clears state', () => {
  fresh();
  const cfg = store.optIn(42, undefined);
  assert.strictEqual(cfg.threshold, 500);
  assert.strictEqual(store.getAllOptedIn().length, 1);
  store.optOut(42);
  assert.strictEqual(store.getAllOptedIn().length, 0);
  assert.strictEqual(store.getConfig(42), null);
});

test('threshold bounds enforced', () => {
  fresh();
  assert.throws(() => store.optIn(1, 5));
  assert.throws(() => store.optIn(1, 99999999999));
  assert.strictEqual(store.optIn(1, 250).threshold, 250);
});

// ───────────────────────────── hysteresis core ─────────────────────────────

test('ACCEPTANCE: PM headroom −$2.8K → +$2.5K fires exactly ONE alert; immediate repeat suppressed', () => {
  fresh();
  const threshold = 500;
  let t = 1000000;

  // Scan 1: over-borrowed — raw headroom −$2,800 clamps to 0 → below.
  const v1 = pmFixture(100, 4800); // capacity 2000, debt 4800 → 0
  assert.strictEqual(v1.pm_borrow_headroom, 0);
  let r = store.evaluate(7, W1, 'pm', v1.pm_borrow_headroom, threshold, t);
  assert.strictEqual(r.shouldFire, false);

  // Scan 2: debt repaid / price up — headroom +$2,500 → CROSSES → fires.
  const v2 = pmFixture(200, 1500); // capacity 4000, debt 1500 → 2500
  assert.strictEqual(v2.pm_borrow_headroom, 2500);
  r = store.evaluate(7, W1, 'pm', v2.pm_borrow_headroom, threshold, t + 60000);
  assert.strictEqual(r.shouldFire, true);

  // Scan 3: same headroom immediately after → suppressed (hysteresis).
  r = store.evaluate(7, W1, 'pm', 2500, threshold, t + 120000);
  assert.strictEqual(r.shouldFire, false);
  assert.strictEqual(r.reason, 'DISARMED');

  // Scan 4: still suppressed while above 50% of threshold.
  r = store.evaluate(7, W1, 'pm', 600, threshold, t + 180000);
  assert.strictEqual(r.shouldFire, false);
});

test('re-arms after falling below 50% of threshold, then fires on next crossing', () => {
  fresh();
  const th = 500;
  let t = 5000000;
  store.evaluate(9, W1, 'total', 0, th, t);
  assert.strictEqual(store.evaluate(9, W1, 'total', 800, th, t + 1000).shouldFire, true);
  // falls to $200 (< 50% of 500) → re-arms silently
  assert.strictEqual(store.evaluate(9, W1, 'total', 200, th, t + 2000).shouldFire, false);
  // crosses again → fires again
  assert.strictEqual(store.evaluate(9, W1, 'total', 900, th, t + 3000).shouldFire, true);
});

test('does NOT re-arm at 60% of threshold (flap suppression)', () => {
  fresh();
  const th = 500;
  let t = 9000000;
  store.evaluate(11, W1, 'total', 0, th, t);
  assert.strictEqual(store.evaluate(11, W1, 'total', 700, th, t + 1000).shouldFire, true);
  // dips to $300 (60%) then back over — classic flap → stays suppressed
  assert.strictEqual(store.evaluate(11, W1, 'total', 300, th, t + 2000).shouldFire, false);
  assert.strictEqual(store.evaluate(11, W1, 'total', 700, th, t + 3000).shouldFire, false);
});

test('re-arms after the 12h cooldown even if value stayed above threshold', () => {
  fresh();
  const th = 500;
  let t = 20000000;
  store.evaluate(13, W1, 'total', 0, th, t);
  assert.strictEqual(store.evaluate(13, W1, 'total', 1000, th, t + 1000).shouldFire, true);
  assert.strictEqual(store.evaluate(13, W1, 'total', 1000, th, t + 2000).shouldFire, false);
  const after12h = t + 2000 + store.COOLDOWN_MS + 1;
  assert.strictEqual(store.evaluate(13, W1, 'total', 1000, th, after12h).shouldFire, true);
});

test('fetch error (null value) never fires and never counts as $0', () => {
  fresh();
  const r = store.evaluate(15, W1, 'total', null, 500, 30000000);
  assert.strictEqual(r.shouldFire, false);
  assert.strictEqual(r.reason, 'NO_VALUE');
});

// ───────────────────────────── scheduler end-to-end ─────────────────────────────

test('scheduler: PM crossing fires exactly one branded alert with breakdown + CTA footer', async () => {
  fresh();
  const userId = 777;
  walletTracker.addWallet(userId, W1, 'PM wallet');
  store.optIn(userId, 500);

  const sent = [];
  const notify = async (chatId, message) => sent.push({ chatId, message });

  // Cycle 1: over-borrowed → below threshold, no alert.
  let fetcher = async () => pmFixture(100, 4800);
  let tel = await scheduler.scanOnce({ notify, fetcher, noJitter: true, now: 1000 });
  assert.strictEqual(tel.alertsSent, 0);

  // Cycle 2 (cache expired via new reset): headroom $2,500 → ONE alert.
  scheduler._resetForTests();
  fetcher = async () => pmFixture(200, 1500);
  tel = await scheduler.scanOnce({ notify, fetcher, noJitter: true, now: 2000 });
  assert.strictEqual(tel.alertsSent, 1);
  const body = sent[0].message;
  assert.match(body, /BORROW HEADROOM AVAILABLE/);
  assert.match(body, /Spot free stables/);
  assert.match(body, /Perp withdrawable/);
  assert.match(body, /PM borrow headroom/);
  assert.match(body, /projected liq/);
  assert.match(body, /pear/i); // standard Pear referral CTA footer
  assert.match(body, /BlackCatDeFi/);

  // Cycle 3: identical state → hysteresis suppresses the repeat.
  scheduler._resetForTests();
  tel = await scheduler.scanOnce({ notify, fetcher, noJitter: true, now: 3000 });
  assert.strictEqual(tel.alertsSent, 0);
  assert.strictEqual(sent.length, 1);
});

test('scheduler rate limiting: N users sharing wallets → unique fetches, per-cycle cap respected', async () => {
  fresh();
  // 40 users, 10 unique wallets (4 users per wallet) → 10 fetches max.
  const wallets = [];
  for (let i = 0; i < 10; i++) wallets.push('0x' + String(i).repeat(40).slice(0, 40));
  for (let u = 0; u < 40; u++) {
    const w = wallets[u % 10];
    walletTracker.addWallet(1000 + u, w, null);
    store.optIn(1000 + u, 500);
  }
  let fetchCount = 0;
  const fetcher = async () => {
    fetchCount++;
    return computeUniversalDeployable({
      spotBalances: [{ coin: 'USDC', total: 10, hold: 0 }],
      perp: { accountValue: 0, marginUsed: 0, withdrawable: 0 },
      prices: { USDC: 1 },
    });
  };
  const tel = await scheduler.scanOnce({ notify: async () => {}, fetcher, noJitter: true, now: 5000 });
  assert.strictEqual(tel.usersScanned, 40);
  assert.strictEqual(tel.walletsScanned, 10); // dedup: one per unique wallet
  assert.strictEqual(fetchCount, 10);
  assert.ok(tel.walletsScanned <= scheduler.MAX_WALLETS_PER_CYCLE);

  // Second scan inside cache TTL → zero new fetches (cache hit).
  const tel2 = await scheduler.scanOnce({ notify: async () => {}, fetcher, noJitter: true, now: 6000 });
  assert.strictEqual(tel2.fetches, 0);
  assert.strictEqual(fetchCount, 10);
});

test('scheduler poll interval clamped to the 15–30 min band', () => {
  assert.ok(scheduler.POLL_MIN >= 15 && scheduler.POLL_MIN <= 30);
});

test('fetch-error view never alerts and never renders $0', async () => {
  fresh();
  walletTracker.addWallet(50, W1, null);
  store.optIn(50, 500);
  const sent = [];
  const tel = await scheduler.scanOnce({
    notify: async (c, m) => sent.push(m),
    fetcher: async () => ({ error: true }),
    noJitter: true,
    now: 7000,
  });
  assert.strictEqual(tel.alertsSent, 0);
  assert.strictEqual(sent.length, 0);
});
