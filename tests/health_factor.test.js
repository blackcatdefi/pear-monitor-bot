'use strict';

/**
 * R-PUBLIC-V3-TRACKING — Health Factor reader regression suite.
 *
 * Asserts:
 *   1. Address validation accepts checksummed + lowercase 0x...; rejects junk.
 *   2. classify() returns OK / ZERO / UNKNOWN as designed.
 *   3. bucket() splits HF into HEALTHY / WATCH / RISK / INFINITY.
 *   4. readWithCache() returns LIVE on a fresh API success and persists to disk.
 *   5. readWithCache() returns CACHED with ageSeconds on RPC failure if cache exists.
 *   6. readWithCache() returns ERROR when no cache + RPC fails.
 *   7. formatHfMessage renders Markdown with HF + bucket icon for each status.
 *   8. ZERO state returns no-position copy and never crashes downstream.
 *   9. Cache TTL labels age (s/min/h/d) correctly via the formatter.
 *  10. /hf <addr> command path validates input + calls reader.
 */

const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const os = require('os');

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'rhf-'));
process.env.HF_CACHE_PATH = path.join(TMP, 'hf_cache.json');
process.env.RAILWAY_VOLUME_MOUNT_PATH = TMP;
process.env.HEALTH_FACTOR_AUTOREADER = 'true';

const hf = require('../src/healthFactor');
const sm = require('../src/userStateMachine');

const VALID_ADDR = '0xc7ae23316b47f7e75f455f53ad37873a18351505';
const ANOTHER_ADDR = '0xa44e' + 'b'.repeat(36); // 0x + 40 hex
const EMPTY_ADDR = '0x' + '1'.repeat(40);

function makeFakeApi(plan) {
  // plan = array of { ok: boolean, data?, error? } — consumed FIFO.
  const queue = plan.slice();
  return {
    async getAccountData(_addr) {
      const next = queue.shift() || queue[queue.length - 1] || { ok: true, data: { healthFactor: 1.5, totalCollateralUsd: 1000, totalDebtUsd: 500, ltv: 0.5, currentLiquidationThreshold: 0.7, availableBorrowsUsd: 100 } };
      if (!next.ok) throw new Error(next.error || 'rpc fail');
      return next.data;
    },
  };
}

test.beforeEach(() => {
  hf._resetCacheForTests(process.env.HF_CACHE_PATH);
  sm._resetForTests();
});

// 1. Address validation.
test('isValidAddress accepts 0x+40hex; rejects junk', () => {
  assert.strictEqual(hf.isValidAddress(VALID_ADDR), true);
  assert.strictEqual(hf.isValidAddress(VALID_ADDR.toUpperCase()), true);
  assert.strictEqual(hf.isValidAddress('0xabc'), false);
  assert.strictEqual(hf.isValidAddress(''), false);
  assert.strictEqual(hf.isValidAddress(null), false);
  assert.strictEqual(hf.isValidAddress('not an address'), false);
  assert.strictEqual(hf.isValidAddress('0xZ'.padEnd(42, '0')), false);
});

// 2. classify cases.
test('classify returns OK / ZERO / UNKNOWN', () => {
  assert.strictEqual(hf.classify({ totalCollateralUsd: 100, totalDebtUsd: 50, healthFactor: 1.5 }), 'OK');
  assert.strictEqual(hf.classify({ totalCollateralUsd: 100, totalDebtUsd: 0, healthFactor: Infinity }), 'OK');
  assert.strictEqual(hf.classify({ totalCollateralUsd: 0, totalDebtUsd: 0 }), 'ZERO');
  assert.strictEqual(hf.classify({ error: 'rpc fail' }), 'UNKNOWN');
  assert.strictEqual(hf.classify({ totalCollateralUsd: 100, totalDebtUsd: 50, healthFactor: NaN }), 'UNKNOWN');
});

// 3. bucket thresholds.
test('bucket splits HF into HEALTHY / WATCH / RISK / INFINITY / UNKNOWN', () => {
  assert.strictEqual(hf.bucket(2.0), 'HEALTHY');
  assert.strictEqual(hf.bucket(1.10), 'HEALTHY');
  assert.strictEqual(hf.bucket(1.099), 'WATCH');
  assert.strictEqual(hf.bucket(1.05), 'WATCH');
  assert.strictEqual(hf.bucket(1.049), 'RISK');
  assert.strictEqual(hf.bucket(0.95), 'RISK');
  assert.strictEqual(hf.bucket(Infinity), 'INFINITY');
  assert.strictEqual(hf.bucket(NaN), 'UNKNOWN');
  assert.strictEqual(hf.bucket(null), 'UNKNOWN');
});

