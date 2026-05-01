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

const SIGNALS_SCRAPER_URL =
  process.env.SIGNALS_SCRAPER_URL ||
  `https://t.me/s/${store.BCD_SIGNALS_CHANNEL}`;
const SIGNALS_SCRAPER_INTERVAL_SEC = parseInt(
  process.env.SIGNALS_SCRAPER_INTERVAL_SEC || '30',
  10
);
const USER_AGENT =
  process.env.SCRAPER_USER_AGENT ||
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15';

let _consecutiveFailures = 0;
let _lastFetchAt = 0;
let _timer = null;
let _onSignal = null;

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
  };
}

/**
 * Fetch the HTML preview. Pure I/O — no parsing.
 */
async function fetchHtml(url) {
  const target = url || SIGNALS_SCRAPER_URL;
  const fetchFn = typeof fetch === 'function' ? fetch : null;
  if (!fetchFn) throw new Error('global fetch unavailable; need Node 18+');
  const res = await fetchFn(target, {
    method: 'GET',
    headers: {
      'User-Agent': USER_AGENT,
      Accept:
        'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    },
  });
  if (!res.ok) {
    throw new Error(`scraper status ${res.status}`);
  }
  return res.text();
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
    };
    try {
      if (typeof onSignal === 'function') {
        await onSignal(signal);
      }
      store.markSignalSeen(channel, p.messageId, {
        pear_url: safeUrl,
        tokens: parsed.tokens,
        posted_at: p.postedAt || null,
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
    _consecutiveFailures = 0;
    _lastFetchAt = Math.floor(Date.now() / 1000);
    if (typeof _onSignal !== 'function') return 0;
    return processNewPosts(posts, _onSignal);
  } catch (e) {
    _consecutiveFailures += 1;
    console.error(
      `[signalsChannelScraper] poll failed (#${_consecutiveFailures}):`,
      e && e.message ? e.message : e
    );
    return 0;
  }
}

function startSchedule(opts) {
  _onSignal = opts && typeof opts.onSignal === 'function' ? opts.onSignal : null;
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
};
