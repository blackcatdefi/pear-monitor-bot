'use strict';

/**
 * R-PUBLIC-SPAM-FINAL (4 may 2026) — regression test.
 *
 * Reproduce the 21-msg vntl:ANTHROPIC spam (13:07-15:41 UTC):
 *   - same wallet 0xc7AE
 *   - same coin "vntl:ANTHROPIC"
 *   - identical entry $1114.80 / size 1.401 / notional $1,562 / 4x
 *   - 21 messages over 2.5h, every 2-7 minutes
 *
 * The leak path was emitAlerts → INDIVIDUAL_OPEN → shouldSendAlert(60s window
 * per coin). When findNewPositions keeps re-flagging the same leg as new
 * (snapshot churn — dex field flips, or HL surfaces the leg intermittently),
 * shouldSendAlert lets each one through after the 60s window elapses.
 *
 * The fix is the PER_LEG_ALERTS_DISABLED kill switch (default true) in
 * openAlerts.emitAlerts. INDIVIDUAL_OPEN never dispatches; it returns
 * type='INDIVIDUAL_OPEN_BLOCKED' and increments
 * healthServer.perLegAlertsBlockedLifetime.
 *
 * This test:
 *   1. Default behaviour: 21 emit attempts → 0 dispatches, 21 counter ticks
 *   2. PER_LEG_ALERTS_DISABLED=false re-enables (forensic mode)
 *   3. BASKET_OPEN flow is unaffected (3+ legs still emits 1 message)
 */

const path = require('path');
process.env.DEDUP_DB_PATH = path.join(
  __dirname,
  '..',
  'data',
  '__test_per_leg_kill_switch.json'
);
process.env.BASKET_DEDUP_ENABLED = 'true';

const test = require('node:test');
const assert = require('node:assert/strict');

const openAlerts = require('../src/openAlerts');
const healthServer = require('../src/healthServer');
const basketDedup = require('../src/basketDedup');
const lockout = require('../src/walletBasketLockout');
const { _resetCachesForTests } = require('../src/closeAlerts');

function _resetAll() {
  _resetCachesForTests();
  basketDedup._resetForTests();
  openAlerts._resetWalletDebounceForTests();
  lockout._resetForTests({ persist: false });
  healthServer._resetForTests();
}

const VNTL_ANTHROPIC_LEG = {
  coin: 'vntl:ANTHROPIC',
  size: 1.401,
  entryPrice: 1114.8,
  side: 'LONG',
  leverage: 4,
  dex: 'pear',
};

test('isPerLegDisabled defaults to TRUE (kill switch on by default)', () => {
  delete process.env.PER_LEG_ALERTS_DISABLED;
  assert.equal(openAlerts.isPerLegDisabled(), true);
});

test('isPerLegDisabled honors PER_LEG_ALERTS_DISABLED=false (forensic mode)', () => {
  process.env.PER_LEG_ALERTS_DISABLED = 'false';
  assert.equal(openAlerts.isPerLegDisabled(), false);
  delete process.env.PER_LEG_ALERTS_DISABLED;
});

test(
  'apr-may regression: 21x vntl:ANTHROPIC INDIVIDUAL_OPEN → 0 dispatches with kill switch',
  async () => {
    _resetAll();
    delete process.env.PER_LEG_ALERTS_DISABLED; // default = true (disabled)
    const sent = [];
    const wallet = '0xc7ae23316b47f7e75f455f53ad37873a18351505';

    for (let i = 0; i < 21; i++) {
      const r = await openAlerts.emitAlerts({
        chatId: 'cid',
        wallet,
        label: 'BCD primary',
        newPositions: [VNTL_ANTHROPIC_LEG], // 1 leg → INDIVIDUAL_OPEN
        notify: async (cid, msg) => {
          sent.push(msg);
        },
      });
      assert.equal(r.dispatched, 0, `iteration ${i}: dispatched should be 0`);
      assert.equal(r.type, 'INDIVIDUAL_OPEN_BLOCKED');
      assert.equal(r.reason, 'per_leg_alerts_disabled');
    }

    assert.equal(sent.length, 0, 'no telegram messages should be sent');
    const status = healthServer.getStatus();
    assert.equal(
      status.spam_guard.per_leg_alerts_blocked_lifetime,
      21,
      'counter should equal exact 21 blocked legs'
    );
    assert.match(
      String(status.spam_guard.last_per_leg_blocked_reason || ''),
      /vntl:ANTHROPIC/,
      'last reason must include the offending coin'
    );
  }
);

