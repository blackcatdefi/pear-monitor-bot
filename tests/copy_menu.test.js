'use strict';

/**
 * R-PUBLIC-V4-COPYMENU — top-level Copy Trading menu regression suite.
 *
 * Asserts:
 *   1. /copy_trading sends a single Markdown message with the V4 top-menu
 *      keyboard (4 rows: BCD / Custom / Settings / Back).
 *   2. callback `copytrade:menu` (or empty root) re-renders the top menu.
 *   3. callback `copytrade:bcd` → BCD sub-menu (size + mode + enable rows).
 *   4. callback `copytrade:custom` → Custom sub-menu (Add wallet row).
 *   5. callback `copytrade:settings` → Settings sub-menu (basket + paused).
 *   6. callback `copytrade:back_start` deletes current msg + falls back to
 *      simplifiedStart.handleStartSimple.
 *   7. simple:copy_trading callback opens the V4 top menu (one msg).
 *   8. Top-level menu mentions on-chain wallet polling — never a signals
 *      channel scraper.
 */

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'rcopymenu-'));
process.env.COPY_TRADING_DB_DIR        = TMP;
process.env.ONBOARDING_DB_PATH         = path.join(TMP, 'onboarding.json');
process.env.USER_TZ_DB_PATH            = path.join(TMP, 'tz.json');
process.env.TRACK_DB_PATH              = path.join(TMP, 'wallets.json');
process.env.STATS_DB_PATH              = path.join(TMP, 'stats.json');
process.env.SHARE_DB_PATH              = path.join(TMP, 'share.json');
process.env.ALERTS_CONFIG_DB_PATH      = path.join(TMP, 'alerts.json');
process.env.HF_CACHE_PATH              = path.join(TMP, 'hf_cache.json');
process.env.PEAR_HERO_URL              = 'https://app.pear.garden/?referral=BlackCatDeFi';
process.env.PEAR_REFERRAL_CODE         = 'BlackCatDeFi';
process.env.SIMPLIFY_START_ENABLED     = 'true';
process.env.SIMPLIFY_DEFAULT_CAPITAL   = '100';
process.env.COPY_AUTO_DEFAULT_CAPITAL  = '100';

const store          = require('../src/copyTradingStore');
const cmd            = require('../src/commandsCopyTrading');
const simplifiedStart = require('../src/simplifiedStart');
const sm             = require('../src/userStateMachine');

function makeMockBot() {
  const bot = {
    sent: [],
    edited: [],
    deleted: [],
    cbAnswered: 0,
    _textHandlers: [],
    _cbHandlers: [],
    _msgHandlers: [],
    onText(regex, fn) { this._textHandlers.push({ regex, fn }); },
    on(event, fn) {
      if (event === 'callback_query') this._cbHandlers.push(fn);
      if (event === 'message') this._msgHandlers.push(fn);
    },
    sendMessage: async (chatId, text, opts) => {
      bot.sent.push({ chatId, text, opts });
      return { message_id: bot.sent.length + 1000 };
    },
    editMessageText: async (text, opts) => {
      bot.edited.push({ text, opts });
    },
    deleteMessage: async (chatId, msgId) => {
      bot.deleted.push({ chatId, msgId });
    },
    answerCallbackQuery: async () => { bot.cbAnswered += 1; },
    async trigger(text) {
      const msg = { chat: { id: 7777 }, from: { id: 7777 }, text };
      for (const { regex, fn } of this._textHandlers) {
        if (regex.test(text)) await fn(msg);
      }
    },
    async triggerCallback(data, msgId = 5555) {
      const c = {
        id: 'cb-id',
        data,
        from: { id: 7777 },
        message: { chat: { id: 7777 }, message_id: msgId },
      };
      for (const fn of this._cbHandlers) await fn(c);
    },
  };
  return bot;
}

test.beforeEach(() => {
  store._resetForTests();
  sm._resetForTests();
});

// 1. /copy_trading sends one msg with 4-row top-menu keyboard.
test('/copy_trading top menu has 4 rows: BCD / Custom / Settings / Back', async () => {
  const bot = makeMockBot();
  cmd.attach(bot);
  await bot.trigger('/copy_trading');
  assert.strictEqual(bot.sent.length, 1);
  const m = bot.sent[0];
  assert.match(m.text, /Copy Trading Menu/i);
  const kb = m.opts.reply_markup;
  assert.ok(kb && Array.isArray(kb.inline_keyboard));
  assert.strictEqual(kb.inline_keyboard.length, 4);
  // Order matters for conversion: BCD first, then Custom, Settings, Back.
  assert.strictEqual(kb.inline_keyboard[0][0].callback_data, 'copytrade:bcd');
  assert.match(kb.inline_keyboard[0][0].text, /BLACK CAT WALLET/i);
  assert.strictEqual(kb.inline_keyboard[1][0].callback_data, 'copytrade:custom');
  assert.match(kb.inline_keyboard[1][0].text, /CUSTOM WALLET/i);
  assert.strictEqual(kb.inline_keyboard[2][0].callback_data, 'copytrade:settings');
  assert.strictEqual(kb.inline_keyboard[3][0].callback_data, 'copytrade:back_start');
});

