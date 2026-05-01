'use strict';

/**
 * R-SCRAPERROBUST (1 may 2026) — tests for fetchWithRetry + URL-hash dedup
 * + owner-alert hook in the t.me/s scraper pipeline.
 *
 * No live network — everything goes through injected fake fetch impls.
 */

// ENV must be set BEFORE requiring scraperFetch — its module-level
// constants (RETRY_MAX, RETRY_BASE_MS, TIMEOUT_MS) are baked at import.
process.env.SCRAPER_RETRY_MAX = '1';
process.env.SCRAPER_RETRY_BASE_MS = '1';
process.env.SCRAPER_TIMEOUT_MS = '500';
process.env.SCRAPER_FAILURES_HARD_ALERT = '3';
process.env.SCRAPER_FAILURES_SOFT_WARN = '2';
process.env.SIGNALS_SCRAPER_ENABLED = 'true';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'scraper-robust-'));
process.env.COPY_TRADING_DB_DIR = TMP;

// Fresh module instances.
delete require.cache[require.resolve('../src/copyTradingStore')];
delete require.cache[require.resolve('../src/signalsChannelScraper')];
delete require.cache[require.resolve('../src/scraperFetch')];

const fetcher = require('../src/scraperFetch');
const store = require('../src/copyTradingStore');
const scraper = require('../src/signalsChannelScraper');

// Helper: a fake response object that mimics the WHATWG fetch shape.
function fakeResponse({ ok = true, status = 200, body = '' }) {
  return {
    ok,
    status,
    text: async () => body,
  };
}

// ─── fetchWithRetry ──────────────────────────────────────────────────────

test('fetchWithRetry — succeeds first attempt, no retry', async () => {
  let calls = 0;
  const fakeFetch = async () => {
    calls += 1;
    return fakeResponse({ body: 'OK' });
  };
  const text = await fetcher.fetchWithRetry('http://x', {
    fetchImpl: fakeFetch,
    sleepFn: async () => {},
    logger: { warn: () => {}, error: () => {} },
  });
  assert.strictEqual(text, 'OK');
  assert.strictEqual(calls, 1);
});

test('fetchWithRetry — retries on transient error then succeeds', async () => {
  let calls = 0;
  const sleeps = [];
  const fakeFetch = async () => {
    calls += 1;
    if (calls < 3) throw new Error('boom');
    return fakeResponse({ body: 'recovered' });
  };
  const text = await fetcher.fetchWithRetry('http://x', {
    maxRetries: 3,
    baseDelayMs: 10,
    fetchImpl: fakeFetch,
    sleepFn: async (ms) => { sleeps.push(ms); },
    logger: { warn: () => {}, error: () => {} },
  });
  assert.strictEqual(text, 'recovered');
  assert.strictEqual(calls, 3);
  // Exponential backoff: 10ms (after attempt 1), 20ms (after attempt 2)
  assert.deepStrictEqual(sleeps, [10, 20]);
});

test('fetchWithRetry — exhausts retries and throws with err.cause', async () => {
  let calls = 0;
  const fakeFetch = async () => {
    calls += 1;
    throw new Error(`fail ${calls}`);
  };
  await assert.rejects(
    fetcher.fetchWithRetry('http://x', {
      maxRetries: 3,
      baseDelayMs: 1,
      fetchImpl: fakeFetch,
      sleepFn: async () => {},
      logger: { warn: () => {}, error: () => {} },
    }),
    (err) => {
      assert.match(err.message, /failed after 3 retries/);
      assert.ok(err.cause, 'preserves underlying cause');
      assert.match(err.cause.message, /fail 3/);
      return true;
    }
  );
  assert.strictEqual(calls, 3);
});

test('fetchWithRetry — non-2xx response is retried then thrown', async () => {
  let calls = 0;
  const fakeFetch = async () => {
    calls += 1;
    return fakeResponse({ ok: false, status: 503, body: '' });
  };
  await assert.rejects(
    fetcher.fetchWithRetry('http://x', {
      maxRetries: 2,
      baseDelayMs: 1,
      fetchImpl: fakeFetch,
      sleepFn: async () => {},
      logger: { warn: () => {}, error: () => {} },
    }),
    /HTTP 503/
  );
  assert.strictEqual(calls, 2);
});