test(
  'INDIVIDUAL_OPEN with 2 legs (sub-basket) — both blocked, counter += 2',
  async () => {
    _resetAll();
    delete process.env.PER_LEG_ALERTS_DISABLED;
    const sent = [];
    const r = await openAlerts.emitAlerts({
      chatId: 'cid',
      wallet: '0xabc',
      label: 'W',
      newPositions: [
        { coin: 'BTC', size: 1, entryPrice: 100000, side: 'LONG' },
        { coin: 'ETH', size: -10, entryPrice: 3000, side: 'SHORT' },
      ],
      notify: async (cid, msg) => sent.push(msg),
    });
    assert.equal(r.dispatched, 0);
    assert.equal(r.type, 'INDIVIDUAL_OPEN_BLOCKED');
    assert.equal(r.blocked_count, 2);
    assert.equal(sent.length, 0);
    assert.equal(
      healthServer.getStatus().spam_guard.per_leg_alerts_blocked_lifetime,
      2
    );
  }
);

test(
  'PER_LEG_ALERTS_DISABLED=false restores legacy per-leg path (forensic mode)',
  async () => {
    _resetAll();
    process.env.PER_LEG_ALERTS_DISABLED = 'false';
    const sent = [];
    const r = await openAlerts.emitAlerts({
      chatId: 'cid',
      wallet: '0xforensicmode',
      label: 'W',
      newPositions: [VNTL_ANTHROPIC_LEG],
      notify: async (cid, msg) => sent.push(msg),
    });
    assert.equal(r.type, 'INDIVIDUAL_OPEN');
    assert.ok(r.dispatched >= 1, 'forensic mode should dispatch the leg');
    assert.equal(sent.length, 1);
    assert.match(sent[0], /NEW POSITION OPENED/);
    delete process.env.PER_LEG_ALERTS_DISABLED;
  }
);

test(
  'BASKET_OPEN flow unaffected by per-leg kill switch (3+ legs → 1 dispatch)',
  async () => {
    _resetAll();
    delete process.env.PER_LEG_ALERTS_DISABLED; // default = true
    const sent = [];
    const r = await openAlerts.emitAlerts({
      chatId: 'cid',
      wallet: '0xbasketwallet',
      label: 'W',
      newPositions: [
        { coin: 'A', size: 1, entryPrice: 1, side: 'LONG' },
        { coin: 'B', size: -1, entryPrice: 1, side: 'SHORT' },
        { coin: 'C', size: 1, entryPrice: 1, side: 'LONG' },
      ],
      notify: async (cid, msg) => sent.push(msg),
    });
    assert.equal(r.type, 'BASKET_OPEN');
    assert.equal(r.dispatched, 1);
    assert.equal(sent.length, 1);
    assert.match(sent[0], /NEW BASKET OPENED/);
    // Per-leg counter should NOT increment for basket flow
    assert.equal(
      healthServer.getStatus().spam_guard.per_leg_alerts_blocked_lifetime,
      0,
      'basket flow must not touch the per-leg counter'
    );
  }
);

test(
  'healthServer /metrics exposes per_leg_alerts_blocked_lifetime gauge',
  async () => {
    _resetAll();
    delete process.env.PER_LEG_ALERTS_DISABLED;
    await openAlerts.emitAlerts({
      chatId: 'cid',
      wallet: '0xmetrics',
      label: 'W',
      newPositions: [VNTL_ANTHROPIC_LEG],
      notify: async () => {},
    });
    const s = healthServer.getStatus();
    assert.equal(s.spam_guard.per_leg_alerts_blocked_lifetime, 1);
    assert.ok(s.spam_guard.last_per_leg_blocked_at);
    assert.match(
      String(s.spam_guard.last_per_leg_blocked_reason || ''),
      /per_leg_disabled/
    );
  }
);