// 4. readWithCache LIVE + persists to disk.
test('readWithCache LIVE persists to disk + returns hf bucket', async () => {
  const api = makeFakeApi([{
    ok: true,
    data: {
      healthFactor: 1.214, totalCollateralUsd: 4018, totalDebtUsd: 881,
      ltv: 0.219, currentLiquidationThreshold: 0.825, availableBorrowsUsd: 1500,
    },
  }]);
  const r = await hf.readWithCache(VALID_ADDR, { api });
  assert.strictEqual(r.status, 'LIVE');
  assert.strictEqual(r.hfBucket, 'HEALTHY');
  assert.strictEqual(r.collateral, 4018);
  assert.strictEqual(r.debt, 881);
  assert.strictEqual(r.recovered, false);

  // Cache file written.
  const raw = JSON.parse(fs.readFileSync(process.env.HF_CACHE_PATH, 'utf-8'));
  assert.ok(raw[VALID_ADDR.toLowerCase()]);
  assert.strictEqual(Number(raw[VALID_ADDR.toLowerCase()].hf), 1.214);
});

// 5. readWithCache CACHED on RPC failure.
test('readWithCache returns CACHED on RPC failure when entry exists', async () => {
  const apiOk = makeFakeApi([{
    ok: true,
    data: {
      healthFactor: 1.5, totalCollateralUsd: 1000, totalDebtUsd: 500,
      ltv: 0.5, currentLiquidationThreshold: 0.7, availableBorrowsUsd: 100,
    },
  }]);
  const r1 = await hf.readWithCache(VALID_ADDR, { api: apiOk });
  assert.strictEqual(r1.status, 'LIVE');

  // Now break the RPC.
  const apiFail = { async getAccountData() { throw new Error('rate limit'); } };
  const r2 = await hf.readWithCache(VALID_ADDR, { api: apiFail });
  assert.strictEqual(r2.status, 'CACHED');
  assert.strictEqual(r2.recovered, true);
  assert.strictEqual(r2.collateral, 1000);
  assert.strictEqual(r2.hf, 1.5);
  assert.ok(typeof r2.ageSeconds === 'number' && r2.ageSeconds >= 0);
});

// 6. readWithCache returns ERROR when no cache + RPC fails.
test('readWithCache returns ERROR with friendly message when nothing cached', async () => {
  const apiFail = { async getAccountData() { throw new Error('rpc broken'); } };
  const r = await hf.readWithCache(EMPTY_ADDR, { api: apiFail });
  assert.strictEqual(r.status, 'ERROR');
  assert.match(r.error, /rpc broken|no data|wallet/i);
});

// 7. formatHfMessage renders Markdown.
test('formatHfMessage renders Markdown for LIVE / CACHED / ZERO / ERROR', () => {
  const live = hf.formatHfMessage(VALID_ADDR, {
    status: 'LIVE', hf: 1.214, hfBucket: 'HEALTHY',
    collateral: 4018, debt: 881, ltv: 0.219, ageSeconds: 0,
  });
  assert.match(live, /Health Factor/);
  assert.match(live, /1\.214/);
  assert.match(live, /Healthy/i);
  assert.match(live, /\$4K|\$4\.0K|\$4,018|4018|4K/);

  const watch = hf.formatHfMessage(VALID_ADDR, {
    status: 'LIVE', hf: 1.07, hfBucket: 'WATCH',
    collateral: 100, debt: 90, ltv: 0.9,
  });
  assert.match(watch, /Watch/i);

  const risk = hf.formatHfMessage(VALID_ADDR, {
    status: 'LIVE', hf: 1.01, hfBucket: 'RISK',
    collateral: 100, debt: 95, ltv: 0.95,
  });
  assert.match(risk, /Risk/i);

  const inf = hf.formatHfMessage(VALID_ADDR, {
    status: 'LIVE', hf: Infinity, hfBucket: 'INFINITY',
    collateral: 1000, debt: 0, ltv: 0,
  });
  assert.match(inf, /∞/);
  assert.match(inf, /no debt/i);

  const cached = hf.formatHfMessage(VALID_ADDR, {
    status: 'CACHED', hf: 1.214, hfBucket: 'HEALTHY',
    collateral: 4018, debt: 881, ltv: 0.219, ageSeconds: 1500,
  });
  assert.match(cached, /cached/i);
  assert.match(cached, /25min/);

  const err = hf.formatHfMessage(VALID_ADDR, { status: 'ERROR', error: 'rpc' });
  assert.match(err, /Could not read/i);
});

