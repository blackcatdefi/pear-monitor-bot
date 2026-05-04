'use strict';

/**
 * R-PUBLIC-BASKET-SPAM-NUCLEAR — /health spam_guard counter wiring.
 *
 * Validates:
 *   1. recordEventDeduplicated() and recordPhantomSuppressed() exist.
 *   2. They monotonically increment lifetime counters.
 *   3. getStatus() exposes them under spam_guard.{events_deduplicated_lifetime,
 *      phantom_events_suppressed_lifetime, last_dedup_at, last_phantom_suppressed_at}.
 *   4. /metrics text output emits the two prometheus-style counters so a
 *      future Grafana board can scrape them without code changes.
 */

const test = require('node:test');
const assert = require('node:assert');

const healthServer = require('../src/healthServer');

test('healthServer exports recordEventDeduplicated and recordPhantomSuppressed', () => {
  assert.strictEqual(typeof healthServer.recordEventDeduplicated, 'function');
  assert.strictEqual(typeof healthServer.recordPhantomSuppressed, 'function');
});

test('counters start at 0 and increment monotonically', () => {
  healthServer._resetForTests();
  let s = healthServer.getStatus();
  assert.strictEqual(s.spam_guard.events_deduplicated_lifetime, 0);
  assert.strictEqual(s.spam_guard.phantom_events_suppressed_lifetime, 0);

  healthServer.recordEventDeduplicated('test:basket-1');
  healthServer.recordEventDeduplicated('test:basket-2');
  healthServer.recordPhantomSuppressed('test:phantom-1');

  s = healthServer.getStatus();
  assert.strictEqual(s.spam_guard.events_deduplicated_lifetime, 2);
  assert.strictEqual(s.spam_guard.phantom_events_suppressed_lifetime, 1);
});

test('last reason and timestamp are captured', () => {
  healthServer._resetForTests();
  healthServer.recordEventDeduplicated('basketDedup.hit:0xc7ae:abcd1234');
  healthServer.recordPhantomSuppressed('monitor.legacy_close_drop:0xc7ae:DYDX');

  const s = healthServer.getStatus();
  assert.strictEqual(
    s.spam_guard.last_dedup_reason,
    'basketDedup.hit:0xc7ae:abcd1234'
  );
  assert.match(
    s.spam_guard.last_phantom_reason,
    /monitor\.legacy_close_drop/
  );
  assert.match(
    s.spam_guard.last_dedup_at,
    /^\d{4}-\d{2}-\d{2}T/,
    'ISO timestamp expected'
  );
  assert.match(
    s.spam_guard.last_phantom_suppressed_at,
    /^\d{4}-\d{2}-\d{2}T/
  );
});

test('long reason strings are truncated to 200 chars', () => {
  healthServer._resetForTests();
  const big = 'x'.repeat(500);
  healthServer.recordEventDeduplicated(big);
  const s = healthServer.getStatus();
  assert.strictEqual(s.spam_guard.last_dedup_reason.length, 200);
});

test('null reason is preserved as null (no crash)', () => {
  healthServer._resetForTests();
  healthServer.recordEventDeduplicated();
  healthServer.recordPhantomSuppressed();
  const s = healthServer.getStatus();
  assert.strictEqual(s.spam_guard.last_dedup_reason, null);
  assert.strictEqual(s.spam_guard.last_phantom_reason, null);
  assert.strictEqual(s.spam_guard.events_deduplicated_lifetime, 1);
  assert.strictEqual(s.spam_guard.phantom_events_suppressed_lifetime, 1);
});

test('/metrics text contains the two new counters', async () => {
  healthServer._resetForTests();
  healthServer.recordEventDeduplicated('m1');
  healthServer.recordEventDeduplicated('m2');
  healthServer.recordEventDeduplicated('m3');
  healthServer.recordPhantomSuppressed('p1');

  // Spin up server on an ephemeral port and curl /metrics.
  const PORT = 18099 + Math.floor(Math.random() * 1000);
  const server = healthServer.start(PORT);

  await new Promise((resolve) => setTimeout(resolve, 50));

  const http = require('http');
  const body = await new Promise((resolve, reject) => {
    http
      .get(`http://127.0.0.1:${PORT}/metrics`, (res) => {
        let data = '';
        res.on('data', (c) => (data += c));
        res.on('end', () => resolve(data));
      })
      .on('error', reject);
  });

  await new Promise((resolve) => server.close(resolve));

  assert.match(
    body,
    /pear_alerts_events_deduplicated_lifetime\s+3/,
    '/metrics must expose dedup counter'
  );
  assert.match(
    body,
    /pear_alerts_phantom_events_suppressed_lifetime\s+1/,
    '/metrics must expose phantom counter'
  );
});
