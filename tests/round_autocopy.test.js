'use strict';

/**
 * R-AUTOCOPY — comprehensive tests for the 12 new modules.
 *
 * Sandboxes JSON storage paths via env vars set BEFORE any require() so each
 * module persists into a unique tmp dir.
 */

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');

const TMP_DIR = fs.mkdtempSync(path.join(os.tmpdir(), 'rautocopy-test-'));
process.env.COPY_AUTO_DB_PATH = path.join(TMP_DIR, 'copy_auto.json');
process.env.SHARE_DB_PATH = path.join(TMP_DIR, 'referrals.json');
process.env.ALERTS_CONFIG_DB_PATH = path.join(TMP_DIR, 'alerts_config.json');
process.env.STATS_DB_PATH = path.join(TMP_DIR, 'user_stats.json');
process.env.DAILY_DIGEST_DB_PATH = path.join(TMP_DIR, 'daily_digest.json');
process.env.FEEDBACK_LOG_PATH = path.join(TMP_DIR, 'feedback_audit.json');
process.env.TRACK_DB_PATH = path.join(TMP_DIR, 'tracked_wallets.json');
process.env.USER_TZ_DB_PATH = path.join(TMP_DIR, 'user_timezones.json');
process.env.PEAR_REFERRAL_CODE = 'BlackCatDeFi';
process.env.PEAR_HERO_URL = 'https://app.pear.garden/?referral=BlackCatDeFi';
process.env.DEFAULT_TZ = 'UTC';
process.env.PREMIUM_REFERRAL_THRESHOLD = '3';
process.env.PREMIUM_TRACK_SLOTS = '25';
process.env.DEFAULT_TRACK_SLOTS = '10';
process.env.LEADERBOARD_MIN_TRACKERS = '2';
process.env.COPY_AUTO_DEFAULT_CAPITAL = '100';
process.env.COPY_AUTO_MIN_CAPITAL = '10';
process.env.COPY_AUTO_MAX_CAPITAL = '50000';
process.env.DAILY_DIGEST_DEFAULT_HOUR = '9';

const signalsParser = require('../src/signalsParser');
const copyAutoStore = require('../src/copyAutoStore');
const copyAuto = require('../src/copyAuto');
const portfolioFetcher = require('../src/portfolioFetcher');
const leaderboard = require('../src/leaderboard');
const alertsConfig = require('../src/alertsConfig');
const share = require('../src/share');
const learn = require('../src/learn');
const feedback = require('../src/feedback');
const stats = require('../src/stats');
const dailyDigest = require('../src/dailyDigest');
const wt = require('../src/walletTracker');

function _resetAll() {
  copyAutoStore._resetForTests();
  share._resetForTests();
  alertsConfig._resetForTests();
  stats._resetForTests();
  dailyDigest._resetForTests();
  wt._resetForTests();
}

// ---------- signalsParser ----------

test('signalsParser: parseSignal extracts 5-token mixed-side basket', () => {
  const text = [
    '🚀 SIGNAL OFICIAL #42',
    '',
    'Basket:',
    '  DYDX SHORT',
    '  OP SHORT',
    '  ARB SHORT',
    '  PYTH SHORT',
    '  ENA SHORT',
    '',
    'Leverage: 4x',
    'SL: 50% / Trailing 10% activación 30%',
    'TWAP: 14h, 30 bullets',
    '#signal #basket',
  ].join('\n');
  const sig = signalsParser.parseSignal(text);
  assert.ok(sig);
  assert.strictEqual(sig.signal_id, '42');
  assert.deepStrictEqual(sig.tokens, ['DYDX', 'OP', 'ARB', 'PYTH', 'ENA']);
  assert.deepStrictEqual(sig.sides, ['SHORT', 'SHORT', 'SHORT', 'SHORT', 'SHORT']);
  assert.strictEqual(sig.leverage, 4);
  assert.strictEqual(sig.sl_pct, 50);
  assert.strictEqual(sig.trailing_pct, 10);
  assert.strictEqual(sig.trailing_activation_pct, 30);
  assert.strictEqual(sig.twap_hours, 14);
  assert.strictEqual(sig.twap_bullets, 30);
});

