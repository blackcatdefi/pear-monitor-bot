'use strict';

/**
 * R-GRAMJS (1 may 2026) — MTProto fallback backend for the public-channel
 * signals pipeline.
 *
 * Why: the primary fetch path (`signalsChannelScraper.js`) hits Telegram's
 * public preview at `https://t.me/s/<channel>`. If Telegram ever rate-limits
 * or blocks that endpoint from our IP, the bot would go silent. This module
 * connects directly to the Telegram MTProto API via the user-account
 * gramjs client and reads messages from `@BlackCatDeFiSignals` as the same
 * BCD account that owns the channel.
 *
 * Design:
 *   - Lazy-loaded: `telegram` (gramjs) and `input` are only `require()`d
 *     inside `_loadGramjs()`, on the first call to `init()`. Tests that
 *     don't exercise this backend never pay the import cost (and don't
 *     need the deps installed in CI).
 *   - Stateless API: callers ask `isAvailable()` first; if true, they call
 *     `fetchRecentMessages()`. The module manages connect/disconnect.
 *   - Returns the SAME post shape as `signalsChannelScraper.parseHtml()`
 *     so callers can reuse `processNewPosts()` without modification.
 *
 * Env:
 *   - TELEGRAM_API_ID         — int, from https://my.telegram.org/apps
 *   - TELEGRAM_API_HASH       — string, from same page
 *   - TELEGRAM_SESSION_STRING — output of scripts/generate_telegram_session.js
 *   - BCD_SIGNALS_CHANNEL     — channel username (e.g. "BlackCatDeFiSignals")
 *
 * Security:
 *   - Session string is read at runtime only; never logged.
 *   - If any env var is empty / PENDING, `isAvailable()` returns false and
 *     the caller keeps using the scraper.
 */

