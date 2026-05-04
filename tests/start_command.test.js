'use strict';

/**
 * R-PUBLIC-START-FIX — regression test for /start handler wiring.
 *
 * Guards against:
 *   1. /start onText regex not matching the command (bot silently ignores it).
 *   2. attach() throwing during bootstrap (handler never registered).
 *   3. handleStart() crashing before sendMessage is reached.
 *   4. Null-message callback_query crashing the process (unhandled rejection).
 *
 * Deliberately does NOT require bot.js or index.js (those involve real
 * TelegramBot() + polling). Tests commandsStart in isolation with a mock bot
 * whose onText/on stores registered handlers so we can call them directly.
 */

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');

// ── sandbox all persistent stores ────────────────────────────────────────────
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'rstart-fix-'));
process.env.ONBOARDING_DB_PATH   = path.join(TMP, 'onboarding.json');
process.env.USER_TZ_DB_PATH      = path.join(TMP, 'timezones.json');
process.env.TRACK_DB_PATH        = path.join(TMP, 'wallets.json');
process.env.STATS_DB_PATH        = path.join(TMP, 'stats.json');
process.env.SHARE_DB_PATH        = path.join(TMP, 'share.json');
process.env.PEAR_HERO_URL        = 'https://app.pear.garden/?referral=BlackCatDeFi';
process.env.DEFAULT_TZ           = 'UTC';
process.env.ONBOARDING_AUTO_TZ   = 'false'; // keep tests deterministic

const onboarding = require('../src/onboarding');
const commandsStart = require('../src/commandsStart');

// ── minimal mock bot ─────────────────────────────────────────────────────────
function makeMockBot() {
  const _textHandlers = [];   // { regex, fn }
  const _cbHandlers   = [];   // fn

  return {
    _textHandlers,
    _cbHandlers,
    sent: [],

    onText(regex, fn)  { _textHandlers.push({ regex, fn }); },
    on(event, fn)      { if (event === 'callback_query') _cbHandlers.push(fn); },

    sendMessage: async (chatId, text, opts) => {
      /* captured by reference on the instance — see usage below */
    },
    answerCallbackQuery: async () => {},

    // helper: simulate a message arriving
    async trigger(text) {
      const msg = { chat: { id: 9001 }, from: { id: 9001, language_code: 'en' }, text };
      for (const { regex, fn } of _textHandlers) {
        if (regex.test(text)) await fn(msg);
      }
    },

    // helper: simulate a callback_query arriving
    async triggerCallback(data, message = { chat: { id: 9001 } }) {
      const cb = { id: 'cbq1', data, from: { id: 9001 }, message };
      for (const fn of _cbHandlers) await fn(cb);
    },
  };
}

// ── reset stores between tests ────────────────────────────────────────────────
test.beforeEach(() => {
  onboarding._resetForTests();
});

// ─────────────────────────────────────────────────────────────────────────────
// 1. attach() registers an onText handler for /start
// ─────────────────────────────────────────────────────────────────────────────
test('attach() registers onText handler for /start', () => {
  const bot = makeMockBot();
  commandsStart.attach(bot);
  const hasStart = bot._textHandlers.some(({ regex }) => regex.test('/start'));
  assert.ok(hasStart, 'no onText handler matched /start after attach()');
});

// ─────────────────────────────────────────────────────────────────────────────
// 2. /start regex matches bare command and deep-link payloads
// ─────────────────────────────────────────────────────────────────────────────
test('/start regex matches bare /start', () => {
  const bot = makeMockBot();
  commandsStart.attach(bot);
  const r = bot._textHandlers.find(({ regex }) => regex.test('/start'));
  assert.ok(r, 'handler missing');
  assert.ok(r.regex.test('/start'));
});

test('/start regex matches /start@BotUsername', () => {
  const bot = makeMockBot();
  commandsStart.attach(bot);
  const r = bot._textHandlers.find(({ regex }) => regex.test('/start'));
  assert.ok(r.regex.test('/start@PearProtocolAlertsBot'));
});

test('/start regex matches /start ref_12345 deep-link payload', () => {
  const bot = makeMockBot();
  commandsStart.attach(bot);
  const r = bot._textHandlers.find(({ regex }) => regex.test('/start'));
  assert.ok(r.regex.test('/start ref_12345'));
});

test('/start regex does NOT match /started or /startover', () => {
  const bot = makeMockBot();
  commandsStart.attach(bot);
  const r = bot._textHandlers.find(({ regex }) => regex.test('/start'));
  assert.ok(!r.regex.test('/started'));
  assert.ok(!r.regex.test('/startover'));
});

// ─────────────────────────────────────────────────────────────────────────────
// 3. Triggering /start actually sends a message (end-to-end handler call)
// ─────────────────────────────────────────────────────────────────────────────
test('/start trigger sends a Telegram message', async () => {
  const bot = makeMockBot();
  const sent = [];
  bot.sendMessage = async (chatId, text, opts) => sent.push({ chatId, text, opts });

  commandsStart.attach(bot);
  await bot.trigger('/start');

  assert.strictEqual(sent.length, 1, 'expected exactly one sendMessage call');
  assert.match(sent[0].text, /Pear Protocol/i);
});

test('/start sends inline keyboard', async () => {
  const bot = makeMockBot();
  const sent = [];
  bot.sendMessage = async (chatId, text, opts) => sent.push({ chatId, text, opts });

  commandsStart.attach(bot);
  await bot.trigger('/start');

  const kb = sent[0].opts && sent[0].opts.reply_markup;
  assert.ok(kb && kb.inline_keyboard, 'inline_keyboard missing');
  assert.ok(kb.inline_keyboard.length >= 4, 'expected ≥4 keyboard rows');
});

// ─────────────────────────────────────────────────────────────────────────────
// 4. Null message in callback_query must NOT throw (regression for Node 20 crash)
// ─────────────────────────────────────────────────────────────────────────────
test('callback_query with null message does not throw', async () => {
  const bot = makeMockBot();
  commandsStart.attach(bot);

  // commandsStart's callback_query handler should early-return on non-start: data
  await assert.doesNotReject(
    () => bot.triggerCallback('start:status_view', null),
    'callback_query with null message must not throw'
  );
});

// ─────────────────────────────────────────────────────────────────────────────
// 5. attach() is idempotent — calling it twice doesn't break /start
// ─────────────────────────────────────────────────────────────────────────────
test('attach() called twice — /start still works', async () => {
  const bot = makeMockBot();
  const sent = [];
  bot.sendMessage = async (chatId, text, opts) => sent.push({ chatId, text, opts });

  commandsStart.attach(bot);
  commandsStart.attach(bot);

  await bot.trigger('/start');

  // Both handlers fire, so we get 2 messages — what matters is NO crash
  assert.ok(sent.length >= 1, 'expected at least one sendMessage call');
  assert.match(sent[0].text, /Pear Protocol/i);
});

// ─────────────────────────────────────────────────────────────────────────────
// 6. Recurring user gets "Welcome back" not onboarding text
// ─────────────────────────────────────────────────────────────────────────────
test('second /start shows "Welcome back" (recurring path)', async () => {
  const bot = makeMockBot();
  const sent = [];
  bot.sendMessage = async (chatId, text, opts) => sent.push({ chatId, text, opts });

  commandsStart.attach(bot);
  await bot.trigger('/start'); // first time
  await bot.trigger('/start'); // recurring

  const last = sent[sent.length - 1];
  assert.match(last.text, /Welcome back/i);
  assert.doesNotMatch(last.text, /on-chain trading copilot/i);
});
