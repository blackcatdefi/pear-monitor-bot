'use strict';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');

const TMP_DIR = fs.mkdtempSync(path.join(os.tmpdir(), 'ct-test-'));
process.env.TRACK_DB_PATH = path.join(TMP_DIR, 'tracked_wallets.json');
process.env.USER_STATE_TIMEOUT_MIN = '5';

const sm = require('../src/userStateMachine');
const wt = require('../src/walletTracker');
const ct = require('../src/commandsTrack');

function makeFakeBot() {
  const messages = [];
  const callbacks = [];
  const onTextHandlers = [];
  const onMessageHandlers = [];
  const onCallbackHandlers = [];
  const bot = {
    onText(re, fn) {
      onTextHandlers.push({ re, fn });
    },
    on(evt, fn) {
      if (evt === 'callback_query') onCallbackHandlers.push(fn);
      else if (evt === 'message') onMessageHandlers.push(fn);
    },
    async sendMessage(chatId, text, opts) {
      messages.push({ chatId, text, opts });
      return { message_id: messages.length };
    },
    async answerCallbackQuery() {},
    _trigger: {
      text: async (msg) => {
        for (const { re, fn } of onTextHandlers) {
          const m = re.exec(msg.text);
          if (m) await fn(msg, m);
        }
      },
      message: async (msg) => {
        for (const fn of onMessageHandlers) await fn(msg);
      },
      callback: async (cb) => {
        for (const fn of onCallbackHandlers) await fn(cb);
      },
    },
    _state: { messages, callbacks },
  };
  return bot;
}

test.beforeEach(() => {
  wt._resetForTests();
  sm._resetForTests();
});

test('/track sends inline menu', async () => {
  const bot = makeFakeBot();
  ct.attach(bot);
  await bot._trigger.text({ chat: { id: 100 }, from: { id: 100 }, text: '/track' });
  const m = bot._state.messages.find((x) => x.text.includes('TRACK'));
  assert.ok(m);
  assert.ok(m.opts.reply_markup.inline_keyboard.length === 3);
});

test('callback track:add sets state AWAITING_WALLET_ADDRESS', async () => {
  const bot = makeFakeBot();
  ct.attach(bot);
  await bot._trigger.callback({
    id: 'cb1',
    data: 'track:add',
    from: { id: 200 },
    message: { chat: { id: 200 } },
  });
  const rec = sm.getState(200);
  assert.strictEqual(rec.state, sm.STATES.AWAITING_WALLET_ADDRESS);
});

test('full add flow: address → label → persisted', async () => {
  const bot = makeFakeBot();
  ct.attach(bot);
  const userId = 300;
  // simulate user tapping Add
  await bot._trigger.callback({
    id: 'cb2',
    data: 'track:add',
    from: { id: userId },
    message: { chat: { id: userId } },
  });
  // user types address
  const addr = '0x' + 'd'.repeat(40);
  await bot._trigger.message({
    chat: { id: userId },
    from: { id: userId },
    text: addr,
  });
  let rec = sm.getState(userId);
  assert.strictEqual(rec.state, sm.STATES.AWAITING_WALLET_LABEL);
  // user types label
  await bot._trigger.message({
    chat: { id: userId },
    from: { id: userId },
    text: 'Whale 7',
  });
  rec = sm.getState(userId);
  assert.strictEqual(rec.state, sm.STATES.IDLE);
  const ws = wt.getUserWallets(userId);
  assert.strictEqual(ws.length, 1);
  assert.strictEqual(ws[0].address, addr);
  assert.strictEqual(ws[0].label, 'Whale 7');
});

test('invalid address rejected, state preserved for retry', async () => {
  const bot = makeFakeBot();
  ct.attach(bot);
  const userId = 400;
  await bot._trigger.callback({
    id: 'cb3',
    data: 'track:add',
    from: { id: userId },
    message: { chat: { id: userId } },
  });
  await bot._trigger.message({
    chat: { id: userId },
    from: { id: userId },
    text: '0xINVALID',
  });
  const rec = sm.getState(userId);
  // still awaiting because it was rejected
  assert.strictEqual(rec.state, sm.STATES.AWAITING_WALLET_ADDRESS);
  const errMsg = bot._state.messages.find((m) => /no parece válida/i.test(m.text));
  assert.ok(errMsg);
});

test('/skip persists wallet without label', async () => {
  const bot = makeFakeBot();
  ct.attach(bot);
  const userId = 500;
  await bot._trigger.callback({
    id: 'cb4',
    data: 'track:add',
    from: { id: userId },
    message: { chat: { id: userId } },
  });
  const addr = '0x' + 'e'.repeat(40);
  await bot._trigger.message({
    chat: { id: userId },
    from: { id: userId },
    text: addr,
  });
  await bot._trigger.text({
    chat: { id: userId },
    from: { id: userId },
    text: '/skip',
  });
  const ws = wt.getUserWallets(userId);
  assert.strictEqual(ws.length, 1);
  assert.strictEqual(ws[0].label, null);
});

test('callback track:list shows wallets', async () => {
  const bot = makeFakeBot();
  ct.attach(bot);
  const userId = 600;
  const addr = '0x' + 'f'.repeat(40);
  wt.addWallet(userId, addr, 'WLT');
  await bot._trigger.callback({
    id: 'cb5',
    data: 'track:list',
    from: { id: userId },
    message: { chat: { id: userId } },
  });
  const m = bot._state.messages.find((x) => x.text.includes('WLT'));
  assert.ok(m);
});

test('/cancel resets in-flight conversation', async () => {
  const bot = makeFakeBot();
  ct.attach(bot);
  const userId = 700;
  await bot._trigger.callback({
    id: 'cb6',
    data: 'track:add',
    from: { id: userId },
    message: { chat: { id: userId } },
  });
  assert.strictEqual(
    sm.getState(userId).state,
    sm.STATES.AWAITING_WALLET_ADDRESS
  );
  await bot._trigger.text({
    chat: { id: userId },
    from: { id: userId },
    text: '/cancel',
  });
  assert.strictEqual(sm.getState(userId).state, sm.STATES.IDLE);
});
