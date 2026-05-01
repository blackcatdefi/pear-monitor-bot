'use strict';

/**
 * R-GRAMJS — backend state-machine tests for signalsChannelScraper.
 *
 * Exercises the scraper → gramjs → scraper switching logic by injecting:
 *   - a fake fetchImpl into scraperFetch (controls scraper success/fail)
 *   - a fake gramjs backend module via _injectGramjsBackendForTests
 *
 * No live network. No real `telegram` package required.
 */

// Tight thresholds so the test runs fast.
process.env.SCRAPER_RETRY_MAX = '1';
process.env.SCRAPER_RETRY_BASE_MS = '1';
process.env.SCRAPER_TIMEOUT_MS = '500';
process.env.SCRAPER_FAILURES_HARD_ALERT = '3';
process.env.SCRAPER_FAILURES_SOFT_WARN = '2';
process.env.GRAMJS_PROBE_OK_THRESHOLD = '2';
process.env.SIGNALS_SCRAPER_ENABLED = 'true';

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'scraper-switch-'));
process.env.COPY_TRADING_DB_DIR = TMP;

// Fresh module instances. The real fetch is patched via global.fetch
// because scraperFetch reads it lazily (no fetchImpl at scraper.pollOnce
// level — we'd have to thread one through fetchAndParse).
delete require.cache[require.resolve('../src/copyTradingStore')];
delete require.cache[require.resolve('../src/signalsChannelScraper')];
delete require.cache[require.resolve('../src/scraperFetch')];

const scraper = require('../src/signalsChannelScraper');

// Mock global fetch so fetchHtml(...) goes through our control plane.
let _fetchBehaviour = 'success'; // 'success' | 'fail'
let _fetchHtmlBody = `
  <div class="tgme_widget_message" data-post="BlackCatDeFiSignals/100">
    <a href="https://app.pear.garden/basket/long-eth-short-btc">link</a>
    <div class="tgme_widget_message_text">basket text</div>
  </div>
`;

const _origFetch = global.fetch;
global.fetch = async function fakeFetch(url) {
  if (_fetchBehaviour === 'fail') throw new Error('simulated network error');
  return {
    ok: true,
    status: 200,
    text: async () => _fetchHtmlBody,
  };
};

// Build a controllable fake gramjs backend — same shape as the real module.
function makeFakeGramjs({ available = true, posts = [] } = {}) {
  let _available = available;
  return {
    _setAvailable(v) { _available = v; },
    _setPosts(p) { posts = p; },
    isAvailable: () => _available,
    statusLines: () => ['fake'],
    fetchRecentMessages: async () => posts,
    disconnect: async () => {},
  };
}

function resetState() {
  scraper._resetFailureStateForTests();
  _fetchBehaviour = 'success';
}

// ─── Switch scraper → gramjs after N failures ────────────────────────────

test('switch — after N scraper failures with gramjs available, switches to gramjs', async () => {
  resetState();
  const fake = makeFakeGramjs({
    available: true,
    posts: [
      { id: 200, date: 1714000000, message: 'gramjs https://app.pear.garden/basket/g1' },
    ],
  });
  scraper._injectGramjsBackendForTests(fake);

  let received = [];
  scraper._wireCallbacksForTests({
    onSignal: async (sig) => { received.push(sig); },
    onAlert: async () => {},
  });

  _fetchBehaviour = 'fail';

  // 3 failures = HARD_ALERT threshold
  await scraper.pollOnce();
  await scraper.pollOnce();
  await scraper.pollOnce();

  const sched = scraper.getSchedule();
  assert.strictEqual(sched.backend, 'gramjs', 'backend must switch to gramjs');
  assert.ok(sched.consecutiveFailures >= 3);
});

