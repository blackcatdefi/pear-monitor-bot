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
// R-GRAMJS (1 may 2026) — MTProto fallback. Lazy-required inside the
// scraper so unit tests that don't exercise the fallback don't pay the
// import cost. The module itself lazy-loads `telegram` (gramjs).
let _gramjsBackend = null;
function _getGramjsBackend() {
  if (_gramjsBackend) return _gramjsBackend;
  try {
    // eslint-disable-next-line global-require
    _gramjsBackend = require('./gramjsBackend');
  } catch (_) {
    _gramjsBackend = null;
  }
  return _gramjsBackend;
}
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

// R-GRAMJS — backend state machine. We default to 'scraper' (HTTP fetch
// from t.me/s/<channel>). After FAILURES_HARD_ALERT consecutive failures
// AND if the gramjs backend reports `isAvailable() === true`, we switch
// to 'gramjs'. While in 'gramjs' mode we still probe the scraper on every
// poll: after GRAMJS_PROBE_OK_THRESHOLD consecutive scraper-probe
// successes, we switch back. This keeps the cheaper / less-credentialed
// path as primary whenever it's healthy.
const GRAMJS_PROBE_OK_THRESHOLD = parseInt(
  process.env.GRAMJS_PROBE_OK_THRESHOLD || '3',
  10
);
let _backend = 'scraper';
let _scraperProbeOks = 0; // probe successes while in gramjs mode
let _backendSwitchedAt = 0;

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
    // R-GRAMJS — current backend visibility
    backend: _backend,
    scraperProbeOks: _scraperProbeOks,
    backendSwitchedAt: _backendSwitchedAt,
    gramjsAvailable: (() => {
      const g = _getGramjsBackend();
      try { return !!(g && g.isAvailable()); } catch (_) { return false; }
    })(),
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

/**
 * Internal — try the HTTP scraper once. Returns { ok: true, posts } on
 * success, or { ok: false, error } on failure. Pure: does NOT mutate
 * module-level failure counters.
 */
async function _tryScraperOnce() {
  try {
    const posts = await fetchAndParse();
    return { ok: true, posts };
  } catch (e) {
    return { ok: false, error: e };
  }
}

/**
 * Internal — try the gramjs MTProto backend once. Returns { ok, posts } or
 * { ok: false, error }. Returns { ok: false, error: 'unavailable' } if
 * the backend isn't configured (env missing or dep absent), so callers
 * can distinguish "not set up" from "set up but failed".
 */
async function _tryGramjsOnce() {
  const g = _getGramjsBackend();
  if (!g) return { ok: false, error: new Error('gramjs module not loadable') };
  if (!g.isAvailable()) return { ok: false, error: new Error('gramjs unavailable (env or dep)') };
  try {
    const posts = await g.fetchRecentMessages({ limit: 20 });
    return { ok: true, posts: posts || [] };
  } catch (e) {
    return { ok: false, error: e };
  }
}

/**
 * R-GRAMJS — switch backend with logging + reset of relevant counters.
 */
function _switchBackend(to, reason) {
  if (_backend === to) return;
  console.log(
    `[signalsChannelScraper] backend switch: ${_backend} → ${to} (${reason})`
  );
  _backend = to;
  _backendSwitchedAt = Math.floor(Date.now() / 1000);
  if (to === 'gramjs') {
    _scraperProbeOks = 0;
  } else {
    _consecutiveFailures = 0;
    _hardAlertSentAt = 0;
  }
}

