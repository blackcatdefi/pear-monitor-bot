'use strict';

/**
 * R-AUTOCOPY-MENU — t.me/s/<channel> HTML scraper.
 *
 * Public Telegram channels expose a message preview at
 *   https://t.me/s/<username>
 * which is parseable HTML (no auth, no admin required). Each post lives
 * inside a `<div class="tgme_widget_message" data-post="<channel>/<id>" ...>`
 * and the embedded text is the message body. We extract:
 *
 *   - data-post="<channel>/<id>"   → message_id
 *   - data-time / time datetime    → posted_at (best effort)
 *   - href containing app.pear.garden → Pear basket URL
 *
 * Each new (unseen) message that has a Pear URL becomes a signal:
 *   { messageId, postedAt, pearUrl, tokens, side, raw }
 *
 * Why HTML and not Bot API channel_post? BCD can't (or won't) add the bot
 * as admin of the public channel, so the Bot API never delivers
 * channel_post updates. The HTML preview is always available.
 *
 * The actual fan-out happens in copyTrading.js — this module is purely
 * fetch + parse + dedup-via-store.
 */

const store = require('./copyTradingStore');
const { extractFromPearUrl } = require('./signalsChannelParser');
// R-SCRAPERROBUST (1 may 2026) — hardened HTTP fetcher with retry +
// realistic UA + AbortController timeout. Kept in its own module so the
// scraper is purely composition (fetchWithRetry + parseHtml + dedup).
const { fetchWithRetry, DEFAULT_HEADERS } = require('./scraperFetch');
const crypto = require('crypto');

const SIGNALS_SCRAPER_URL =
  process.env.SIGNALS_SCRAPER_URL ||
  `https://t.me/s/${store.BCD_SIGNALS_CHANNEL}`;
const SIGNALS_SCRAPER_INTERVAL_SEC = parseInt(
  process.env.SIGNALS_SCRAPER_INTERVAL_SEC || '30',
  10
);

// R-SCRAPERROBUST — owner-alert thresholds (consecutive failures).
// 3   → soft warn (console.error)
// 10  → hard alert via _onAlert callback if wired by extensions.js
const FAILURES_SOFT_WARN = parseInt(
  process.env.SCRAPER_FAILURES_SOFT_WARN || '3',
  10
);
const FAILURES_HARD_ALERT = parseInt(
  process.env.SCRAPER_FAILURES_HARD_ALERT || '10',
  10
);

let _consecutiveFailures = 0;
let _lastFetchAt = 0;
let _lastSuccessAt = 0;
let _hardAlertSentAt = 0;
let _timer = null;
let _onSignal = null;
let _onAlert = null;

function isEnabled() {
  const v = process.env.SIGNALS_SCRAPER_ENABLED;
  if (v === undefined) return true;
  return String(v).toLowerCase() !== 'false';
}

function getSchedule() {
  return {
    url: SIGNALS_SCRAPER_URL,
    intervalSec: SIGNALS_SCRAPER_INTERVAL_SEC,
    consecutiveFailures: _consecutiveFailures,
    lastFetchAt: _lastFetchAt,
    lastSuccessAt: _lastSuccessAt,
    hardAlertSentAt: _hardAlertSentAt,
  };
}

/**
 * Fetch the HTML preview. Pure I/O — no parsing.
 *
 * R-SCRAPERROBUST: delegates to scraperFetch.fetchWithRetry which provides
 * exponential backoff (3 attempts, 2s/4s/8s), realistic browser UA, and
 * AbortController-based timeout per attempt. Tests can inject a fake
 * fetch via opts.fetchImpl.
 */
async function fetchHtml(url, opts) {
  const target = url || SIGNALS_SCRAPER_URL;
  return fetchWithRetry(target, opts || {});
}

/**
 * Pure parser. Walks the HTML and extracts an array of posts. Used by
 * tests directly (no network needed).
 */
