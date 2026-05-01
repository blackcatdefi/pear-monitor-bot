'use strict';

/**
 * R-SCRAPERROBUST (1 may 2026) — hardened HTTP fetcher for the t.me/s
 * Telegram channel scraper.
 *
 * Adds:
 *   • Exponential backoff retry (default 3 attempts, base 2s)
 *   • Realistic browser User-Agent (Telegram blocks empty/sparse UAs)
 *   • Configurable timeout via AbortController
 *   • Standard request headers so the response looks like a real browser hit
 *
 * Pure I/O — no parsing, no state. The scraper module composes
 * fetchWithRetry + parseHtml + dedup.
 */

const DEFAULT_USER_AGENT =
  process.env.SCRAPER_USER_AGENT ||
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36';

const DEFAULT_HEADERS = {
  'User-Agent': DEFAULT_USER_AGENT,
  Accept: 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
  'Accept-Language': 'en-US,en;q=0.9',
  'Cache-Control': 'no-cache',
};

const RETRY_MAX = parseInt(process.env.SCRAPER_RETRY_MAX || '3', 10);
const RETRY_BASE_MS = parseInt(process.env.SCRAPER_RETRY_BASE_MS || '2000', 10);
const TIMEOUT_MS = parseInt(process.env.SCRAPER_TIMEOUT_MS || '10000', 10);

function _sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Fetch with retry + exponential backoff. Returns the response body text.
 *
 * Throws after exhausting all retries. The thrown error preserves the last
 * underlying cause via err.cause so callers can introspect.
 *
 * @param {string} url
 * @param {object} [opts]
 *   maxRetries  — total attempts (default RETRY_MAX env-driven)
 *   baseDelayMs — base delay for exponential backoff (default RETRY_BASE_MS)
 *   timeoutMs   — per-attempt timeout (default TIMEOUT_MS)
 *   fetchImpl   — injected fetch fn (tests pass mock)
 *   sleepFn     — injected sleep fn (tests pass mock)
 *   logger      — { warn, error } (defaults to console)
 * @returns {Promise<string>} response body text
 */
async function fetchWithRetry(url, opts) {
  opts = opts || {};
  const maxRetries = Number.isFinite(opts.maxRetries) ? opts.maxRetries : RETRY_MAX;
  const baseDelayMs = Number.isFinite(opts.baseDelayMs)
    ? opts.baseDelayMs
    : RETRY_BASE_MS;
  const timeoutMs = Number.isFinite(opts.timeoutMs) ? opts.timeoutMs : TIMEOUT_MS;
  const fetchImpl = opts.fetchImpl || (typeof fetch === 'function' ? fetch : null);
  const sleepFn = opts.sleepFn || _sleep;
  const logger = opts.logger || console;

  if (!fetchImpl) {
    throw new Error('scraperFetch: global fetch unavailable; need Node 18+');
  }
  if (maxRetries < 1) {
    throw new Error('scraperFetch: maxRetries must be >= 1');
  }

  let lastError = null;
  for (let attempt = 0; attempt < maxRetries; attempt++) {
    let controller = null;
    let timer = null;
    try {
      // AbortController gives us a hard timeout per attempt regardless of
      // whether the underlying fetch impl honours its own timeout option.
      try {
        controller = typeof AbortController !== 'undefined'
          ? new AbortController()
          : null;
      } catch (_) {
        controller = null;
      }
      if (controller) {
        timer = setTimeout(() => controller.abort(), timeoutMs);
      }
      const res = await fetchImpl(url, {
        method: 'GET',
        headers: DEFAULT_HEADERS,
        signal: controller ? controller.signal : undefined,
      });
      if (timer) clearTimeout(timer);
      if (!res || typeof res.ok !== 'boolean') {
        throw new Error('scraperFetch: invalid response object');
      }
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      const text = await res.text();
      if (typeof text !== 'string') {
        throw new Error('scraperFetch: non-string body');
      }
      return text;
    } catch (err) {
      lastError = err;
      if (timer) clearTimeout(timer);
      const willRetry = attempt < maxRetries - 1;
      const delay = baseDelayMs * Math.pow(2, attempt); // 2s, 4s, 8s
      try {
        logger.warn(
          `[scraperFetch] attempt ${attempt + 1}/${maxRetries} failed: ${
            err && err.message ? err.message : err
          }${willRetry ? ` — retry in ${delay}ms` : ''}`
        );
      } catch (_) {}
      if (willRetry) {
        await sleepFn(delay);
      }
    }
  }
  const finalErr = new Error(
    `scraperFetch: failed after ${maxRetries} retries: ${
      lastError && lastError.message ? lastError.message : lastError
    }`
  );
  if (lastError) finalErr.cause = lastError;
  throw finalErr;
}

module.exports = {
  fetchWithRetry,
  DEFAULT_HEADERS,
  DEFAULT_USER_AGENT,
  RETRY_MAX,
  RETRY_BASE_MS,
  TIMEOUT_MS,
};
