'use strict';

/**
 * R-PUBLIC-SIMPLIFY regression suite.
 *
 * Covers the brutal-conversion /start UX:
 *   1. Feature flag default ON.
 *   2. Hero message contains the conversion-critical strings (real money,
 *      YTD PnL, 1 tap, fee rebate).
 *   3. Inline keyboard contains the hero CTA + (when basket live) size
 *      selector + 📊 perf + 🔔 alerts buttons.
 *   4. Hero URL embeds Pear referral code + size-aware utm_campaign.
 *   5. simple:perf callback fires a Performance message containing YTD/trades.
 *   6. simple:alerts callback toggles basket_open + basket_close in
 *      alertsConfig (default OFF → ON; idempotent on second tap).
 *   7. End-to-end "time-to-copy" smoke: from /start to having a Pear copy
 *      URL with referral code + capital pre-fill in <= 1 user tap.
 *   8. Network/HL failure must NOT block /start — fallback URL is used and
 *      keyboard still has 3 rows minimum.
 *   9. Feature flag OFF re-routes to the legacy commandsStart UX.
 *
 * Mocks the HyperLiquid API via bcdBasketCache._setCacheForTests so the
 * tests are network-free and deterministic.
 */

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');

// ── sandbox persistent stores ────────────────────────────────────────────────
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'rsimplify-'));
process.env.ONBOARDING_DB_PATH    = path.join(TMP, 'onboarding.json');
process.env.USER_TZ_DB_PATH       = path.join(TMP, 'timezones.json');
process.env.TRACK_DB_PATH         = path.join(TMP, 'wallets.json');
process.env.STATS_DB_PATH         = path.join(TMP, 'stats.json');
process.env.SHARE_DB_PATH         = path.join(TMP, 'share.json');
process.env.ALERTS_CONFIG_DB_PATH = path.join(TMP, 'alerts.json');
process.env.COPY_AUTO_DB_PATH     = path.join(TMP, 'copy_auto.json');
process.env.PEAR_HERO_URL         = 'https://app.pear.garden/?referral=BlackCatDeFi';
process.env.DEFAULT_TZ            = 'UTC';
process.env.ONBOARDING_AUTO_TZ    = 'false';
process.env.PEAR_REFERRAL_CODE    = 'BlackCatDeFi';
process.env.SIMPLIFY_DEFAULT_CAPITAL = '100';
// Default to enabled — the suite explicitly flips off in test 9.
process.env.SIMPLIFY_START_ENABLED = 'true';

const onboarding       = require('../src/onboarding');
const alertsConfig     = require('../src/alertsConfig');
const bcdBasketCache   = require('../src/bcdBasketCache');
const simplifiedStart  = require('../src/simplifiedStart');
const commandsStart    = require('../src/commandsStart');

// ── deterministic basket fixture ────────────────────────────────────────────
const BASKET_FIXTURE = [
  { coin: 'WLD',  side: 'SHORT', notional: 5000 },
  { coin: 'STRK', side: 'SHORT', notional: 3000 },
  { coin: 'ENA',  side: 'SHORT', notional: 2000 },
];

// ── minimal mock bot — captures sent messages + callback queries ────────────
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
      const msg = { chat: { id: 9001 }, from: { id: 9001, language_code: 'en' }, text };
      for (const { regex, fn } of this._textHandlers) {
        if (regex.test(text)) await fn(msg);
      }
    },
    async triggerCallback(data) {
      const cb = { id: 'cbq', data, from: { id: 9001 }, message: { chat: { id: 9001 } } };
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
  process.env.SIMPLIFY_START_ENABLED = 'true';
});

// ─────────────────────────────────────────────────────────────────────────────
// 1. Feature flag default ON
// ─────────────────────────────────────────────────────────────────────────────
test('SIMPLIFY_START_ENABLED defaults to enabled', () => {
  delete process.env.SIMPLIFY_START_ENABLED;
  assert.strictEqual(simplifiedStart.isEnabled(), true);
  process.env.SIMPLIFY_START_ENABLED = 'false';
  assert.strictEqual(simplifiedStart.isEnabled(), false);
  process.env.SIMPLIFY_START_ENABLED = 'true';
});