function parseHtml(html) {
  const out = [];
  if (typeof html !== 'string' || !html) return out;

  // Match each tgme_widget_message block. Lookahead terminates at the
  // next outer message div (identified by data-post attr — inner
  // tgme_widget_message_text divs don't have it) or end-of-string.
  const blockRx =
    /<div[^>]*class="[^"]*tgme_widget_message[^"]*"[^>]*data-post="([^"]+)"[\s\S]*?(?=<div[^>]*data-post="|$)/g;

  let m;
  while ((m = blockRx.exec(html)) !== null) {
    const dataPost = m[1];
    const block = m[0];
    const idMatch = dataPost.match(/^([^/]+)\/(\d+)$/);
    if (!idMatch) continue;
    const channel = idMatch[1];
    const messageId = parseInt(idMatch[2], 10);
    if (!Number.isFinite(messageId)) continue;

    let postedAt = null;
    const tMatch =
      block.match(/datetime="([^"]+)"/) || block.match(/data-time="([^"]+)"/);
    if (tMatch) {
      const dt = new Date(tMatch[1]);
      if (!isNaN(dt.getTime())) postedAt = Math.floor(dt.getTime() / 1000);
    }

    // Extract first Pear URL from the block — supports both quoted href
    // and bare text URLs (the t.me/s preview embeds links twice).
    const pearMatch =
      block.match(
        /href="(https?:\/\/app\.pear\.garden\/[^"]+)"/i
      ) ||
      block.match(/(https?:\/\/app\.pear\.garden\/[^\s"<]+)/i);
    const pearUrl = pearMatch ? pearMatch[1] : null;

    // Strip HTML tags from message_text block to get plain text fallback.
    let text = '';
    const textBlockMatch = block.match(
      /class="[^"]*tgme_widget_message_text[^"]*"[^>]*>([\s\S]*?)<\/div>/
    );
    if (textBlockMatch) {
      text = textBlockMatch[1]
        .replace(/<br\s*\/?>/gi, '\n')
        .replace(/<[^>]+>/g, '')
        .replace(/&amp;/g, '&')
        .replace(/&lt;/g, '<')
        .replace(/&gt;/g, '>')
        .replace(/&quot;/g, '"')
        .trim();
    }

    out.push({
      channel,
      messageId,
      postedAt,
      pearUrl,
      text,
    });
  }

  return out;
}

/**
 * Combine fetch + parse + force referral. Returns the raw post list.
 */
async function fetchAndParse(url) {
  const html = await fetchHtml(url);
  return parseHtml(html);
}

/**
 * Iterate detected posts and fire onSignal() for each previously-unseen
 * one that contains a Pear URL. Returns count of new signals dispatched.
 *
 *   onSignal(signal) — async callback.
 *   signal: { messageId, channel, postedAt, pearUrl, tokens, longTokens,
 *             shortTokens, signal_id, raw_text, dispatched_at }
 */
/**
 * R-SCRAPERROBUST — extra dedup layer keyed by sha256(canonicalPearUrl).
 *
 * Why: the message_id-based dedup in copyTradingStore catches the common
 * case (same post seen twice). But if BCD edits a post, t.me/s emits a NEW
 * message_id for the edit — we'd re-fire the same Pear basket as a fresh
 * signal. Hashing the canonicalised URL (referral stripped + tokens sorted)
 * makes the dedup robust against edits / reposts.
 */
function _canonicalUrlForHash(url) {
  if (!url || typeof url !== 'string') return '';
  // Strip query (referral, utm, anything) so an edited or reposted URL with
  // a different ref still hashes the same. We keep path because the path
  // encodes the basket (long/short tokens + collateral).
  try {
    const u = new URL(url);
    return `${u.origin}${u.pathname}`.toLowerCase().replace(/\/+$/, '');
  } catch (_) {
    return String(url).toLowerCase().split('?')[0].replace(/\/+$/, '');
  }
}

function _urlHash(url) {
  return crypto.createHash('sha256').update(_canonicalUrlForHash(url)).digest('hex');
}

async function processNewPosts(posts, onSignal) {
  let dispatched = 0;
  for (const p of posts) {
    if (!p || !p.messageId) continue;
    const channel = p.channel || store.BCD_SIGNALS_CHANNEL;
    if (store.hasSignalBeenSeen(channel, p.messageId)) continue;
    // Posts without a Pear URL: mark seen so we don't re-evaluate, then skip.
    if (!p.pearUrl) {
      store.markSignalSeen(channel, p.messageId, { skipped: true, no_url: true });
      continue;
    }

    const parsed = extractFromPearUrl(p.pearUrl);
    if (!parsed || parsed.tokens.length === 0) {
      // Not a basket URL — skip but mark seen so we don't reparse.
      store.markSignalSeen(channel, p.messageId, {
        skipped: true,
        pear_url: p.pearUrl,
      });
      continue;
    }
    // Force referral=BlackCatDeFi regardless of what came in the URL.
    const safeUrl = parsed.urlWithReferral;

    // R-SCRAPERROBUST — URL-hash dedup. The seen-store is keyed by
    // `${channel}/url-hash:${hash}` so we don't collide with the
    // message_id key. If we've already dispatched this canonical URL,
    // mark the new message_id as a duplicate-edit and skip dispatch.
    const urlHash = _urlHash(safeUrl);
    const urlHashKey = `url-hash:${urlHash}`;
    if (store.hasSignalBeenSeen(channel, urlHashKey)) {
      store.markSignalSeen(channel, p.messageId, {
        skipped: true,
        duplicate_of_url_hash: urlHash,
      });
      continue;
    }

    const signal = {
      messageId: p.messageId,
      channel,
      postedAt: p.postedAt,
      pearUrl: safeUrl,
      tokens: parsed.tokens,
      longTokens: parsed.longTokens,
      shortTokens: parsed.shortTokens,
      collateral: parsed.collateral,
      signal_id: String(p.messageId),
      raw_text: p.text || '',
      dispatched_at: Math.floor(Date.now() / 1000),
      url_hash: urlHash,
    };
    try {
      if (typeof onSignal === 'function') {
        await onSignal(signal);
      }
      store.markSignalSeen(channel, p.messageId, {
        pear_url: safeUrl,
        tokens: parsed.tokens,
        posted_at: p.postedAt || null,
        url_hash: urlHash,
      });
      // Index by url-hash too so future edits/reposts of the same basket
      // are recognised as duplicates regardless of message_id.
      store.markSignalSeen(channel, urlHashKey, {
        url_hash: urlHash,
        first_message_id: p.messageId,
        first_dispatched_at: signal.dispatched_at,
      });
      dispatched += 1;
    } catch (e) {
      console.error(
        '[signalsChannelScraper] onSignal failed for',
        p.messageId,
        e && e.message ? e.message : e
      );
    }
  }
  return dispatched;
}

async function pollOnce() {
  try {
    const posts = await fetchAndParse();
    const wasFailing = _consecutiveFailures > 0;
    _consecutiveFailures = 0;
    _hardAlertSentAt = 0; // reset so we re-alert on a future outage
    _lastFetchAt = Math.floor(Date.now() / 1000);
    _lastSuccessAt = _lastFetchAt;
    if (wasFailing && typeof _onAlert === 'function') {
      // R-SCRAPERROBUST — recovery notice (best-effort, swallow errors).
      try {
        await _onAlert({
          severity: 'recovery',
          consecutiveFailures: 0,
          lastFetchAt: _lastFetchAt,
          message: 'Scraper recovered (HTTP fetch succeeded after failures).',
        });
      } catch (_) {}
    }
    if (typeof _onSignal !== 'function') return 0;
    return processNewPosts(posts, _onSignal);
  } catch (e) {
    _consecutiveFailures += 1;
    _lastFetchAt = Math.floor(Date.now() / 1000);
    const errMsg = e && e.message ? e.message : String(e);
    if (_consecutiveFailures >= FAILURES_SOFT_WARN) {
      console.error(
        `[signalsChannelScraper] poll failed (#${_consecutiveFailures}/${FAILURES_HARD_ALERT}): ${errMsg}`
      );
    } else {
      console.warn(
        `[signalsChannelScraper] poll failed (#${_consecutiveFailures}): ${errMsg}`
      );
    }
    if (
      _consecutiveFailures >= FAILURES_HARD_ALERT &&
      typeof _onAlert === 'function' &&
      // Only fire one hard alert per outage window (reset on next success).
      _hardAlertSentAt === 0
    ) {
      _hardAlertSentAt = Math.floor(Date.now() / 1000);
      try {
        await _onAlert({
          severity: 'critical',
          consecutiveFailures: _consecutiveFailures,
          lastFetchAt: _lastFetchAt,
          lastSuccessAt: _lastSuccessAt,
          message:
            `Scraper has failed ${_consecutiveFailures} times in a row. ` +
            `Last error: ${errMsg}.`,
        });
      } catch (_) {}
    }
    return 0;
  }
}

function startSchedule(opts) {
  _onSignal = opts && typeof opts.onSignal === 'function' ? opts.onSignal : null;
  _onAlert = opts && typeof opts.onAlert === 'function' ? opts.onAlert : null;
  if (!isEnabled()) {
    console.log('[signalsChannelScraper] disabled via SIGNALS_SCRAPER_ENABLED=false');
    return null;
  }
  if (_timer) return _timer;
  // Fire an initial poll soon, then on the configured interval.
  setTimeout(() => {
    pollOnce().catch(() => {});
  }, 5_000);
  _timer = setInterval(() => {
    pollOnce().catch(() => {});
  }, Math.max(15, SIGNALS_SCRAPER_INTERVAL_SEC) * 1000);
  if (_timer && typeof _timer.unref === 'function') _timer.unref();
  console.log(
    `[signalsChannelScraper] schedule: ${SIGNALS_SCRAPER_INTERVAL_SEC}s @ ${SIGNALS_SCRAPER_URL}`
  );
  return _timer;
}

function stopSchedule() {
  if (_timer) {
    clearInterval(_timer);
    _timer = null;
  }
}

// R-SCRAPERROBUST — test hook. Resets the failure-tracking state so a
// fresh test scenario starts at zero failures with no pending hard-alert.
function _resetFailureStateForTests() {
  _consecutiveFailures = 0;
  _lastFetchAt = 0;
  _lastSuccessAt = 0;
  _hardAlertSentAt = 0;
  _onAlert = null;
  _onSignal = null;
}

/**
 * R-SCRAPERROBUST — test hook. Wires the onAlert/onSignal callbacks WITHOUT
 * starting the real interval timer (which would block process exit). Lets
 * tests drive pollOnce manually and observe the alert hook.
 */
function _wireCallbacksForTests({ onSignal, onAlert } = {}) {
  if (typeof onSignal === 'function') _onSignal = onSignal;
  if (typeof onAlert === 'function') _onAlert = onAlert;
}

module.exports = {
  isEnabled,
  getSchedule,
  fetchHtml,
  parseHtml,
  fetchAndParse,
  processNewPosts,
  pollOnce,
  startSchedule,
  stopSchedule,
  SIGNALS_SCRAPER_URL,
  SIGNALS_SCRAPER_INTERVAL_SEC,
  // R-SCRAPERROBUST exports
  FAILURES_SOFT_WARN,
  FAILURES_HARD_ALERT,
  _urlHash,
  _canonicalUrlForHash,
  _resetFailureStateForTests,
  _wireCallbacksForTests,
};
