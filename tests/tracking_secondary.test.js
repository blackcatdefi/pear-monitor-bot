'use strict';

/**
 * R-PUBLIC-V3-TRACKING — secondary tracking row regression suite.
 *
 * Asserts:
 *   1. Hero rows render BEFORE the tracking row (conversion never lost).
 *   2. The tracking row contains exactly 2 buttons (👁 Track, 🛡 HF).
 *   3. simple:track callback sends a Track submenu (Add / List / Remove).
 *   4. simple:track_add transitions the chat into AWAITING_WALLET_ADDRESS.
 *   5. simple:hf transitions the chat into AWAITING_HF_ADDRESS.
 *   6. Pyrus is NOT mentioned anywhere in simplifiedStart's defaults.
 *   7. Hero text + perf text contain only the Pear-only rebate copy.
 *
 * Mocks bcdBasketCache via _setCacheForTests so the suite is network-free.
 */

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'rtrack2-'));
process.env.ONBOARDING_DB_PATH    = path.join(TMP, 'onboarding.json');
process.env.USER_TZ_DB_PATH       = path.join(TMP, 'timezones.json');
process.env.TRACK_DB_PATH         = path.join(TMP, 'wallets.json');
process.env.STATS_DB_PATH         = path.join(TMP, 'stats.json');
process.env.SHARE_DB_PATH         = path.join(TMP, 'share.json');
process.env.ALERTS_CONFIG_DB_PATH = path.join(TMP, 'alerts.json');
process.env.COPY_AUTO_DB_PATH     = path.join(TMP, 'copy_auto.json');
process.env.HF_CACHE_PATH         = path.join(TMP, 'hf_cache.json');
process.env.PEAR_HERO_URL         = 'https://app.pear.garden/?referral=BlackCatDeFi';
process.env.PEAR_REFERRAL_CODE    = 'BlackCatDeFi';
process.env.SIMPLIFY_DEFAULT_CAPITAL = '100';
process.env.SIMPLIFY_START_ENABLED = 'true';
delete process.env.FUND_REBATE_LINE; // exercise the source default

const onboarding      = require('../src/onboarding');
const alertsConfig    = require('../src/alertsConfig');
const bcdBasketCache  = require('../src/bcdBasketCache');
const simplifiedStart = require('../src/simplifiedStart');
const commandsStart   = require('../src/commandsStart');
const sm              = require('../src/userStateMachine');

const BASKET_FIXTURE = [
  { coin: 'WLD',  side: 'SHORT', notional: 5000 },
  { coin: 'STRK', side: 'SHORT', notional: 3000 },
  { coin: 'ENA',  side: 'SHORT', notional: 2000 },
];

function makeMockBot() {
  const bot = {
    sent: [],
    cbAnswered: 0,
    _textHandlers: [],
    _cbHandlers: [],
    onText(regex, fn) { this._textHandlers.push({ regex, fn }); },
    on(event, fn) { if (event === 'callback_query') this._cbHandlers.push(fn); },
    sendMessage: async (chatId, text, opts) => {
      bot.sent.push({ chatId, text, opts });
      return { message_id: bot.sent.length };
    },
    answerCallbackQuery: async () => { bot.cbAnswered += 1; },
    async trigger(text) {
      const msg = { chat: { id: 9101 }, from: { id: 9101, language_code: 'en' }, text };
      for (const { regex, fn } of this._textHandlers) {
        if (regex.test(text)) await fn(msg);
      }
    },
    async triggerCallback(data) {
      const cb = { id: 'cbq', data, from: { id: 9101 }, message: { chat: { id: 9101 } } };
      for (const fn of this._cbHandlers) await fn(cb);
    },
  };
  return bot;
}