// ─────────────────────────────────────────────────────────────────────────────
// 2. Hero text contains conversion-critical strings
// ─────────────────────────────────────────────────────────────────────────────
test('hero text mentions real money + YTD PnL + 1 tap + fee rebate', () => {
  const txt = simplifiedStart._heroText();
  assert.match(txt, /Real money/i);
  assert.match(txt, /YTD/i);
  assert.match(txt, /1 tap/i);
  assert.match(txt, /rebate/i);
  assert.match(txt, /Pear/i);
});

// ─────────────────────────────────────────────────────────────────────────────
// 3. Keyboard layout when basket live = 4 rows (hero + size + perf + alerts)
// ─────────────────────────────────────────────────────────────────────────────
test('keyboard has 4 rows when basket is live', async () => {
  const kb = await simplifiedStart._buildKeyboard(9001);
  assert.ok(kb && kb.inline_keyboard);
  assert.strictEqual(kb.inline_keyboard.length, 4);
  // Row 0 = hero, single button.
  assert.strictEqual(kb.inline_keyboard[0].length, 1);
  assert.match(kb.inline_keyboard[0][0].text, /COPY MY BASKET/i);
  assert.ok(kb.inline_keyboard[0][0].url, 'hero must have URL not callback');
  // Row 1 = size selector, 2 buttons.
  assert.strictEqual(kb.inline_keyboard[1].length, 2);
  assert.match(kb.inline_keyboard[1][0].text, /0\.5x/i);
  assert.match(kb.inline_keyboard[1][1].text, /2x/i);
  // Row 2 = perf (callback).
  assert.match(kb.inline_keyboard[2][0].text, /LIVE PERFORMANCE/i);
  assert.strictEqual(kb.inline_keyboard[2][0].callback_data, 'simple:perf');
  // Row 3 = alerts (callback).
  assert.match(kb.inline_keyboard[3][0].text, /ALERT ME/i);
  assert.strictEqual(kb.inline_keyboard[3][0].callback_data, 'simple:alerts');
});

// ─────────────────────────────────────────────────────────────────────────────
// 4. Hero URL embeds referral + capital pre-fill + utm campaign
// ─────────────────────────────────────────────────────────────────────────────
test('hero URL contains referral code + capital + size utm_campaign', async () => {
  const kb = await simplifiedStart._buildKeyboard(9001);
  const heroUrl = kb.inline_keyboard[0][0].url;
  assert.ok(heroUrl.includes('referral=BlackCatDeFi'));
  assert.ok(heroUrl.includes('utm_source=tg-start-hero'));
  assert.ok(heroUrl.includes('utm_campaign=size-1x'));
  // Default capital $100 must appear in pre-fill aliases.
  assert.ok(/[?&](amount|size|capital)=100\b/.test(heroUrl));
  // Tokens should include the basket coins.
  assert.ok(heroUrl.includes('WLD'));
});

// ─────────────────────────────────────────────────────────────────────────────
// 5. simple:perf callback fires a Performance message
// ─────────────────────────────────────────────────────────────────────────────
test('simple:perf callback sends performance text with YTD/trades', async () => {
  const bot = makeMockBot();
  commandsStart.attach(bot);
  await bot.trigger('/start');
  bot.sent.length = 0; // clear /start message

  await bot.triggerCallback('simple:perf');

  assert.strictEqual(bot.sent.length, 1);
  const m = bot.sent[0];
  assert.match(m.text, /Performance/i);
  assert.match(m.text, /YTD/i);
  assert.match(m.text, /trades/i);
  assert.match(m.text, /HyperDash/i);
});

// ─────────────────────────────────────────────────────────────────────────────
// 6. simple:alerts toggle: OFF → ON, then ON → OFF (idempotent reverse)
// ─────────────────────────────────────────────────────────────────────────────
test('simple:alerts toggles basket_open + basket_close categories', async () => {
  const bot = makeMockBot();
  commandsStart.attach(bot);
  await bot.trigger('/start');
  bot.sent.length = 0;

  // Initial state: both ON (alertsConfig DEFAULTS_ON.basket_open=1,
  // basket_close=1) — first tap should turn them OFF.
  await bot.triggerCallback('simple:alerts');
  assert.strictEqual(bot.sent.length, 1);
  assert.match(bot.sent[0].text, /OFF/i);
  let cfg = alertsConfig.getConfig(9001);
  assert.strictEqual(cfg.basket_open, 0);
  assert.strictEqual(cfg.basket_close, 0);

  // Second tap → back ON.
  await bot.triggerCallback('simple:alerts');
  assert.strictEqual(bot.sent.length, 2);
  assert.match(bot.sent[1].text, /ON/i);
  cfg = alertsConfig.getConfig(9001);
  assert.strictEqual(cfg.basket_open, 1);
  assert.strictEqual(cfg.basket_close, 1);
});

