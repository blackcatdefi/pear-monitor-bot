#!/usr/bin/env node
'use strict';

/**
 * R-GRAMJS — Telegram MTProto session generator.
 *
 * Usage (from any local machine where BCD can receive the Telegram login
 * code in the official Telegram app):
 *
 *   1. Visit https://my.telegram.org/apps with the Telegram account that
 *      owns @BlackCatDeFiSignals.
 *   2. Create an app called "PearProtocolAlertsBot" (or similar). Copy
 *      the api_id (integer) and api_hash (long hex string).
 *   3. Run:
 *        cd pear-monitor-bot
 *        npm install --no-save telegram input
 *        TELEGRAM_API_ID=<id> TELEGRAM_API_HASH=<hash> \
 *            node scripts/generate_telegram_session.js
 *   4. Enter the phone number, then the login code (delivered via the
 *      Telegram app, NOT SMS), and the 2FA password if enabled.
 *   5. Copy the printed SESSION_STRING into Railway env var
 *      TELEGRAM_SESSION_STRING on the gentle-luck service. Restart the
 *      service (or push any commit to master) — the bot will pick it up.
 *
 * Security:
 *   - The session string grants full account access. Never paste it into
 *     code, logs, chat, or commit it.
 *   - This script ONLY prints the string to stdout. Redirect to a file
 *     only if you intend to handle that file as a secret (chmod 600,
 *     remove after copying to Railway).
 *   - The script does NOT send the string anywhere — it's a local CLI.
 */

const apiIdRaw = process.env.TELEGRAM_API_ID;
const apiHash = process.env.TELEGRAM_API_HASH;

if (!apiIdRaw || !apiHash) {
  console.error('ERROR: TELEGRAM_API_ID and TELEGRAM_API_HASH must be set.');
  console.error('Get them at https://my.telegram.org/apps');
  process.exit(1);
}

const apiId = parseInt(apiIdRaw, 10);
if (!Number.isFinite(apiId) || apiId <= 0) {
  console.error('ERROR: TELEGRAM_API_ID must be a positive integer.');
  process.exit(1);
}

let TelegramClient;
let StringSession;
let input;
try {
  // eslint-disable-next-line global-require
  ({ TelegramClient } = require('telegram'));
  // eslint-disable-next-line global-require
  ({ StringSession } = require('telegram/sessions'));
  // eslint-disable-next-line global-require
  input = require('input');
} catch (e) {
  console.error('ERROR: missing dependency. Run:');
  console.error('  npm install --no-save telegram input');
  console.error('Then re-run this script.');
  console.error('Original error:', e && e.message ? e.message : e);
  process.exit(1);
}

(async () => {
  const stringSession = new StringSession('');
  const client = new TelegramClient(stringSession, apiId, apiHash, {
    connectionRetries: 5,
  });

  console.log('Connecting to Telegram MTProto…');
  console.log('You will be asked for: phone, login code, and 2FA password.');
  console.log('The code is sent through the Telegram app (NOT SMS).');
  console.log('');

  await client.start({
    phoneNumber: async () => await input.text('Phone number (e.g. +5491155555555): '),
    password: async () => await input.password('2FA password (blank if none): '),
    phoneCode: async () => await input.text('Login code from Telegram app: '),
    onError: (err) => {
      console.error('Auth error:', err && err.message ? err.message : err);
    },
  });

  const session = client.session.save();
  console.log('');
  console.log('═══════════════════════════════════════════════════════════════');
  console.log('SUCCESS — session string generated.');
  console.log('═══════════════════════════════════════════════════════════════');
  console.log('');
  console.log('Copy the line below and paste it into Railway env var:');
  console.log('  TELEGRAM_SESSION_STRING=<paste here>');
  console.log('');
  console.log(session);
  console.log('');
  console.log('After setting the env var on gentle-luck, the bot will use');
  console.log('the gramjs MTProto fallback automatically when the public');
  console.log('t.me/s scraper returns 10 consecutive failures.');
  console.log('');

  await client.disconnect();
  process.exit(0);
})().catch((e) => {
  console.error('Fatal:', e && e.message ? e.message : e);
  process.exit(1);
});
