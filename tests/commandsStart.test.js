'use strict';

/**
 * R-START — /start handler tests.
 *
 * Sandboxes both the onboarding store and the timezone store under unique
 * tmp dirs so tests don't pollute /app/data or each other.
 */

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');

const TMP_DIR = fs.mkdtempSync(path.join(os.tmpdir(), 'rstart-test-'));
process.env.ONBOARDING_DB_PATH = path.join(TMP_DIR, 'onboarding_users.json');
process.env.USER_TZ_DB_PATH = path.join(TMP_DIR, 'user_timezones.json');
process.env.TRACK_DB_PATH = path.join(TMP_DIR, 'tracked_wallets.json');
process.env.DEFAULT_TZ = 'UTC';
process.env.PEAR_HERO_URL =
  'https://app.pear.garden/?referral=BlackCatDeFi';
process.env.ONBOARDING_AUTO_TZ = 'true';

const onboarding = require('../src/onboarding');
const tzMgr = require('../src/timezoneManager');
const wt = require('../src/walletTracker');
const commandsStart = require('../src/commandsStart');

function mockBot() {
  const sent = [];
  const acks = [];
  return {
    sent,
    acks,
    sendMessage: async (chatId, text, opts) => {
      sent.push({ chatId, text, opts });
      return { message_id: sent.length };
    },
    answerCallbackQuery: async (id, opts) => {
      acks.push({ id, opts });
    },
  };
}

function mockMsg({ chatId = 1001, userId, lang = 'es-AR' } = {}) {
  return {
    chat: { id: chatId },
    from: {
      id: userId == null ? chatId : userId,
      language_code: lang,
    },
    text: '/start',
  };
}

test.beforeEach(() => {
  onboarding._resetForTests();
  tzMgr._resetForTests();
  wt._resetForTests();
});

test('/start first-time renders full onboarding (English)', async () => {
  const bot = mockBot();
  await commandsStart.handleStart(bot, mockMsg({ userId: 99999 }));
  assert.strictEqual(bot.sent.length, 1);
  const out = bot.sent[0];
  assert.match(out.text, /on-chain trading copilot/i);
  assert.match(out.text, /Track top-trader wallets/i);
  assert.match(out.text, /Set your timezone/i);
  // Inline keyboard: 4 rows (R-AUTOCOPY adds signals + copy_auto row).
  assert.ok(out.opts.reply_markup);
  assert.strictEqual(out.opts.reply_markup.inline_keyboard.length, 4);
});

test('/start recurring user renders compact dashboard (English)', async () => {
  const bot = mockBot();
  await commandsStart.handleStart(bot, mockMsg({ userId: 12345 }));
  await commandsStart.handleStart(bot, mockMsg({ userId: 12345 }));
  // Second message is the recurring text.
  const out = bot.sent[bot.sent.length - 1];
  assert.match(out.text, /Welcome back/i);
  assert.doesNotMatch(out.text, /on-chain trading copilot/i);
});

test('hero button URL contiene referral', () => {
  const kb = commandsStart.buildStartKeyboard(false);
  const heroBtn = kb.inline_keyboard
    .flat()
    .find((b) => b.text && b.text.includes('Pear'));
  assert.ok(heroBtn, 'hero button missing');
  assert.match(heroBtn.url, /referral=BlackCatDeFi/);
});

test('hero button label NO menciona referral en texto visible', () => {
  const kb = commandsStart.buildStartKeyboard(false);
  const heroBtn = kb.inline_keyboard
    .flat()
    .find((b) => b.text && b.text.includes('Pear'));
  assert.doesNotMatch(heroBtn.text, /referral/i);
  assert.doesNotMatch(heroBtn.text, /BlackCat/i);
});