async function pollOnce() {
  // R-GRAMJS — branch by backend. The wire-shape on success is the same
  // (a post array consumed by processNewPosts), so the success path below
  // is shared.
  let posts = null;
  let primaryOk = false;
  let primaryErr = null;

  if (_backend === 'scraper') {
    const r = await _tryScraperOnce();
    if (r.ok) {
      posts = r.posts;
      primaryOk = true;
    } else {
      primaryErr = r.error;
    }
  } else {
    // In gramjs mode, ALWAYS probe the scraper first (it's free / no API
    // credits). If the probe returns successfully GRAMJS_PROBE_OK_THRESHOLD
    // times in a row, switch back to scraper-primary.
    const probe = await _tryScraperOnce();
    if (probe.ok) {
      _scraperProbeOks += 1;
      console.log(
        `[signalsChannelScraper] scraper probe ok (#${_scraperProbeOks}/${GRAMJS_PROBE_OK_THRESHOLD}) while gramjs primary`
      );
      if (_scraperProbeOks >= GRAMJS_PROBE_OK_THRESHOLD) {
        _switchBackend('scraper', `probe success x${_scraperProbeOks}`);
        posts = probe.posts;
        primaryOk = true;
      } else {
        // Stay on gramjs for now — use its data for this tick (probe
        // result is discarded so we don't double-fire; gramjs is the
        // authoritative source until we've fully recovered).
        const g = await _tryGramjsOnce();
        if (g.ok) {
          posts = g.posts;
          primaryOk = true;
        } else {
          primaryErr = g.error;
        }
      }
    } else {
      _scraperProbeOks = 0;
      const g = await _tryGramjsOnce();
      if (g.ok) {
        posts = g.posts;
        primaryOk = true;
      } else {
        primaryErr = g.error;
      }
    }
  }

  if (primaryOk) {
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
          backend: _backend,
          message: `Signals pipeline recovered via ${_backend} backend.`,
        });
      } catch (_) {}
    }
    if (typeof _onSignal !== 'function') return 0;
    return processNewPosts(posts || [], _onSignal);
  }

  // Failure path. Increment counter, log, possibly hard-alert and
  // possibly switch backend.
  _consecutiveFailures += 1;
  _lastFetchAt = Math.floor(Date.now() / 1000);
  const errMsg =
    primaryErr && primaryErr.message ? primaryErr.message : String(primaryErr);
  if (_consecutiveFailures >= FAILURES_SOFT_WARN) {
    console.error(
      `[signalsChannelScraper] poll failed via ${_backend} (#${_consecutiveFailures}/${FAILURES_HARD_ALERT}): ${errMsg}`
    );
  } else {
    console.warn(
      `[signalsChannelScraper] poll failed via ${_backend} (#${_consecutiveFailures}): ${errMsg}`
    );
  }

  // R-GRAMJS — auto-switch from scraper to gramjs on hard failure
  // threshold, IF gramjs is available. We only attempt the switch from
  // 'scraper' state; the recovery path back is handled in the success
  // branch above.
  if (
    _backend === 'scraper' &&
    _consecutiveFailures >= FAILURES_HARD_ALERT
  ) {
    const g = _getGramjsBackend();
    if (g && (() => { try { return g.isAvailable(); } catch (_) { return false; } })()) {
      _switchBackend(
        'gramjs',
        `${_consecutiveFailures} consecutive scraper failures`
      );
    }
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
        backend: _backend,
        message:
          `Signals pipeline has failed ${_consecutiveFailures} times in a row ` +
          `(active backend: ${_backend}). Last error: ${errMsg}.`,
      });
    } catch (_) {}
  }
  return 0;
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
  // R-GRAMJS — also reset backend state machine.
  _backend = 'scraper';
  _scraperProbeOks = 0;
  _backendSwitchedAt = 0;
  _gramjsBackend = null;
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

// R-GRAMJS — test hook. Lets tests inject a mock gramjs backend to drive
// the state machine without loading the real `telegram` package.
function _injectGramjsBackendForTests(mock) {
  _gramjsBackend = mock;
}

// R-GRAMJS — test hook. Force backend state for deterministic switching
// scenarios.
function _setBackendForTests(b) {
  if (b === 'scraper' || b === 'gramjs') _backend = b;
  _scraperProbeOks = 0;
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
  // R-GRAMJS exports
  GRAMJS_PROBE_OK_THRESHOLD,
  _injectGramjsBackendForTests,
  _setBackendForTests,
};