test.beforeEach(() => {
  onboarding._resetForTests();
  alertsConfig._resetForTests(process.env.ALERTS_CONFIG_DB_PATH);
  bcdBasketCache._resetForTests();
  bcdBasketCache._setCacheForTests(BASKET_FIXTURE);
  sm._resetForTests();
  process.env.SIMPLIFY_START_ENABLED = 'true';
});

// Helper — locate the secondary tracking row regardless of layout drift.
function _findTrackingRow(kb) {
  return kb.inline_keyboard.find(
    (row) =>
      row.some((b) => b.callback_data === 'simple:track') &&
      row.some((b) => b.callback_data === 'simple:hf')
  );
}

// 1. Hero rows render BEFORE the tracking row (order matters for conversion).
test('keyboard order: hero/size/perf/alerts come BEFORE the tracking row', async () => {
  const kb = await simplifiedStart._buildKeyboard(9101);
  assert.ok(kb && Array.isArray(kb.inline_keyboard));
  // R-PUBLIC-V4-COPYMENU layout: hero + size + perf + alerts + tracking +
  // copy_trading + community = 7 rows.
  assert.strictEqual(kb.inline_keyboard.length, 7);
  // Row 0 must be the hero (URL button, not a tracking callback).
  assert.match(kb.inline_keyboard[0][0].text, /COPY MY BASKET/i);
  assert.ok(kb.inline_keyboard[0][0].url);
  // Tracking row must come AFTER hero/size/perf/alerts.
  const trackingIdx = kb.inline_keyboard.findIndex(
    (row) => row.some((b) => b.callback_data === 'simple:track')
  );
  assert.ok(trackingIdx >= 4, 'tracking row must be at index >= 4');
  const trackRow = kb.inline_keyboard[trackingIdx];
  assert.ok(trackRow.some((b) => /TRACK/i.test(b.text)));
  assert.ok(trackRow.some((b) => /HEALTH FACTOR/i.test(b.text)));
  // V4 last row must be the community-links URL row.
  const last = kb.inline_keyboard[kb.inline_keyboard.length - 1];
  assert.ok(last.every((b) => b.url));
  assert.ok(last.some((b) => /Signals Channel/i.test(b.text)));
  assert.ok(last.some((b) => /Thesis Channel/i.test(b.text)));
});

// 2. Secondary row has exactly 2 buttons.
test('secondary row contains exactly 2 buttons (track + HF)', async () => {
  const kb = await simplifiedStart._buildKeyboard(9101);
  const trackRow = _findTrackingRow(kb);
  assert.ok(trackRow, 'tracking row must exist');
  assert.strictEqual(trackRow.length, 2);
  assert.strictEqual(trackRow[0].callback_data, 'simple:track');
  assert.strictEqual(trackRow[1].callback_data, 'simple:hf');
});

// 3. Even when there is NO live basket, the tracking row still appears.
test('tracking row renders even on empty basket fallback', async () => {
  bcdBasketCache._resetForTests();
  const original = bcdBasketCache.getActiveBasket;
  bcdBasketCache.getActiveBasket = async () => [];
  try {
    const kb = await simplifiedStart._buildKeyboard(9101);
    // R-PUBLIC-V4-COPYMENU empty-basket layout: hero + perf + alerts +
    // tracking + copy_trading + community = 6 rows.
    assert.strictEqual(kb.inline_keyboard.length, 6);
    const trackRow = _findTrackingRow(kb);
    assert.ok(trackRow, 'tracking row must exist on empty basket fallback');
    assert.strictEqual(trackRow.length, 2);
    assert.strictEqual(trackRow[0].callback_data, 'simple:track');
    assert.strictEqual(trackRow[1].callback_data, 'simple:hf');
  } finally {
    bcdBasketCache.getActiveBasket = original;
  }
});