test('fetchWithRetry — sends realistic browser User-Agent', async () => {
  let receivedHeaders = null;
  const fakeFetch = async (_url, opts) => {
    receivedHeaders = opts.headers;
    return fakeResponse({ body: 'OK' });
  };
  await fetcher.fetchWithRetry('http://x', {
    fetchImpl: fakeFetch,
    sleepFn: async () => {},
    logger: { warn: () => {}, error: () => {} },
  });
  assert.ok(receivedHeaders);
  assert.match(receivedHeaders['User-Agent'], /Mozilla\/5\.0/);
  assert.match(receivedHeaders['User-Agent'], /Chrome|Safari/);
  assert.ok(receivedHeaders['Accept'].includes('text/html'));
});

test('fetchWithRetry — passes AbortController.signal to underlying fetch', async () => {
  let receivedSignal = null;
  const fakeFetch = async (_url, opts) => {
    receivedSignal = opts.signal;
    return fakeResponse({ body: 'OK' });
  };
  await fetcher.fetchWithRetry('http://x', {
    timeoutMs: 5000,
    fetchImpl: fakeFetch,
    sleepFn: async () => {},
    logger: { warn: () => {}, error: () => {} },
  });
  assert.ok(receivedSignal, 'AbortController signal was wired');
  assert.strictEqual(typeof receivedSignal.aborted, 'boolean');
});

test('fetchWithRetry — exposes DEFAULT_HEADERS and DEFAULT_USER_AGENT', () => {
  assert.ok(fetcher.DEFAULT_HEADERS);
  assert.match(fetcher.DEFAULT_USER_AGENT, /Mozilla/);
  assert.strictEqual(
    fetcher.DEFAULT_HEADERS['User-Agent'],
    fetcher.DEFAULT_USER_AGENT
  );
});

test('fetchWithRetry — invalid maxRetries rejects', async () => {
  await assert.rejects(
    fetcher.fetchWithRetry('http://x', {
      maxRetries: 0,
      fetchImpl: async () => fakeResponse({ body: '' }),
      sleepFn: async () => {},
      logger: { warn: () => {} },
    }),
    /maxRetries must be >= 1/
  );
});

// ─── URL-hash dedup ──────────────────────────────────────────────────────

test('_canonicalUrlForHash — strips query + trailing slash + lowercases', () => {
  const a = scraper._canonicalUrlForHash(
    'https://app.pear.garden/trade/hl/USDC-WLD+ENA?referral=foo'
  );
  const b = scraper._canonicalUrlForHash(
    'https://app.pear.garden/trade/hl/USDC-WLD+ENA?referral=BlackCatDeFi&utm_source=tg'
  );
  const c = scraper._canonicalUrlForHash(
    'https://app.pear.garden/trade/hl/USDC-WLD+ENA/'
  );
  assert.strictEqual(a, b, 'different referrals canonicalize equally');
  assert.strictEqual(a, c, 'trailing slash stripped');
});

test('_urlHash — produces deterministic sha256 hex', () => {
  const h1 = scraper._urlHash('https://app.pear.garden/trade/hl/USDC-WLD+ENA?referral=foo');
  const h2 = scraper._urlHash('https://app.pear.garden/trade/hl/USDC-WLD+ENA?referral=BlackCatDeFi');
  assert.strictEqual(h1, h2);
  assert.strictEqual(h1.length, 64); // sha256 hex
  assert.match(h1, /^[a-f0-9]{64}$/);
});

test('_urlHash — different paths produce different hashes', () => {
  const h1 = scraper._urlHash('https://app.pear.garden/trade/hl/USDC-WLD+ENA');
  const h2 = scraper._urlHash('https://app.pear.garden/trade/hl/USDC-DYDX+OP');
  assert.notStrictEqual(h1, h2);
});