const PEAR_URL_RX = /(https?:\/\/app\.pear\.garden\/[^\s"<]+)/i;

let _client = null;
let _connected = false;
let _initError = null;
let _gramjs = null; // { TelegramClient, StringSession }

/**
 * Lazy-require gramjs. Throws if the package isn't installed — caller
 * should treat that as "fallback unavailable" rather than fatal.
 */
function _loadGramjs() {
  if (_gramjs) return _gramjs;
  // Wrapped in try so callers get a clean "unavailable" signal when the
  // dep isn't installed (e.g. in unit-test sandbox).
  // eslint-disable-next-line global-require
  const telegram = require('telegram');
  // eslint-disable-next-line global-require
  const sessions = require('telegram/sessions');
  _gramjs = {
    TelegramClient: telegram.TelegramClient,
    StringSession: sessions.StringSession,
  };
  return _gramjs;
}

function _readEnv() {
  const apiIdRaw = process.env.TELEGRAM_API_ID || '';
  const apiHash = process.env.TELEGRAM_API_HASH || '';
  const sessionString = process.env.TELEGRAM_SESSION_STRING || '';
  const channel = process.env.BCD_SIGNALS_CHANNEL || 'BlackCatDeFiSignals';

  const apiId = parseInt(apiIdRaw, 10);
  const ready =
    Number.isFinite(apiId) &&
    apiId > 0 &&
    typeof apiHash === 'string' &&
    apiHash.length > 0 &&
    typeof sessionString === 'string' &&
    sessionString.length > 0 &&
    sessionString !== 'PENDING_BCD_SETUP';

  return { apiId, apiHash, sessionString, channel, ready };
}

/**
 * Returns true iff env is fully populated AND gramjs is loadable. Does NOT
 * connect — connection is deferred to fetchRecentMessages() so that probe
 * calls in scraper-mode don't open a socket.
 */
function isAvailable() {
  const env = _readEnv();
  if (!env.ready) return false;
  try {
    _loadGramjs();
    return true;
  } catch (_) {
    return false;
  }
}

/**
 * Reason string for /x_status-style debugging. Returns an array of human-
 * readable lines describing why the backend is or isn't available.
 */
function statusLines() {
  const env = _readEnv();
  const lines = [];
  lines.push(`channel: @${env.channel}`);
  lines.push(`api_id: ${env.apiId > 0 ? 'set' : 'missing'}`);
  lines.push(`api_hash: ${env.apiHash ? 'set' : 'missing'}`);
  lines.push(
    `session: ${
      env.sessionString === ''
        ? 'missing'
        : env.sessionString === 'PENDING_BCD_SETUP'
        ? 'pending (run scripts/generate_telegram_session.js)'
        : 'set'
    }`
  );
  let depOk = true;
  try { _loadGramjs(); } catch (_) { depOk = false; }
  lines.push(`gramjs dep: ${depOk ? 'installed' : 'missing'}`);
  lines.push(`available: ${env.ready && depOk ? 'YES' : 'NO'}`);
  return lines;
}

/**
 * Connect (or return cached client). Returns null if unavailable.
 */
async function _ensureConnected() {
  if (_initError) return null;
  if (_client && _connected) return _client;

  const env = _readEnv();
  if (!env.ready) return null;

  let gramjs;
  try {
    gramjs = _loadGramjs();
  } catch (e) {
    _initError = `gramjs not installed: ${e && e.message ? e.message : e}`;
    console.error(`[gramjsBackend] ${_initError}`);
    return null;
  }

  try {
    const session = new gramjs.StringSession(env.sessionString);
    _client = new gramjs.TelegramClient(session, env.apiId, env.apiHash, {
      connectionRetries: 3,
      // Keep gramjs's own logger quiet so MTProto chatter doesn't pollute
      // Railway logs. We surface our own structured messages.
      baseLogger: { ...console, log: () => {}, info: () => {} },
    });
    await _client.connect();
    _connected = true;
    console.log('[gramjsBackend] connected to Telegram MTProto');
    return _client;
  } catch (e) {
    const msg = e && e.message ? e.message : String(e);
    _initError = `connect failed: ${msg}`;
    console.error(`[gramjsBackend] ${_initError}`);
    _client = null;
    _connected = false;
    return null;
  }
}

/**
 * Convert a gramjs Message object into the same post shape that
 * `signalsChannelScraper.parseHtml()` produces. This lets the scraper's
 * `processNewPosts()` consume gramjs results unchanged.
 */
function _normalizeMessage(channel, msg) {
  if (!msg || typeof msg !== 'object') return null;
  // msg.id is the message_id; msg.date is a unix timestamp (seconds) on
  // gramjs, or a Date instance — handle both.
  const messageId =
    typeof msg.id === 'number' ? msg.id : parseInt(msg.id, 10);
  if (!Number.isFinite(messageId)) return null;

  let postedAt = null;
  if (typeof msg.date === 'number') postedAt = msg.date;
  else if (msg.date instanceof Date)
    postedAt = Math.floor(msg.date.getTime() / 1000);

  // gramjs exposes message text on either `.message` (string) or `.text`.
  const text =
    (typeof msg.message === 'string' && msg.message) ||
    (typeof msg.text === 'string' && msg.text) ||
    '';

  // Look for Pear URL in text first, then in entities (typed URL entities).
  let pearUrl = null;
  const m = text.match(PEAR_URL_RX);
  if (m) pearUrl = m[1];

  if (!pearUrl && Array.isArray(msg.entities)) {
    for (const e of msg.entities) {
      // MessageEntityTextUrl carries the URL on .url; MessageEntityUrl has
      // it inline in the text we already scanned.
      if (e && typeof e.url === 'string' && PEAR_URL_RX.test(e.url)) {
        pearUrl = e.url.match(PEAR_URL_RX)[1];
        break;
      }
    }
  }

  return {
    channel,
    messageId,
    postedAt,
    pearUrl,
    text,
  };
}

/**
 * Fetch the most recent N messages from the configured channel and return
 * them as parser-shape posts. `limit` defaults to 20 to match the typical
 * t.me/s preview window.
 *
 * Returns [] (and logs) on any error — callers treat that as a probe miss
 * rather than a hard failure.
 */
async function fetchRecentMessages(opts) {
  const limit = (opts && opts.limit) || 20;
  const env = _readEnv();
  const channel = (opts && opts.channel) || env.channel;

  const client = await _ensureConnected();
  if (!client) return [];

  try {
    const messages = await client.getMessages(`@${channel}`, { limit });
    if (!Array.isArray(messages)) return [];
    const posts = [];
    for (const m of messages) {
      const p = _normalizeMessage(channel, m);
      if (p) posts.push(p);
    }
    // gramjs returns newest first; the scraper expects newest first too,
    // so no reorder needed.
    return posts;
  } catch (e) {
    const msg = e && e.message ? e.message : String(e);
    console.error(`[gramjsBackend] fetchRecentMessages failed: ${msg}`);
    // On certain errors, force a reconnect on next call.
    if (/AUTH_KEY|FLOOD|CONNECTION/i.test(msg)) {
      try { await disconnect(); } catch (_) {}
    }
    return [];
  }
}

async function disconnect() {
  if (_client && _connected) {
    try { await _client.disconnect(); } catch (_) {}
  }
  _client = null;
  _connected = false;
  _initError = null;
}

// Test hook — reset module-level state so unit tests get a clean slate.
function _resetForTests() {
  _client = null;
  _connected = false;
  _initError = null;
  _gramjs = null;
}

module.exports = {
  isAvailable,
  statusLines,
  fetchRecentMessages,
  disconnect,
  // Exposed for tests / debug only:
  _normalizeMessage,
  _readEnv,
  _resetForTests,
};