// ─────────────────────────────────────────────────────────────────────────────
// 7. Time-to-copy smoke: /start → user has tappable Pear URL with referral
// ─────────────────────────────────────────────────────────────────────────────
test('time-to-copy is one tap: /start hero URL is the copy URL', async () => {
  const bot = makeMockBot();
  commandsStart.attach(bot);

  const tStart = Date.now();
  await bot.trigger('/start');
  const tEnd = Date.now();

  // Local mock — should be near-instant. Live Telegram round-trip is in the
  // 150–500 ms range, well under 30s. We assert the deterministic <2s here.
  assert.ok(tEnd - tStart < 2000, `/start took ${tEnd - tStart} ms`);

  assert.strictEqual(bot.sent.length, 1);
  const kb = bot.sent[0].opts.reply_markup;
  // Row 0 button is a URL (1-tap) NOT a callback.
  const hero = kb.inline_keyboard[0][0];
  assert.ok(hero.url, 'hero must be a URL button — no extra tap required');
  assert.ok(!hero.callback_data, 'hero must NOT be a callback');
  // URL must include referral code + a Pear basket path so a copy-trade
  // executes immediately when tapped.
  assert.ok(/referral=BlackCatDeFi/.test(hero.url));
  assert.ok(/\/trade\/hl\/USDC-/.test(hero.url));
});

// ─────────────────────────────────────────────────────────────────────────────
// 8. Network/HL failure: empty cache → fallback URL + keyboard still rendered
// ─────────────────────────────────────────────────────────────────────────────
test('empty basket falls back to PEAR_HERO_URL without crashing', async () => {
  bcdBasketCache._resetForTests(); // empties cache + drops api ref
  // Force getActiveBasket to return [] without touching the network.
  const original = bcdBasketCache.getActiveBasket;
  bcdBasketCache.getActiveBasket = async () => [];

  try {
    const bot = makeMockBot();
    commandsStart.attach(bot);
    await bot.trigger('/start');

    assert.strictEqual(bot.sent.length, 1);
    const kb = bot.sent[0].opts.reply_markup;
    // Without a live basket: hero + perf + alerts = 3 rows (no size selector).
    assert.strictEqual(kb.inline_keyboard.length, 3);
    const hero = kb.inline_keyboard[0][0];
    assert.ok(hero.url, 'fallback hero must still be URL-tappable');
    assert.ok(/referral=BlackCatDeFi/.test(hero.url));
    assert.match(hero.text, /OPEN PEAR/i);
  } finally {
    bcdBasketCache.getActiveBasket = original;
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// 9. Feature flag OFF re-routes to legacy /start (Welcome back / Track wallet)
// ─────────────────────────────────────────────────────────────────────────────
test('SIMPLIFY_START_ENABLED=false uses legacy commandsStart flow', async () => {
  process.env.SIMPLIFY_START_ENABLED = 'false';
  // The handler reads the env on every call — no module re-require needed.

  const bot = makeMockBot();
  commandsStart.attach(bot);
  await bot.trigger('/start');

  assert.strictEqual(bot.sent.length, 1);
  // Legacy first-time text mentions "on-chain trading copilot".
  assert.match(bot.sent[0].text, /on-chain trading copilot/i);
});

// ─────────────────────────────────────────────────────────────────────────────
// 10. /start trigger sends exactly one Markdown message
// ─────────────────────────────────────────────────────────────────────────────
test('/start sends exactly one Markdown message with simplified UX', async () => {
  const bot = makeMockBot();
  commandsStart.attach(bot);
  await bot.trigger('/start');

  assert.strictEqual(bot.sent.length, 1);
  const m = bot.sent[0];
  assert.strictEqual(m.opts.parse_mode, 'Markdown');
  assert.strictEqual(m.opts.disable_web_page_preview, true);
  assert.match(m.text, /Black Cat/i);
  assert.match(m.text, /Pear/i);
});