test('processNewPosts — duplicate URL across different message_ids fires once', async () => {
  store._resetForTests();
  scraper._resetFailureStateForTests();

  let fired = 0;
  const onSignal = async () => { fired += 1; };

  const samePearUrl = 'https://app.pear.garden/trade/hl/USDC-WLD+STRK+ENA';
  const posts = [
    { messageId: 100, channel: 'BlackCatDeFiSignals', pearUrl: samePearUrl, text: 'first' },
    { messageId: 101, channel: 'BlackCatDeFiSignals', pearUrl: samePearUrl + '?referral=other', text: 'edit' },
    { messageId: 102, channel: 'BlackCatDeFiSignals', pearUrl: samePearUrl, text: 'repost' },
  ];
  const dispatched = await scraper.processNewPosts(posts, onSignal);
  assert.strictEqual(dispatched, 1, 'only first basket URL fires');
  assert.strictEqual(fired, 1);

  // Subsequent calls also dedup
  const again = await scraper.processNewPosts(posts, onSignal);
  assert.strictEqual(again, 0);
});

test('processNewPosts — different baskets each fire once', async () => {
  store._resetForTests();
  scraper._resetFailureStateForTests();

  let fired = [];
  const onSignal = async (s) => { fired.push(s); };

  const posts = [
    { messageId: 200, channel: 'BlackCatDeFiSignals',
      pearUrl: 'https://app.pear.garden/trade/hl/USDC-WLD+STRK', text: 'a' },
    { messageId: 201, channel: 'BlackCatDeFiSignals',
      pearUrl: 'https://app.pear.garden/trade/hl/USDC-DYDX+OP', text: 'b' },
  ];
  const n = await scraper.processNewPosts(posts, onSignal);
  assert.strictEqual(n, 2);
  assert.strictEqual(fired.length, 2);
  assert.notStrictEqual(fired[0].url_hash, fired[1].url_hash);
});

// ─── Owner alert hook ────────────────────────────────────────────────────

test('pollOnce — fires hard-alert after FAILURES_HARD_ALERT consecutive failures', async () => {
  store._resetForTests();
  scraper._resetFailureStateForTests();
  // Override global fetch to throw → fetchWithRetry will reject after
  // maxRetries=1 (env var set above) without burning real time.
  const origFetch = global.fetch;
  global.fetch = async () => { throw new Error('network down'); };

  const alerts = [];
  scraper._wireCallbacksForTests({
    onSignal: async () => {},
    onAlert: async (a) => { alerts.push(a); },
  });

  const HARD = scraper.FAILURES_HARD_ALERT;
  for (let i = 0; i < HARD + 1; i++) {
    await scraper.pollOnce();
  }
  const critical = alerts.filter((a) => a.severity === 'critical');
  assert.ok(critical.length >= 1, 'critical alert fired');
  assert.strictEqual(critical[0].consecutiveFailures, HARD);

  // Now make fetch succeed → recovery alert
  global.fetch = async () => fakeResponse({ body: '<html></html>' });
  await scraper.pollOnce();
  const recovery = alerts.filter((a) => a.severity === 'recovery');
  assert.ok(recovery.length >= 1, 'recovery alert fired after success');

  global.fetch = origFetch;
});

test('pollOnce — single hard-alert per outage window (not every failure)', async () => {
  store._resetForTests();
  scraper._resetFailureStateForTests();
  const origFetch = global.fetch;
  global.fetch = async () => { throw new Error('still down'); };

  const alerts = [];
  scraper._wireCallbacksForTests({
    onSignal: async () => {},
    onAlert: async (a) => { alerts.push(a); },
  });

  const HARD = scraper.FAILURES_HARD_ALERT;
  // Way more failures than HARD threshold
  for (let i = 0; i < HARD + 5; i++) {
    await scraper.pollOnce();
  }
  const critical = alerts.filter((a) => a.severity === 'critical');
  assert.strictEqual(critical.length, 1, 'only one critical alert per outage');

  global.fetch = origFetch;
});

// ─── Schedule reflects last-success timestamp ───────────────────────────

test('getSchedule — exposes lastSuccessAt + consecutiveFailures', () => {
  store._resetForTests();
  scraper._resetFailureStateForTests();
  const sch = scraper.getSchedule();
  assert.strictEqual(typeof sch.url, 'string');
  assert.strictEqual(typeof sch.intervalSec, 'number');
  assert.strictEqual(typeof sch.consecutiveFailures, 'number');
  assert.strictEqual(typeof sch.lastSuccessAt, 'number');
});
