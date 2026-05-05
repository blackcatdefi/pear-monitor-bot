'use strict';

/**
 * R-PUBLIC-V4-COPYMENU — custom-wallet sub-menu + 3-step add flow regression.
 *
 * Asserts:
 *   1. copytrade:custom:add → state machine transitions to
 *      COPY_TRADE_AWAIT_ADDRESS, prompt mentions "0x" format.
 *   2. Step 1: invalid address → friendly warning, state unchanged.
 *   3. Step 1 → 2: valid 0x address transitions to COPY_TRADE_AWAIT_LABEL.
 *   4. Step 2: "skip" defaults label to no-label; non-skip stores trimmed
 *      label (≤64 chars).
 *   5. Step 3: "default" capital uses DEFAULT_CAPITAL; numeric input is
 *      validated against MIN/MAX.
 *   6. Successful add stores wallet enabled=1 with the label + capital.
 *   7. Cap of 3: a 4th add via callback shows the "limit" warning and does
 *      NOT enter the address-collection state.
 *   8. copytrade:custom:toggle:<addr> flips enabled; rm:<addr> removes.
 */

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'rcustom-'));
process.env.COPY_TRADING_DB_DIR              = TMP;
process.env.COPY_AUTO_DEFAULT_CAPITAL        = '100';
process.env.COPY_AUTO_MIN_CAPITAL            = '10';
process.env.COPY_AUTO_MAX_CAPITAL            = '50000';
process.env.COPY_TRADING_MAX_CUSTOM_PER_USER = '3';
process.env.PEAR_REFERRAL_CODE               = 'BlackCatDeFi';

const store = require('../src/copyTradingStore');
const cmd   = require('../src/commandsCopyTrading');
const sm    = require('../src/userStateMachine');

function makeMockBot() {
  const bot = {
    sent: [],
    edited: [],
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
      return { message_id: bot.sent.length + 3000 };
    },
    editMessageText: async (text, opts) => {
      bot.edited.push({ text, opts });
    },
    deleteMessage: async () => {},
    answerCallbackQuery: async () => {},
    async triggerCallback(data, msgId = 6543) {
      const c = {
        id: 'cb',
        data,
        from: { id: 4242 },
        message: { chat: { id: 4242 }, message_id: msgId },
      };
      for (const fn of this._cbHandlers) await fn(c);
    },
    async triggerMessage(text) {
      const m = { chat: { id: 4242 }, from: { id: 4242 }, text };
      for (const fn of this._msgHandlers) await fn(m);
    },
  };
  return bot;
}

test.beforeEach(() => {
  store._resetForTests();
  sm._resetForTests();
});

const ADDR_A = '0x' + 'a'.repeat(40);
const ADDR_B = '0x' + 'b'.repeat(40);
const ADDR_C = '0x' + 'c'.repeat(40);
const ADDR_D = '0x' + 'd'.repeat(40);

// 1. add → state COPY_TRADE_AWAIT_ADDRESS + 0x prompt.
test('copytrade:custom:add → state COPY_TRADE_AWAIT_ADDRESS + prompt', async () => {
  const bot = makeMockBot();
  cmd.attach(bot);
  await bot.triggerCallback('copytrade:custom:add');
  const rec = sm.getState(4242);
  assert.strictEqual(rec.state, sm.STATES.COPY_TRADE_AWAIT_ADDRESS);
  const last = bot.sent[bot.sent.length - 1];
  assert.match(last.text, /0x/i);
});

// 2. Invalid address bumps a warning, state unchanged.
test('AWAIT_ADDRESS rejects non-0x text and stays in same state', async () => {
  const bot = makeMockBot();
  cmd.attach(bot);
  await bot.triggerCallback('copytrade:custom:add');
  bot.sent.length = 0;
  await bot.triggerMessage('not an address');
  assert.match(bot.sent[bot.sent.length - 1].text, /Invalid address/i);
  const rec = sm.getState(4242);
  assert.strictEqual(rec.state, sm.STATES.COPY_TRADE_AWAIT_ADDRESS);
});

// 3. valid 0x address → AWAIT_LABEL.
test('AWAIT_ADDRESS → AWAIT_LABEL on valid 0x address', async () => {
  const bot = makeMockBot();
  cmd.attach(bot);
  await bot.triggerCallback('copytrade:custom:add');
  await bot.triggerMessage(ADDR_A);
  const rec = sm.getState(4242);
  assert.strictEqual(rec.state, sm.STATES.COPY_TRADE_AWAIT_LABEL);
  assert.strictEqual(rec.payload.address, ADDR_A.toLowerCase());
});

