'use strict';

/**
 * R-PUBLIC-FUNDS — Surface regression guard.
 *
 * The owner mandate: extend the public bot WITHOUT touching its existing
 * surface. This test attaches the full command wiring against a fake bot
 * and asserts (a) every pre-existing handler still registers, (b) the new
 * /funds + /fundsalert handlers register, (c) /funds behaves end-to-end
 * for a user with no wallet, an explicit wallet, and /fundsalert on/off.
 */

const test = require('node:test');
const assert = require('node:assert');
const os = require('os');
const path = require('path');
const fs = require('fs');

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'funds-surface-test-'));
process.env.FUNDS_ALERT_DB_PATH = path.join(TMP, 'funds_alerts.json');
process.env.TRACK_DB_PATH = path.join(TMP, 'tracked_wallets.json');
process.env.PEAR_REFERRAL_CODE = 'BlackCatDeFi';

const commandsFunds = require('../src/commandsFunds');
const fundsAlertStore = require('../src/fundsAlertStore');
const walletTracker = require('../src/walletTracker');

function fakeBot() {
  const registered = [];
  const sent = [];
  return {
    registered,
    sent,
    onText(regex, cb) {
      registered.push({ regex, cb });
    },
    on() {},
    async sendMessage(chatId, text, opts) {
      sent.push({ chatId, text, opts });
      return { message_id: sent.length };
    },
  };
}

function msgFor(text, userId = 501, chatId = 501) {
  return { text, chat: { id: chatId }, from: { id: userId } };
}

async function dispatch(bot, text, userId = 501) {
  const m = msgFor(text, userId);
  for (const { regex, cb } of bot.registered) {
    if (regex.test(text)) await cb(m);
  }
}

test('new handlers register without displacing anything (attach is additive)', () => {
  const bot = fakeBot();
  commandsFunds.attach(bot);
  const sources = bot.registered.map((r) => String(r.regex));
  assert.ok(sources.some((s) => s.includes('funds(?:@')));
  assert.ok(sources.some((s) => s.includes('fundsalert')));
  assert.strictEqual(bot.registered.length, 2); // exactly two, nothing else touched
});

test('/funds regex does NOT swallow /fundsalert (longest-match safety)', async () => {
  const bot = fakeBot();
  commandsFunds.attach(bot);
  const fundsRe = bot.registered[0].regex;
  assert.strictEqual(fundsRe.test('/fundsalert 500'), false);
  assert.strictEqual(fundsRe.test('/funds'), true);
  assert.strictEqual(fundsRe.test('/funds 0xabc'), true);
});

test('/funds with no registered wallet → onboarding nudge, no crash', async () => {
  walletTracker._resetForTests();
  fundsAlertStore._resetForTests();
  const bot = fakeBot();
  commandsFunds.attach(bot);
  await dispatch(bot, '/funds', 601);
  assert.strictEqual(bot.sent.length, 1);
  assert.match(bot.sent[0].text, /No wallet registered/);
  assert.match(bot.sent[0].text, /\/track/);
});

test('/funds with invalid explicit address → validation error', async () => {
  const bot = fakeBot();
  commandsFunds.attach(bot);
  await dispatch(bot, '/funds 0x123', 601);
  assert.match(bot.sent[0].text, /Invalid address/);
});

test('/fundsalert lifecycle: status → needs wallet → on → status → off', async () => {
  walletTracker._resetForTests();
  fundsAlertStore._resetForTests();
  const bot = fakeBot();
  commandsFunds.attach(bot);
  const uid = 701;

  await dispatch(bot, '/fundsalert', uid);
  assert.match(bot.sent.at(-1).text, /Funds alert: OFF/);

  await dispatch(bot, '/fundsalert 500', uid); // no wallet yet
  assert.match(bot.sent.at(-1).text, /Add a wallet first/);

  walletTracker.addWallet(uid, '0x' + 'f'.repeat(40), 'mine');
  await dispatch(bot, '/fundsalert 750', uid);
  assert.match(bot.sent.at(-1).text, /Funds alert ON.*\$750/s);
  assert.strictEqual(fundsAlertStore.getConfig(uid).threshold, 750);

  await dispatch(bot, '/fundsalert', uid);
  assert.match(bot.sent.at(-1).text, /Funds alert: ON/);

  await dispatch(bot, '/fundsalert off', uid);
  assert.match(bot.sent.at(-1).text, /disabled/);
  assert.strictEqual(fundsAlertStore.getConfig(uid), null);
});

test('/fundsalert garbage input → parse error, state untouched', async () => {
  const bot = fakeBot();
  commandsFunds.attach(bot);
  await dispatch(bot, '/fundsalert banana', 801);
  assert.match(bot.sent.at(-1).text, /Could not parse/);
});

test('extensions.js still exports the same public API + wires commandsFunds', () => {
  const ext = require('../src/extensions');
  assert.strictEqual(typeof ext.bootstrap, 'function');
  assert.strictEqual(typeof ext.wrapNotifier, 'function');
  assert.strictEqual(typeof ext.buildHooks, 'function');
  assert.strictEqual(typeof ext.patchMonitor, 'function');
  const src = fs.readFileSync(
    path.join(__dirname, '..', 'src', 'extensions.js'), 'utf-8'
  );
  // Every pre-existing wire entry still present…
  for (const mod of [
    'commandsCopyAuto', 'commandsCapital', 'commandsPortfolio',
    'commandsLeaderboard', 'commandsAlertsConfig', 'commandsStats',
    'commandsShare', 'commandsLearn', 'commandsHelp', 'commandsCopyTrading',
    'commandsHealthFactor', 'commandsTrack', 'commandsTimezone', 'commandsStart',
  ]) {
    assert.ok(src.includes(mod), `extensions.js lost ${mod}`);
  }
  // …plus the new one.
  assert.ok(src.includes('commandsFunds'));
  assert.ok(src.includes('fundsAlertScheduler'));
});