test('signalsParser: parseSignal supports mixed LONG+SHORT positions', () => {
  const text = [
    '🚀 SIGNAL OFICIAL #11',
    '  BTC LONG',
    '  ETH LONG',
    '  SOL SHORT',
    '#signal',
  ].join('\n');
  const sig = signalsParser.parseSignal(text);
  assert.ok(sig);
  assert.deepStrictEqual(sig.tokens, ['BTC', 'ETH', 'SOL']);
  assert.deepStrictEqual(sig.sides, ['LONG', 'LONG', 'SHORT']);
});

test('signalsParser: returns null for non-signal text', () => {
  assert.strictEqual(signalsParser.parseSignal('hola mundo'), null);
  assert.strictEqual(signalsParser.parseSignal(''), null);
  assert.strictEqual(signalsParser.parseSignal(null), null);
});

test('signalsParser: returns null when hashtag present but no positions', () => {
  const text = '#signal\n\nUpdate: hold.';
  assert.strictEqual(signalsParser.parseSignal(text), null);
});

test('signalsParser: applies defaults when SL / trailing absent', () => {
  const text = [
    '#signal SIGNAL OFICIAL #99',
    '  BTC SHORT',
  ].join('\n');
  const sig = signalsParser.parseSignal(text);
  assert.ok(sig);
  assert.strictEqual(sig.sl_pct, 50);
  assert.strictEqual(sig.trailing_pct, 10);
  assert.strictEqual(sig.trailing_activation_pct, 30);
  assert.strictEqual(sig.leverage, null);
});

test('signalsParser: looksLikeSignal predicate', () => {
  assert.strictEqual(signalsParser.looksLikeSignal('#signal foo'), true);
  assert.strictEqual(signalsParser.looksLikeSignal('SIGNAL OFICIAL #5'), true);
  assert.strictEqual(signalsParser.looksLikeSignal('hola'), false);
  assert.strictEqual(signalsParser.looksLikeSignal(null), false);
});

test('signalsParser: skips header tokens (BASKET, LEVERAGE, etc)', () => {
  const text = [
    '#signal SIGNAL OFICIAL #1',
    'BASKET LONG', // header word — must be skipped
    'BTC SHORT',
  ].join('\n');
  const sig = signalsParser.parseSignal(text);
  assert.ok(sig);
  assert.deepStrictEqual(sig.tokens, ['BTC']);
});

// ---------- copyAutoStore ----------

test('copyAutoStore: getConfig returns defaults for unknown user', () => {
  _resetAll();
  const cfg = copyAutoStore.getConfig(99999);
  assert.strictEqual(cfg.enabled, 0);
  assert.strictEqual(cfg.mode, 'MANUAL');
  assert.strictEqual(cfg.capital_usdc, 100);
  assert.strictEqual(cfg.sl_pct, 50);
  assert.strictEqual(cfg.trailing_pct, 10);
  assert.strictEqual(cfg.trailing_activation_pct, 30);
});

test('copyAutoStore: setEnabled / setMode persists config', () => {
  _resetAll();
  copyAutoStore.setEnabled(1, true);
  copyAutoStore.setMode(1, 'AUTO');
  const cfg = copyAutoStore.getConfig(1);
  assert.strictEqual(cfg.enabled, 1);
  assert.strictEqual(cfg.mode, 'AUTO');
});

test('copyAutoStore: setMode rejects invalid mode', () => {
  _resetAll();
  assert.throws(() => copyAutoStore.setMode(1, 'CRAZY'), /MANUAL o AUTO/);
});

