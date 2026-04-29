'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

// Bridge reads env at module load — disable webhook before require so the
// default behaviour (skipped: true) kicks in for these tests.
delete process.env.PRINCIPAL_WEBHOOK_URL;
delete process.env.PRINCIPAL_WEBHOOK_SECRET;

const { publish, getStats, WEBHOOK_ENABLED, _resetForTests } =
  require('../src/principalBridge');

test('WEBHOOK_ENABLED defaults to false when no URL', () => {
  assert.equal(WEBHOOK_ENABLED, false);
});

test('publish: returns { logged, webhook } with webhook skipped when no URL', async () => {
  _resetForTests();
  const r = await publish({
    type: 'FULL_CLOSE', wallet: '0xa', coin: 'BLUR', pnl: 406.94,
  });
  assert.equal(typeof r.logged, 'boolean');
  assert.equal(r.webhook.skipped, true);
});

test('getStats reports webhook disabled', () => {
  const s = getStats();
  assert.equal(s.webhook_enabled, false);
  assert.equal(s.webhook_url_configured, false);
  assert.ok('sent' in s && 'failed' in s);
});