test('/start dispara auto-TZ detection en first-time (es-AR)', async () => {
  const bot = mockBot();
  await commandsStart.handleStart(
    bot,
    mockMsg({ userId: 88888, lang: 'es-AR' })
  );
  assert.strictEqual(
    tzMgr.getUserTz(88888),
    'America/Argentina/Buenos_Aires'
  );
});

test('/start no clobbera TZ si user ya la tiene seteada', async () => {
  tzMgr.setUserTz(77777, 'America/New_York');
  const bot = mockBot();
  await commandsStart.handleStart(
    bot,
    mockMsg({ userId: 77777, lang: 'es-AR' })
  );
  // Even though es-AR maps to Buenos_Aires, we keep the manual override.
  assert.strictEqual(tzMgr.getUserTz(77777), 'America/New_York');
});

test('keyboard tiene 4 filas: track, signals+copy, tz+status, hero', () => {
  const kb = commandsStart.buildStartKeyboard();
  assert.strictEqual(kb.inline_keyboard.length, 4);
  // Row 1: 2 buttons (track add + track list)
  assert.strictEqual(kb.inline_keyboard[0].length, 2);
  // Row 2: 2 buttons (signals + copy_auto) — R-AUTOCOPY
  assert.strictEqual(kb.inline_keyboard[1].length, 2);
  // Row 3: 2 buttons (tz menu + status)
  assert.strictEqual(kb.inline_keyboard[2].length, 2);
  // Row 4: 1 hero button
  assert.strictEqual(kb.inline_keyboard[3].length, 1);
});

test('callback start:track_list with zero wallets prompts /track', async () => {
  const bot = mockBot();
  const cb = {
    id: 'cb1',
    data: 'start:track_list',
    from: { id: 5555 },
    message: { chat: { id: 5555 } },
  };
  const handled = await commandsStart._handleCallback(bot, cb);
  assert.strictEqual(handled, true);
  assert.match(bot.sent[0].text, /not tracking any wallet/i);
});

test('callback start:status_view renders synthetic dashboard', async () => {
  const bot = mockBot();
  const cb = {
    id: 'cb2',
    data: 'start:status_view',
    from: { id: 6666 },
    message: { chat: { id: 6666 } },
  };
  const handled = await commandsStart._handleCallback(bot, cb);
  assert.strictEqual(handled, true);
  assert.match(bot.sent[0].text, /Active alerts/i);
  assert.match(bot.sent[0].text, /Bot: active/i);
});

test('mute:<addr> callback removes the tracked wallet', async () => {
  const userId = 7777;
  const addr = '0x' + 'a'.repeat(40);
  wt.addWallet(userId, addr, 'Test Whale');
  assert.strictEqual(wt.getUserWallets(userId).length, 1);

  const bot = mockBot();
  const cb = {
    id: 'cb3',
    data: `mute:${addr.toLowerCase()}`,
    from: { id: userId },
    message: { chat: { id: userId } },
  };
  const handled = await commandsStart._handleCallback(bot, cb);
  assert.strictEqual(handled, true);
  assert.strictEqual(wt.getUserWallets(userId).length, 0);
  assert.match(bot.sent[0].text, /muted/i);
});

test('callback no-ours retorna false sin tocar nada', async () => {
  const bot = mockBot();
  const cb = {
    id: 'cb4',
    data: 'track:add',
    from: { id: 9999 },
    message: { chat: { id: 9999 } },
  };
  const handled = await commandsStart._handleCallback(bot, cb);
  assert.strictEqual(handled, false);
  assert.strictEqual(bot.sent.length, 0);
});

test('isFirstTime cambia a false después de markSeen', () => {
  assert.strictEqual(onboarding.isFirstTime(33333), true);
  onboarding.markSeen(33333);
  assert.strictEqual(onboarding.isFirstTime(33333), false);
});

test('markSeen incrementa contador starts', () => {
  onboarding.markSeen(44444);
  onboarding.markSeen(44444);
  const r = onboarding.getUserRecord(44444);
  assert.strictEqual(r.starts, 2);
});