test('copyAutoStore: validateCapital boundaries', () => {
  assert.strictEqual(copyAutoStore.validateCapital(100), 100);
  assert.strictEqual(copyAutoStore.validateCapital(10), 10);
  assert.strictEqual(copyAutoStore.validateCapital(50000), 50000);
  assert.throws(() => copyAutoStore.validateCapital(5), /mínimo/i);
  assert.throws(() => copyAutoStore.validateCapital(60000), /máximo/i);
  assert.throws(() => copyAutoStore.validateCapital('abc'), /inválido/i);
});

test('copyAutoStore: setCapital strips/validates', () => {
  _resetAll();
  copyAutoStore.setCapital(2, 500);
  const cfg = copyAutoStore.getConfig(2);
  assert.strictEqual(cfg.capital_usdc, 500);
});

test('copyAutoStore: listEnabledUsers filters by enabled=1', () => {
  _resetAll();
  copyAutoStore.setEnabled(101, true);
  copyAutoStore.setEnabled(102, false);
  copyAutoStore.setEnabled(103, true);
  const list = copyAutoStore.listEnabledUsers();
  const ids = list.map((u) => u.userId).sort();
  assert.deepStrictEqual(ids, ['101', '103']);
});

// ---------- copyAuto ----------

test('copyAuto: buildManualMessage renders capital + SL', () => {
  const sig = {
    signal_id: '7',
    positions: [{ coin: 'BTC', side: 'SHORT' }, { coin: 'ETH', side: 'SHORT' }],
    tokens: ['BTC', 'ETH'],
    sides: ['SHORT', 'SHORT'],
    leverage: 4,
    twap_hours: 12,
    twap_bullets: 20,
  };
  const cfg = { capital_usdc: 250, sl_pct: 50, trailing_pct: 10, trailing_activation_pct: 30 };
  const msg = copyAuto.buildManualMessage(sig, cfg, 1);
  assert.match(msg, /NUEVA SIGNAL OFICIAL #7/);
  assert.match(msg, /\$250 USDC/);
  assert.match(msg, /BTC SHORT/);
  assert.match(msg, /ETH SHORT/);
  assert.match(msg, /Leverage: 4x/);
  assert.match(msg, /SL 50%/);
  assert.match(msg, /TWAP: 12h, 20 bullets/);
});

test('copyAuto: buildAutoMessage uses concise format', () => {
  const sig = {
    signal_id: '8',
    positions: [{ coin: 'SOL', side: 'LONG' }],
    tokens: ['SOL'],
    sides: ['LONG'],
    leverage: 3,
  };
  const cfg = { capital_usdc: 100, sl_pct: 50, trailing_pct: 10 };
  const msg = copyAuto.buildAutoMessage(sig, cfg, 1);
  assert.match(msg, /COPY AUTO/);
  assert.match(msg, /\$100 USDC capital/);
  assert.match(msg, /SOL LONG/);
});

test('copyAuto: _heroButtons splits SHORTs and LONGs', () => {
  const positions = [
    { coin: 'BTC', side: 'SHORT' },
    { coin: 'ETH', side: 'SHORT' },
    { coin: 'SOL', side: 'LONG' },
  ];
  const rows = copyAuto._heroButtons(positions, 100);
  // 2 rows: one for SHORTs, one for LONGs
  assert.strictEqual(rows.length, 2);
  assert.match(rows[0][0].text, /SHORTs/);
  assert.match(rows[1][0].text, /LONGs/);
});

test('copyAuto: dispatchSignal returns 0 when notifier not attached', async () => {
  _resetAll();
  // Don't call attach()
  const sig = { signal_id: '1', positions: [{ coin: 'BTC', side: 'SHORT' }] };
  const n = await copyAuto.dispatchSignal(sig);
  assert.strictEqual(n, 0);
});

test('copyAuto: dispatchSignal sends to enabled users only', async () => {
  _resetAll();
  copyAutoStore.setEnabled(1001, true);
  copyAutoStore.setEnabled(1002, false);
  copyAutoStore.setMode(1001, 'AUTO');
  const sent = [];
  copyAuto.attach(async (uid, body) => { sent.push({ uid, body }); });
  const sig = {
    signal_id: '12',
    positions: [{ coin: 'BTC', side: 'SHORT' }],
    tokens: ['BTC'],
    sides: ['SHORT'],
    leverage: 2,
  };
  const n = await copyAuto.dispatchSignal(sig);
  assert.strictEqual(n, 1);
  assert.strictEqual(sent.length, 1);
  assert.strictEqual(sent[0].uid, 1001);
});

test('copyAuto: dispatchSignal respects alertsConfig signals=0', async () => {
  _resetAll();
  copyAutoStore.setEnabled(2001, true);
  alertsConfig.setCategory(2001, 'signals', false);
  const sent = [];
  copyAuto.attach(async (uid) => { sent.push(uid); });
  const sig = {
    signal_id: '50',
    positions: [{ coin: 'BTC', side: 'SHORT' }],
    tokens: ['BTC'],
    sides: ['SHORT'],
  };
  const n = await copyAuto.dispatchSignal(sig);
  assert.strictEqual(n, 0);
  assert.strictEqual(sent.length, 0);
});

// ---------- portfolioFetcher ----------

test('portfolioFetcher: isValidAddress rejects non-0x', () => {
  assert.strictEqual(portfolioFetcher.isValidAddress('0x' + 'a'.repeat(40)), true);
  assert.strictEqual(portfolioFetcher.isValidAddress('0xabc'), false);
  assert.strictEqual(portfolioFetcher.isValidAddress('not-an-address'), false);
  assert.strictEqual(portfolioFetcher.isValidAddress(null), false);
  assert.strictEqual(portfolioFetcher.isValidAddress(''), false);
});

test('portfolioFetcher: formatPortfolio renders error for ok:false', () => {
  const out = portfolioFetcher.formatPortfolio({ ok: false, error: 'down' });
  assert.match(out, /down/);
});

test('portfolioFetcher: formatPortfolio renders empty positions', () => {
  const p = {
    ok: true,
    address: '0x' + 'b'.repeat(40),
    equity: 1000,
    marginUsed: 0,
    freeCollateral: 1000,
    positions: [],
  };
  const out = portfolioFetcher.formatPortfolio(p);
  assert.match(out, /Sin posiciones abiertas/);
  assert.match(out, /\$1,000/);
});

test('portfolioFetcher: formatPortfolio renders positions with PnL', () => {
  const p = {
    ok: true,
    address: '0x' + 'c'.repeat(40),
    equity: 5000,
    marginUsed: 1000,
    freeCollateral: 4000,
    positions: [
      { coin: 'BTC', side: 'SHORT', notional: 1000, upnl: 50, leverage: 4 },
      { coin: 'ETH', side: 'LONG', notional: 500, upnl: -10, leverage: 2 },
    ],
  };
  const out = portfolioFetcher.formatPortfolio(p);
  assert.match(out, /BTC.*SHORT/);
  assert.match(out, /ETH.*LONG/);
  assert.match(out, /\+\$50/);
  assert.match(out, /-\$10/);
});

test('portfolioFetcher: fetchPortfolio rejects invalid address', async () => {
  const r = await portfolioFetcher.fetchPortfolio('0xbad');
  assert.strictEqual(r.ok, false);
  assert.match(r.error, /inválida/i);
});

// ---------- leaderboard ----------

test('leaderboard: empty when no wallets', () => {
  _resetAll();
  const lb = leaderboard.getLeaderboard();
  assert.deepStrictEqual(lb, []);
  const fmt = leaderboard.formatLeaderboard(lb);
  assert.match(fmt, /Aún no hay suficientes datos/);
});

test('leaderboard: ranks by tracker count descending', () => {
  _resetAll();
  const a1 = '0x' + 'a'.repeat(40);
  const a2 = '0x' + 'b'.repeat(40);
  wt.addWallet(1, a1, 'whale1');
  wt.addWallet(2, a1, 'whale1');
  wt.addWallet(3, a1, 'whale1');
  wt.addWallet(1, a2, 'whale2');
  wt.addWallet(2, a2, 'whale2');
  const lb = leaderboard.getLeaderboard({ minTrackers: 2 });
  assert.strictEqual(lb.length, 2);
  assert.strictEqual(lb[0].address, a1);
  assert.strictEqual(lb[0].count, 3);
  assert.strictEqual(lb[1].count, 2);
});

test('leaderboard: filters wallets below minTrackers', () => {
  _resetAll();
  const a1 = '0x' + '1'.repeat(40);
  wt.addWallet(99, a1, 'solo');
  const lb = leaderboard.getLeaderboard({ minTrackers: 3 });
  assert.strictEqual(lb.length, 0);
});

test('leaderboard: anonymizes addresses in formatted output', () => {
  _resetAll();
  const a = '0x' + 'd'.repeat(40);
  wt.addWallet(1, a, 'private label');
  wt.addWallet(2, a, 'other label');
  const lb = leaderboard.getLeaderboard({ minTrackers: 2 });
  const fmt = leaderboard.formatLeaderboard(lb);
  assert.doesNotMatch(fmt, /private label/);
  assert.doesNotMatch(fmt, /other label/);
  assert.match(fmt, /0xdddd/);
});

test('leaderboard: resolveAddressByPrefix recovers full addr', () => {
  _resetAll();
  const a = '0x' + 'e'.repeat(40);
  wt.addWallet(1, a, 'x');
  wt.addWallet(2, a, 'y');
  const found = leaderboard.resolveAddressByPrefix(a.slice(2, 10));
  assert.strictEqual(found.toLowerCase(), a.toLowerCase());
});

test('leaderboard: buildKeyboard top-5 only', () => {
  _resetAll();
  for (let i = 0; i < 7; i++) {
    const a = '0x' + i.toString(16).repeat(40);
    wt.addWallet(1, a, 'a');
    wt.addWallet(2, a, 'b');
  }
  const lb = leaderboard.getLeaderboard({ minTrackers: 2 });
  const kb = leaderboard.buildKeyboard(lb);
  assert.ok(kb.inline_keyboard.length <= 5);
});

// ---------- alertsConfig ----------

test('alertsConfig: getConfig returns DEFAULTS_ON for new user', () => {
  _resetAll();
  const cfg = alertsConfig.getConfig(123);
  assert.strictEqual(cfg.basket_open, 1);
  assert.strictEqual(cfg.basket_close, 1);
  assert.strictEqual(cfg.signals, 1);
  assert.strictEqual(cfg.compounding, 0);
  assert.strictEqual(cfg.hf_critical, 0);
  assert.strictEqual(cfg.daily_summary, 0);
});

test('alertsConfig: setCategory persists', () => {
  _resetAll();
  alertsConfig.setCategory(456, 'compounding', true);
  assert.strictEqual(alertsConfig.isAllowed(456, 'compounding'), true);
});

test('alertsConfig: toggle flips current state', () => {
  _resetAll();
  alertsConfig.toggle(789, 'basket_open'); // 1 -> 0
  assert.strictEqual(alertsConfig.isAllowed(789, 'basket_open'), false);
  alertsConfig.toggle(789, 'basket_open'); // 0 -> 1
  assert.strictEqual(alertsConfig.isAllowed(789, 'basket_open'), true);
});

test('alertsConfig: setCategory rejects unknown category', () => {
  _resetAll();
  assert.throws(() => alertsConfig.setCategory(1, 'bogus', true), /Categoría desconocida/);
});

// ---------- share / referrals ----------

test('share: buildReferralLink uses bot username', () => {
  const url = share.buildReferralLink(12345, 'PearProtocolAlertsBot');
  assert.strictEqual(url, 'https://t.me/PearProtocolAlertsBot?start=ref_12345');
});

test('share: parseStartPayload returns id from ref_X', () => {
  assert.strictEqual(share.parseStartPayload('ref_99'), '99');
  assert.strictEqual(share.parseStartPayload('ref_abc'), null);
  assert.strictEqual(share.parseStartPayload(''), null);
  assert.strictEqual(share.parseStartPayload(null), null);
});

test('share: recordReferral idempotent and rejects self-ref', () => {
  _resetAll();
  assert.strictEqual(share.recordReferral(1, 2), true);
  assert.strictEqual(share.recordReferral(1, 2), false); // already exists
  assert.strictEqual(share.recordReferral(3, 3), false); // self
  const s = share.getStats(1);
  assert.strictEqual(s.count, 1);
});

test('share: isPremium flips at threshold (3 referrals)', () => {
  _resetAll();
  share.recordReferral(10, 11);
  share.recordReferral(10, 12);
  assert.strictEqual(share.isPremium(10), false);
  share.recordReferral(10, 13);
  assert.strictEqual(share.isPremium(10), true);
  assert.strictEqual(share.getMaxSlots(10), 25);
});

test('share: non-premium gets DEFAULT_TRACK_SLOTS', () => {
  _resetAll();
  assert.strictEqual(share.getMaxSlots(99), 10);
});

// ---------- learn ----------

test('learn: 5 lessons exist', () => {
  assert.strictEqual(learn.getLessonCount(), 5);
});

test('learn: getLesson by index', () => {
  const l = learn.getLesson(0);
  assert.ok(l);
  assert.match(l.title, /trackear/i);
});

test('learn: getLesson out-of-bounds returns null', () => {
  assert.strictEqual(learn.getLesson(99), null);
  assert.strictEqual(learn.getLesson(-1), null);
});

test('learn: formatLesson includes "Lección N de M"', () => {
  const out = learn.formatLesson(0);
  assert.match(out, /Lección 1 de 5/);
});

test('learn: buildKeyboard pagination — first lesson has no Anterior', () => {
  const kb = learn.buildKeyboard(0);
  const allBtns = kb.inline_keyboard.flat().map((b) => b.text);
  assert.ok(!allBtns.some((t) => t.includes('Anterior')));
  assert.ok(allBtns.some((t) => t.includes('Siguiente')));
});

test('learn: buildKeyboard pagination — last lesson has no Siguiente', () => {
  const kb = learn.buildKeyboard(4);
  const allBtns = kb.inline_keyboard.flat().map((b) => b.text);
  assert.ok(allBtns.some((t) => t.includes('Anterior')));
  assert.ok(!allBtns.some((t) => t.includes('Siguiente')));
});

test('learn: buildIndexKeyboard renders 5 rows', () => {
  const kb = learn.buildIndexKeyboard();
  assert.strictEqual(kb.inline_keyboard.length, 5);
});

// ---------- feedback ----------

test('feedback: ownerConfigured reflects env', () => {
  process.env.OWNER_USER_ID = '999';
  // re-evaluate via fresh require would normally be needed but feedback reads
  // env at call time, so we just check the helper directly.
  assert.strictEqual(feedback._ownerUserId(), 999);
  delete process.env.OWNER_USER_ID;
});

test('feedback: forwardFeedback returns owner_user_id_not_set without env', async () => {
  delete process.env.OWNER_USER_ID;
  delete process.env.BCD_TELEGRAM_CHAT_ID;
  const r = await feedback.forwardFeedback({
    notify: async () => {},
    fromUserId: 1,
    text: 'hello',
  });
  assert.strictEqual(r.ok, false);
  assert.strictEqual(r.error, 'owner_user_id_not_set');
});

test('feedback: forwardFeedback delivers via notify', async () => {
  process.env.OWNER_USER_ID = '12345';
  const calls = [];
  const r = await feedback.forwardFeedback({
    notify: async (uid, body) => { calls.push({ uid, body }); },
    fromUserId: 999,
    fromUsername: 'alice',
    text: 'great bot!',
  });
  assert.strictEqual(r.ok, true);
  assert.strictEqual(calls[0].uid, 12345);
  assert.match(calls[0].body, /FEEDBACK USUARIO/);
  assert.match(calls[0].body, /great bot/);
  assert.match(calls[0].body, /@alice/);
  delete process.env.OWNER_USER_ID;
});

test('feedback: truncate caps at MAX_FEEDBACK_LEN', () => {
  const big = 'x'.repeat(3000);
  const out = feedback.truncate(big);
  assert.ok(out.length <= feedback.MAX_FEEDBACK_LEN + 50);
  assert.match(out, /truncado/);
});

test('feedback: forwardFeedback returns error when notify missing', async () => {
  const r = await feedback.forwardFeedback({ notify: null, fromUserId: 1, text: 'x' });
  assert.strictEqual(r.ok, false);
  assert.strictEqual(r.error, 'notify_not_attached');
});

// ---------- stats ----------

test('stats: touch creates record + getStats returns 1 day minimum', () => {
  _resetAll();
  stats.touch(42);
  const s = stats.getStats(42);
  assert.strictEqual(s.daysActive, 1);
  assert.strictEqual(s.signalsReceived, 0);
});

test('stats: incrementSignal/copy/feedback increments counters', () => {
  _resetAll();
  stats.incrementSignal(7);
  stats.incrementSignal(7);
  stats.incrementCopy(7);
  stats.incrementFeedback(7);
  const s = stats.getStats(7);
  assert.strictEqual(s.signalsReceived, 2);
  assert.strictEqual(s.copyClicks, 1);
  assert.strictEqual(s.feedbackCount, 1);
});

test('stats: formatStats includes user-facing labels', () => {
  _resetAll();
  stats.touch(8);
  const out = stats.formatStats(8);
  assert.match(out, /Bot uso/);
  assert.match(out, /Wallets trackeadas/);
  assert.match(out, /Signals recibidas/);
  assert.match(out, /Trades copiados/);
});

// ---------- dailyDigest ----------

test('dailyDigest: shouldSend false without daily_summary opt-in', () => {
  _resetAll();
  // alertsConfig defaults daily_summary=0
  assert.strictEqual(dailyDigest.shouldSend(1, new Date()), false);
});

test('dailyDigest: shouldSend true at default hour with opt-in & no last_ymd', () => {
  _resetAll();
  alertsConfig.setCategory(11, 'daily_summary', true);
  // Construct a Date that is 9am UTC (since user TZ defaults to UTC)
  const d = new Date(Date.UTC(2026, 0, 1, 9, 0, 0));
  assert.strictEqual(dailyDigest.shouldSend(11, d), true);
});

test('dailyDigest: shouldSend false after markSent same day', () => {
  _resetAll();
  alertsConfig.setCategory(12, 'daily_summary', true);
  const d = new Date(Date.UTC(2026, 0, 1, 9, 0, 0));
  dailyDigest.markSent(12, d);
  assert.strictEqual(dailyDigest.shouldSend(12, d), false);
});

test('dailyDigest: buildDigest renders wallets count', () => {
  _resetAll();
  wt.addWallet(13, '0x' + 'f'.repeat(40), 'foo');
  const body = dailyDigest.buildDigest(13);
  assert.match(body, /Daily digest/);
  assert.match(body, /Wallets trackeadas: 1/);
});

test('dailyDigest: pollOnce sends to opted-in users', async () => {
  _resetAll();
  alertsConfig.setCategory(14, 'daily_summary', true);
  wt.addWallet(14, '0x' + '4'.repeat(40), 'tag');
  const sent = [];
  const fakeNotify = async (uid, body) => { sent.push({ uid, body }); };
  // Use private startSchedule attach by calling internal _notify via exported pollOnce.
  // dailyDigest.pollOnce reads module-private _notify. Since we can't set it directly,
  // start the schedule (immediately stop the timer) then call pollOnce.
  const t = dailyDigest.startSchedule({ notify: fakeNotify });
  if (t && typeof t.unref === 'function') t.unref();
  dailyDigest.stopSchedule();
  const d = new Date(Date.UTC(2026, 0, 2, 9, 0, 0));
  const n = await dailyDigest.pollOnce(d);
  assert.strictEqual(n, 1);
  assert.strictEqual(sent[0].uid, 14);
});

// ---------- sanitizer regression for new modules ----------

test('sanitizer: R-AUTOCOPY new modules pass forbidden-term scan', () => {
  const sanitizer = require('../src/sanitizer');
  const all = sanitizer.collectAllUserFacingStrings();
  const violators = all.filter((s) => sanitizer.findForbiddenInString(s).length > 0);
  if (violators.length > 0) console.error('violators:', violators);
  assert.strictEqual(violators.length, 0);
});

test('sanitizer: @BlackCatDeFiSignals handle is allowed', () => {
  const sanitizer = require('../src/sanitizer');
  assert.deepStrictEqual(
    sanitizer.findForbiddenInString('Suscribite a @BlackCatDeFiSignals'),
    []
  );
});

// ---------- end-to-end signal → dispatch → message ----------

test('e2e: parsed signal flows through dispatchSignal to MANUAL message', async () => {
  _resetAll();
  copyAutoStore.setEnabled(5001, true);
  copyAutoStore.setMode(5001, 'MANUAL');
  copyAutoStore.setCapital(5001, 200);
  const calls = [];
  copyAuto.attach(async (uid, body, opts) => {
    calls.push({ uid, body, opts });
  });
  const text = [
    '🚀 SIGNAL OFICIAL #100',
    '  BTC SHORT',
    '  ETH SHORT',
    'Leverage: 4x',
    '#signal',
  ].join('\n');
  const sig = signalsParser.parseSignal(text);
  const n = await copyAuto.dispatchSignal(sig);
  assert.strictEqual(n, 1);
  assert.strictEqual(calls[0].uid, 5001);
  assert.match(calls[0].body, /\$200 USDC/);
  assert.match(calls[0].body, /BTC SHORT/);
  assert.ok(calls[0].opts.reply_markup);
});

test('e2e: AUTO mode user gets concise auto message', async () => {
  _resetAll();
  copyAutoStore.setEnabled(5002, true);
  copyAutoStore.setMode(5002, 'AUTO');
  copyAutoStore.setCapital(5002, 75);
  let captured = null;
  copyAuto.attach(async (uid, body) => {
    captured = body;
  });
  const sig = signalsParser.parseSignal('#signal SIGNAL OFICIAL #200\n  SOL LONG');
  const n = await copyAuto.dispatchSignal(sig);
  assert.strictEqual(n, 1);
  assert.match(captured, /COPY AUTO/);
  assert.match(captured, /\$75 USDC capital/);
});

test('e2e: PEAR_REFERRAL_CODE preserved in copy URL', async () => {
  _resetAll();
  copyAutoStore.setEnabled(5003, true);
  copyAutoStore.setMode(5003, 'MANUAL');
  let opts = null;
  copyAuto.attach(async (uid, body, o) => { opts = o; });
  const sig = signalsParser.parseSignal('#signal SIGNAL OFICIAL #300\n  ARB SHORT');
  await copyAuto.dispatchSignal(sig);
  const urls = opts.reply_markup.inline_keyboard
    .flat()
    .map((b) => b.url || '')
    .filter(Boolean);
  assert.ok(urls.length > 0);
  assert.ok(urls.every((u) => /referral=BlackCatDeFi/.test(u)));
});