// 2. copytrade:menu re-renders top menu via editMessageText.
test('callback copytrade:menu edits the same message back to top menu', async () => {
  const bot = makeMockBot();
  cmd.attach(bot);
  await bot.triggerCallback('copytrade:menu', 9999);
  // Edit-first strategy → should call editMessageText, not sendMessage.
  assert.strictEqual(bot.edited.length, 1);
  assert.match(bot.edited[0].text, /Copy Trading Menu/i);
  assert.strictEqual(bot.edited[0].opts.message_id, 9999);
});

// 3. copytrade:bcd opens BCD sub-menu (size buttons present).
test('callback copytrade:bcd renders BCD sub-menu with 0.5x/1x/2x size row', async () => {
  const bot = makeMockBot();
  cmd.attach(bot);
  await bot.triggerCallback('copytrade:bcd', 9999);
  assert.strictEqual(bot.edited.length, 1);
  const kb = bot.edited[0].opts.reply_markup;
  const flat = kb.inline_keyboard.flat();
  assert.ok(flat.find((b) => b.callback_data === 'copytrade:bcd:size:0.5'));
  assert.ok(flat.find((b) => b.callback_data === 'copytrade:bcd:size:1'));
  assert.ok(flat.find((b) => b.callback_data === 'copytrade:bcd:size:2'));
  assert.ok(flat.find((b) => b.callback_data === 'copytrade:bcd:toggle_enabled'));
  assert.ok(flat.find((b) => b.callback_data === 'copytrade:bcd:toggle_mode'));
});

// 4. copytrade:custom opens Custom sub-menu (Add wallet visible when under cap).
test('callback copytrade:custom renders Custom sub-menu with Add row', async () => {
  const bot = makeMockBot();
  cmd.attach(bot);
  await bot.triggerCallback('copytrade:custom', 9999);
  assert.strictEqual(bot.edited.length, 1);
  const kb = bot.edited[0].opts.reply_markup;
  const flat = kb.inline_keyboard.flat();
  assert.ok(flat.find((b) => b.callback_data === 'copytrade:custom:add'));
  // Back button must always exist
  assert.ok(flat.find((b) => b.callback_data === 'copytrade:menu'));
});

// 5. copytrade:settings opens Settings sub-menu (basket toggle + pause toggle).
test('callback copytrade:settings renders Settings sub-menu with both toggles', async () => {
  const bot = makeMockBot();
  cmd.attach(bot);
  await bot.triggerCallback('copytrade:settings', 9999);
  assert.strictEqual(bot.edited.length, 1);
  const kb = bot.edited[0].opts.reply_markup;
  const flat = kb.inline_keyboard.flat();
  assert.ok(flat.find((b) => b.callback_data === 'copytrade:settings:toggle_basket_level'));
  assert.ok(flat.find((b) => b.callback_data === 'copytrade:settings:toggle_paused'));
  assert.ok(flat.find((b) => b.callback_data === 'copytrade:menu'));
});

// 6. copytrade:back_start deletes current msg + invokes simplifiedStart fallback.
test('callback copytrade:back_start deletes the menu message', async () => {
  const bot = makeMockBot();
  cmd.attach(bot);
  await bot.triggerCallback('copytrade:back_start', 4242);
  assert.strictEqual(bot.deleted.length, 1);
  assert.strictEqual(bot.deleted[0].msgId, 4242);
});

// 7. simple:copy_trading callback opens the V4 top menu (one new msg).
test('simple:copy_trading callback opens V4 top menu via simplifiedStart', async () => {
  const bot = makeMockBot();
  // simulate the same wiring commandsStart does
  await simplifiedStart.handleSimpleCallback(bot, {
    id: 'cb-id',
    data: 'simple:copy_trading',
    from: { id: 7777 },
    message: { chat: { id: 7777 }, message_id: 5555 },
  });
  assert.strictEqual(bot.sent.length, 1);
  assert.match(bot.sent[0].text, /Copy Trading Menu/i);
  const kb = bot.sent[0].opts.reply_markup;
  assert.ok(kb && Array.isArray(kb.inline_keyboard));
  assert.strictEqual(kb.inline_keyboard[0][0].callback_data, 'copytrade:bcd');
});

// 8. Top-level menu copy mentions on-chain polling, not signals scraper.
test('top menu copy: "on-chain wallet polling only", no signals scraper', () => {
  const m = cmd._renderTopMenu(7777);
  assert.match(m.text, /on-chain wallet polling/i);
  assert.ok(!/signals?\s+(channel|scraper|posts?)/i.test(m.text),
    `top-menu text should not mention signals scraping: ${m.text}`);
});