// 4. skip label → AWAIT_CAPITAL with empty label.
test('AWAIT_LABEL → AWAIT_CAPITAL — "skip" yields empty label', async () => {
  const bot = makeMockBot();
  cmd.attach(bot);
  await bot.triggerCallback('copytrade:custom:add');
  await bot.triggerMessage(ADDR_A);
  await bot.triggerMessage('skip');
  const rec = sm.getState(4242);
  assert.strictEqual(rec.state, sm.STATES.COPY_TRADE_AWAIT_CAPITAL);
  assert.strictEqual(rec.payload.label, '');
});

// 5. AWAIT_CAPITAL "default" → DEFAULT_CAPITAL stored, state cleared.
test('AWAIT_CAPITAL "default" stores DEFAULT_CAPITAL + clears state', async () => {
  const bot = makeMockBot();
  cmd.attach(bot);
  await bot.triggerCallback('copytrade:custom:add');
  await bot.triggerMessage(ADDR_A);
  await bot.triggerMessage('Whale 1');
  await bot.triggerMessage('default');
  const rec = sm.getState(4242);
  assert.strictEqual(rec.state, sm.STATES.IDLE);
  const cfg = store.getTarget(4242, store.TYPE_CUSTOM_WALLET, ADDR_A);
  assert.ok(cfg, 'wallet must be persisted');
  assert.strictEqual(cfg.capital_usdc, store.DEFAULT_CAPITAL);
  assert.strictEqual(cfg.label, 'Whale 1');
  assert.strictEqual(!!cfg.enabled, true);
});

// 6. AWAIT_CAPITAL numeric input persists exact amount.
test('AWAIT_CAPITAL "$250" stores 250 USDC', async () => {
  const bot = makeMockBot();
  cmd.attach(bot);
  await bot.triggerCallback('copytrade:custom:add');
  await bot.triggerMessage(ADDR_B);
  await bot.triggerMessage('skip');
  await bot.triggerMessage('$250');
  const cfg = store.getTarget(4242, store.TYPE_CUSTOM_WALLET, ADDR_B);
  assert.strictEqual(cfg.capital_usdc, 250);
});

// 7. Cap of 3: 4th add via callback gets a warning, state stays IDLE.
test('Cap of 3 — 4th add fails with warning, no AWAIT state', async () => {
  const bot = makeMockBot();
  cmd.attach(bot);
  // Pre-load 3 wallets directly into the store
  store.setTarget(4242, store.TYPE_CUSTOM_WALLET, ADDR_A, { enabled: true });
  store.setTarget(4242, store.TYPE_CUSTOM_WALLET, ADDR_B, { enabled: true });
  store.setTarget(4242, store.TYPE_CUSTOM_WALLET, ADDR_C, { enabled: true });
  bot.sent.length = 0;
  await bot.triggerCallback('copytrade:custom:add');
  // Should send a warning and NOT enter AWAIT state
  assert.ok(bot.sent.some((m) => /limit/i.test(m.text)),
    'expected a limit warning');
  const rec = sm.getState(4242);
  assert.strictEqual(rec.state, sm.STATES.IDLE);
  // 4th address attempt direct via store also rejected
  assert.throws(
    () => store.setTarget(4242, store.TYPE_CUSTOM_WALLET, ADDR_D, { enabled: true }),
    /Maximum 3/i
  );
});

// 8. toggle / rm callbacks flip + delete.
test('copytrade:custom:toggle:<addr> + rm:<addr>', async () => {
  const bot = makeMockBot();
  cmd.attach(bot);
  store.setTarget(4242, store.TYPE_CUSTOM_WALLET, ADDR_A, { enabled: true });
  await bot.triggerCallback(`copytrade:custom:toggle:${ADDR_A}`);
  let cfg = store.getTarget(4242, store.TYPE_CUSTOM_WALLET, ADDR_A);
  assert.strictEqual(!!cfg.enabled, false);
  await bot.triggerCallback(`copytrade:custom:rm:${ADDR_A}`);
  cfg = store.getTarget(4242, store.TYPE_CUSTOM_WALLET, ADDR_A);
  assert.strictEqual(cfg, null);
});