// 4. simple:track sends a submenu (Add / List / Remove).
test('simple:track callback sends a submenu with 3 callback buttons', async () => {
  const bot = makeMockBot();
  commandsStart.attach(bot);
  await bot.trigger('/start');
  bot.sent.length = 0;

  await bot.triggerCallback('simple:track');
  assert.strictEqual(bot.sent.length, 1);
  const m = bot.sent[0];
  assert.match(m.text, /Track your own wallet/i);
  const kb = m.opts.reply_markup;
  assert.ok(kb && kb.inline_keyboard);
  const flat = kb.inline_keyboard.flat();
  assert.ok(flat.some(b => b.callback_data === 'simple:track_add'));
  assert.ok(flat.some(b => b.callback_data === 'simple:track_list'));
  assert.ok(flat.some(b => b.callback_data === 'simple:track_remove'));
});

// 5. simple:track_add transitions the chat into AWAITING_WALLET_ADDRESS.
test('simple:track_add sets state AWAITING_WALLET_ADDRESS', async () => {
  const bot = makeMockBot();
  commandsStart.attach(bot);
  await bot.trigger('/start');
  bot.sent.length = 0;

  await bot.triggerCallback('simple:track_add');
  const rec = sm.getState(9101);
  assert.strictEqual(rec.state, sm.STATES.AWAITING_WALLET_ADDRESS);
  assert.match(bot.sent[bot.sent.length - 1].text, /paste a `0x/i);
});

// 6. simple:hf transitions the chat into AWAITING_HF_ADDRESS.
test('simple:hf sets state AWAITING_HF_ADDRESS + sends prompt', async () => {
  const bot = makeMockBot();
  commandsStart.attach(bot);
  await bot.trigger('/start');
  bot.sent.length = 0;

  await bot.triggerCallback('simple:hf');
  const rec = sm.getState(9101);
  assert.strictEqual(rec.state, sm.STATES.AWAITING_HF_ADDRESS);
  assert.match(bot.sent[bot.sent.length - 1].text, /Health Factor reader/i);
});

// 7. Pyrus copy purge: source defaults must not contain "Pyrus" or "20%".
test('simplifiedStart source defaults do NOT mention Pyrus or 20%', () => {
  const txt = fs.readFileSync(path.join(__dirname, '..', 'src', 'simplifiedStart.js'), 'utf-8');
  // Strip all comment blocks first — historical notes about the rationale
  // for the purge are allowed there. Code-level defaults must be clean.
  const noComments = txt
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:])\/\/.*$/gm, '$1');
  const heroRebate = simplifiedStart.FUND_YTD_PNL; // tickle export
  void heroRebate;
  // Source default rebate line must be Pear-only.
  assert.match(
    noComments,
    /'10% fee rebate via Pear \(referral: BlackCatDeFi\)'/,
    'Pear-only rebate string must be the default'
  );
  // No Pyrus mention in CODE (literals, regex, etc).
  // We use a code-only window — pull each string literal and check it.
  const literalRe = /['"`]([^'"`\n]*)['"`]/g;
  let m;
  while ((m = literalRe.exec(noComments)) !== null) {
    assert.ok(
      !/pyrus/i.test(m[1]),
      `Pyrus must not appear in code literal: ${m[1]}`
    );
    assert.ok(
      !/20%\s+fee\s+rebate/i.test(m[1]),
      `20% rebate copy must not appear in code literal: ${m[1]}`
    );
  }
});

// 8. Hero + perf text contain only the Pear-only rebate copy.
test('hero + perf text mention Pear referral, not Pyrus, not 20% rebate', () => {
  const hero = simplifiedStart._heroText();
  const perf = simplifiedStart._perfText();
  assert.match(hero, /Pear/i);
  assert.match(hero, /referral|rebate/i);
  assert.ok(!/Pyrus/i.test(hero), `Pyrus leaked into hero text: ${hero}`);
  assert.ok(!/20%\s+fee/i.test(hero));
  assert.ok(!/Pyrus/i.test(perf), `Pyrus leaked into perf text: ${perf}`);
  assert.ok(!/20%\s+fee/i.test(perf));
});