// 8. ZERO state.
test('readWithCache returns ZERO on empty wallet without throwing', async () => {
  const api = makeFakeApi([{
    ok: true,
    data: {
      healthFactor: Infinity, totalCollateralUsd: 0, totalDebtUsd: 0,
      ltv: 0, currentLiquidationThreshold: 0, availableBorrowsUsd: 0,
    },
  }]);
  const r = await hf.readWithCache(EMPTY_ADDR, { api });
  assert.strictEqual(r.status, 'ZERO');
  const txt = hf.formatHfMessage(EMPTY_ADDR, r);
  assert.match(txt, /No HyperLend position/i);
});

// 9. Address sanity: invalid address short-circuits to ERROR.
test('readWithCache rejects invalid address with friendly error', async () => {
  const r = await hf.readWithCache('not-a-wallet');
  assert.strictEqual(r.status, 'ERROR');
  assert.match(r.error, /invalid address/i);
});

// 10. /hf command path validates address and calls reader.
test('/hf <address> command path validates + calls reader', async () => {
  const cmds = require('../src/commandsHealthFactor');
  // Mock api injected globally via require chain — we patch readWithCache.
  const originalRead = hf.readWithCache;
  let calledWith = null;
  hf.readWithCache = async (addr) => {
    calledWith = addr;
    return {
      status: 'LIVE', hf: 1.5, hfBucket: 'HEALTHY',
      collateral: 1000, debt: 500, ltv: 0.5, ageSeconds: 0,
    };
  };
  try {
    const sent = [];
    const bot = {
      sent,
      _textHandlers: [],
      _msgHandlers: [],
      onText(re, fn) { this._textHandlers.push({ re, fn }); },
      on(ev, fn) { if (ev === 'message') this._msgHandlers.push(fn); },
      sendMessage: async (cid, t, opts) => { sent.push({ cid, t, opts }); },
      answerCallbackQuery: async () => {},
    };
    cmds.attach(bot);

    // /hf with no arg → enters AWAITING_HF_ADDRESS.
    sm._resetForTests();
    for (const { re, fn } of bot._textHandlers) {
      if (re.test('/hf')) {
        await fn({ chat: { id: 1 }, from: { id: 1 }, text: '/hf' }, '/hf'.match(re));
      }
    }
    assert.strictEqual(sm.getState(1).state, sm.STATES.AWAITING_HF_ADDRESS);
    assert.match(sent[sent.length - 1].t, /Health Factor reader/i);

    // /hf <addr> → reads directly, no state change.
    sm._resetForTests();
    sent.length = 0;
    for (const { re, fn } of bot._textHandlers) {
      const m = `/hf ${VALID_ADDR}`.match(re);
      if (m) await fn({ chat: { id: 1 }, from: { id: 1 }, text: `/hf ${VALID_ADDR}` }, m);
    }
    assert.strictEqual(calledWith, VALID_ADDR);
    assert.match(sent[sent.length - 1].t, /Health Factor/i);

    // Plain text in AWAITING_HF_ADDRESS state → consumes + reads.
    sm.setState(2, sm.STATES.AWAITING_HF_ADDRESS, { userId: 2 });
    calledWith = null;
    sent.length = 0;
    for (const fn of bot._msgHandlers) {
      await fn({ chat: { id: 2 }, from: { id: 2 }, text: VALID_ADDR });
    }
    assert.strictEqual(calledWith, VALID_ADDR);
    assert.strictEqual(sm.getState(2).state, sm.STATES.IDLE);
  } finally {
    hf.readWithCache = originalRead;
  }
});