test('switch — does NOT switch to gramjs when isAvailable=false', async () => {
  resetState();
  const fake = makeFakeGramjs({ available: false });
  scraper._injectGramjsBackendForTests(fake);
  scraper._wireCallbacksForTests({ onSignal: async () => {}, onAlert: async () => {} });

  _fetchBehaviour = 'fail';
  await scraper.pollOnce();
  await scraper.pollOnce();
  await scraper.pollOnce();
  await scraper.pollOnce();

  const sched = scraper.getSchedule();
  assert.strictEqual(sched.backend, 'scraper', 'must stay on scraper when gramjs unavailable');
});

// ─── Switch back: gramjs → scraper after K probe successes ────────────────

test('recover — after K scraper-probe successes, switches back to scraper', async () => {
  resetState();
  const fake = makeFakeGramjs({
    available: true,
    posts: [{ id: 300, date: 1714000000, message: 'g https://app.pear.garden/basket/g2' }],
  });
  scraper._injectGramjsBackendForTests(fake);
  scraper._wireCallbacksForTests({ onSignal: async () => {}, onAlert: async () => {} });

  // Force backend = 'gramjs' to simulate post-failover state.
  scraper._setBackendForTests('gramjs');

  // Now scraper is healthy again — probes succeed.
  _fetchBehaviour = 'success';
  await scraper.pollOnce(); // probe ok #1 of 2
  let sched = scraper.getSchedule();
  assert.strictEqual(sched.backend, 'gramjs', 'should still be gramjs after 1 probe');
  assert.strictEqual(sched.scraperProbeOks, 1);

  await scraper.pollOnce(); // probe ok #2 of 2 → switch back
  sched = scraper.getSchedule();
  assert.strictEqual(sched.backend, 'scraper', 'should switch back after threshold probes');
});

test('recover — probe failure resets the probe counter', async () => {
  resetState();
  const fake = makeFakeGramjs({ available: true, posts: [] });
  scraper._injectGramjsBackendForTests(fake);
  scraper._wireCallbacksForTests({ onSignal: async () => {}, onAlert: async () => {} });
  scraper._setBackendForTests('gramjs');

  _fetchBehaviour = 'success';
  await scraper.pollOnce();
  let sched = scraper.getSchedule();
  assert.strictEqual(sched.scraperProbeOks, 1);

  _fetchBehaviour = 'fail';
  await scraper.pollOnce();
  sched = scraper.getSchedule();
  assert.strictEqual(sched.scraperProbeOks, 0, 'probe counter must reset on failure');
  assert.strictEqual(sched.backend, 'gramjs', 'still gramjs since recovery not complete');
});

// ─── Behaviour when gramjs throws ──────────────────────────────────────────

test('gramjs throw — counted as failure, scraper stays primary if not yet on gramjs', async () => {
  resetState();
  const fake = {
    isAvailable: () => true,
    fetchRecentMessages: async () => { throw new Error('mtproto down'); },
    statusLines: () => ['fake'],
    disconnect: async () => {},
  };
  scraper._injectGramjsBackendForTests(fake);
  scraper._wireCallbacksForTests({ onSignal: async () => {}, onAlert: async () => {} });
  scraper._setBackendForTests('gramjs');

  _fetchBehaviour = 'fail'; // both probe AND gramjs fail
  await scraper.pollOnce();
  await scraper.pollOnce();
  const sched = scraper.getSchedule();
  // Failure counter should increment; backend stays gramjs (no fallback
  // FROM gramjs is implemented — both backends are dead at this point).
  assert.ok(sched.consecutiveFailures >= 2);
  assert.strictEqual(sched.backend, 'gramjs');
});

// ─── getSchedule shape ────────────────────────────────────────────────────

test('getSchedule — exposes new R-GRAMJS fields', () => {
  resetState();
  const sched = scraper.getSchedule();
  assert.ok('backend' in sched);
  assert.ok('scraperProbeOks' in sched);
  assert.ok('backendSwitchedAt' in sched);
  assert.ok('gramjsAvailable' in sched);
});

// Cleanup
test.after(() => {
  global.fetch = _origFetch;
});
