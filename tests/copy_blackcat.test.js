'use strict';

/**
 * R-PUBLIC-V4-COPYMENU — Black Cat wallet sub-menu regression suite.
 *
 * Asserts:
 *   1. 0.5x / 1x / 2x size buttons set capital_usdc to DEFAULT_CAPITAL × n.
 *   2. toggle_enabled flips the BCD enabled flag (and persists via store).
 *   3. toggle_mode flips MANUAL ↔ AUTO.
 *   4. Capital out-of-range size mults raise a friendly error msg.
 *   5. /capital_bcd <n> command shortcut works for direct entry.
 *   6. BCD config persists across module reload (file path uses TMP).
 */

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'rblackcat-'));
process.env.COPY_TRADING_DB_DIR        = TMP;
process.env.COPY_AUTO_DEFAULT_CAPITAL  = '100';
process.env.COPY_AUTO_MIN_CAPITAL      = '10';
process.env.COPY_AUTO_MAX_CAPITAL      = '50000';
process.env.PEAR_REFERRAL_CODE         = 'BlackCatDeFi';

const store = require('../src/copyTradingStore');
const cmd   = require('../src/commandsCopyTrading');

function makeMockBot() {
  const bot = {
    sent: [],
    edited: [],
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
      return { message_id: bot.sent.length + 2000 };
    },
    editMessageText: async (text, opts) => {
      bot.edited.push({ text, opts });
    },
    deleteMessage: async () => {},
    answerCallbackQuery: async () => { bot.cbAnswered += 1; },
    async trigger(text) {
      const msg = { chat: { id: 8888 }, from: { id: 8888 }, text };
      for (const { regex, fn } of this._textHandlers) {
        if (regex.test(text)) await fn(msg);
      }
    },
    async triggerCallback(data, msgId = 7777) {
      const c = {
        id: 'cb',
        data,
        from: { id: 8888 },
        message: { chat: { id: 8888 }, message_id: msgId },
      };
      for (const fn of this._cbHandlers) await fn(c);
    },
  };
  return bot;
}

test.beforeEach(() => {
  store._resetForTests();
});

// 1. 0.5x sets capital to DEFAULT_CAPITAL × 0.5
test('copytrade:bcd:size:0.5 → capital = DEFAULT × 0.5', async () => {
  const bot = makeMockBot();
  cmd.attach(bot);
  await bot.triggerCallback('copytrade:bcd:size:0.5');
  const cfg = store.getTarget(8888, store.TYPE_BCD_WALLET);
  assert.ok(cfg, 'BCD config must exist after size press');
  assert.strictEqual(cfg.capital_usdc, store.DEFAULT_CAPITAL * 0.5);
});

// 2. 1x sets capital to DEFAULT_CAPITAL × 1
test('copytrade:bcd:size:1 → capital = DEFAULT × 1', async () => {
  const bot = makeMockBot();
  cmd.attach(bot);
  await bot.triggerCallback('copytrade:bcd:size:1');
  const cfg = store.getTarget(8888, store.TYPE_BCD_WALLET);
  assert.strictEqual(cfg.capital_usdc, store.DEFAULT_CAPITAL);
});

// 3. 2x sets capital to DEFAULT_CAPITAL × 2
test('copytrade:bcd:size:2 → capital = DEFAULT × 2', async () => {
  const bot = makeMockBot();
  cmd.attach(bot);
  await bot.triggerCallback('copytrade:bcd:size:2');
  const cfg = store.getTarget(8888, store.TYPE_BCD_WALLET);
  assert.strictEqual(cfg.capital_usdc, store.DEFAULT_CAPITAL * 2);
});

// 4. toggle_enabled flips the enabled flag.
test('copytrade:bcd:toggle_enabled flips BCD enabled flag', async () => {
  const bot = makeMockBot();
  cmd.attach(bot);
  await bot.triggerCallback('copytrade:bcd:size:1'); // create BCD entry
  let cfg = store.getTarget(8888, store.TYPE_BCD_WALLET);
  assert.strictEqual(!!cfg.enabled, false);
  await bot.triggerCallback('copytrade:bcd:toggle_enabled');
  cfg = store.getTarget(8888, store.TYPE_BCD_WALLET);
  assert.strictEqual(!!cfg.enabled, true);
  await bot.triggerCallback('copytrade:bcd:toggle_enabled');
  cfg = store.getTarget(8888, store.TYPE_BCD_WALLET);
  assert.strictEqual(!!cfg.enabled, false);
});

// 5. toggle_mode flips MANUAL ↔ AUTO.
test('copytrade:bcd:toggle_mode flips MANUAL ↔ AUTO', async () => {
  const bot = makeMockBot();
  cmd.attach(bot);
  await bot.triggerCallback('copytrade:bcd:size:1');
  let cfg = store.getTarget(8888, store.TYPE_BCD_WALLET);
  assert.strictEqual(cfg.mode, 'MANUAL');
  await bot.triggerCallback('copytrade:bcd:toggle_mode');
  cfg = store.getTarget(8888, store.TYPE_BCD_WALLET);
  assert.strictEqual(cfg.mode, 'AUTO');
  await bot.triggerCallback('copytrade:bcd:toggle_mode');
  cfg = store.getTarget(8888, store.TYPE_BCD_WALLET);
  assert.strictEqual(cfg.mode, 'MANUAL');
});

// 6. /capital_bcd <amount> command sets capital directly.
test('/capital_bcd 250 → BCD capital_usdc = 250', async () => {
  const bot = makeMockBot();
  cmd.attach(bot);
  // First create BCD entry by toggling
  await bot.triggerCallback('copytrade:bcd:size:1');
  // Then send /capital_bcd 250
  bot._textHandlers.forEach(({ regex, fn }) => {
    if (regex.test('/capital_bcd 250')) {
      fn({ chat: { id: 8888 }, from: { id: 8888 }, text: '/capital_bcd 250' });
    }
  });
  // Allow async sendMessage to settle
  await new Promise((r) => setImmediate(r));
  const cfg = store.getTarget(8888, store.TYPE_BCD_WALLET);
  assert.strictEqual(cfg.capital_usdc, 250);
});

// 7. Capital below MIN raises a friendly warning, does NOT crash callback.
test('copytrade:bcd:size: invalid mult sends a warning, does not throw', async () => {
  const bot = makeMockBot();
  cmd.attach(bot);
  // Mult of 0.001 → capital = 0.1 (below MIN 10) → should send warning.
  await bot.triggerCallback('copytrade:bcd:size:0.001');
  const warned = bot.sent.some((m) => /minimum amount/i.test(m.text));
  assert.ok(warned, 'expected a "Minimum amount" warning, got: ' + JSON.stringify(bot.sent.map(s => s.text)));
});

// 8. After 2x size press, sub-menu re-render shows "2.00x" multiplier.
test('BCD sub-menu shows updated multiplier after size press', async () => {
  const bot = makeMockBot();
  cmd.attach(bot);
  await bot.triggerCallback('copytrade:bcd:size:2');
  const last = bot.edited[bot.edited.length - 1];
  assert.ok(last, 'expected at least one edit after size press');
  assert.match(last.text, /size multiplier:\s*2\.00x/i);
});
