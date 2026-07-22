'use strict';

/**
 * R-PUBLIC-FUNDS — Universal deployable-capital engine fixtures.
 *
 * Acceptance-criteria fixtures: unified, perp-only, spot-only, PM-with-debt,
 * empty. Each must return a correct deployable view without crashing.
 */

const test = require('node:test');
const assert = require('node:assert');

const {
  computeUniversalDeployable,
  formatDeployableView,
  DEFAULT_LTV,
  LIQ_THRESHOLD_HYPE,
} = require('../src/fundsEngine');

const PRICES = { HYPE: 40, UBTC: 100000, USDC: 1, USDT0: 1, USDH: 1 };

// ───────────────────────────── fixtures ─────────────────────────────

test('fixture UNIFIED: spot stables + perp balance → spot MAX rule + withdrawable, additive', () => {
  const v = computeUniversalDeployable({
    spotBalances: [
      { coin: 'USDC', total: 1200, hold: 200 },
      { coin: 'USDT0', total: 800, hold: 0 },
    ],
    perp: { accountValue: 5000, marginUsed: 2000, withdrawable: 1500 },
    prices: PRICES,
  });
  assert.strictEqual(v.error, false);
  assert.strictEqual(v.account_type, 'unified');
  // MAX rule: max(1200-200, 800) = 1000, NEVER 1000+800
  assert.strictEqual(v.spot_free, 1000);
  assert.strictEqual(v.perp_withdrawable, 1500);
  assert.strictEqual(v.pm_borrow_headroom, null);
  assert.strictEqual(v.total_deployable, 2500);
});

test('fixture PERP-ONLY: no spot balances → perp withdrawable only', () => {
  const v = computeUniversalDeployable({
    spotBalances: [],
    perp: { accountValue: 3000, marginUsed: 1000, withdrawable: 900 },
    prices: PRICES,
  });
  assert.strictEqual(v.account_type, 'perp_only');
  assert.strictEqual(v.spot_free, 0);
  assert.strictEqual(v.perp_withdrawable, 900);
  assert.strictEqual(v.total_deployable, 900);
});

test('fixture SPOT-ONLY: stables only, zero perp → spot pool only', () => {
  const v = computeUniversalDeployable({
    spotBalances: [{ coin: 'USDC', total: 750, hold: 0 }],
    perp: { accountValue: 0, marginUsed: 0, withdrawable: 0 },
    prices: PRICES,
  });
  assert.strictEqual(v.account_type, 'spot_only');
  assert.strictEqual(v.spot_free, 750);
  assert.strictEqual(v.total_deployable, 750);
});

test('fixture EMPTY: nothing anywhere → empty, $0, no crash', () => {
  const v = computeUniversalDeployable({
    spotBalances: [],
    perp: { accountValue: 0, marginUsed: 0, withdrawable: 0 },
    prices: PRICES,
  });
  assert.strictEqual(v.account_type, 'empty');
  assert.strictEqual(v.total_deployable, 0);
  const lines = formatDeployableView(v, '0x' + 'a'.repeat(40));
  assert.ok(lines.length > 0);
});

test('fixture PM-WITH-DEBT: HYPE collateral + borrowed USDC → headroom + projected liq', () => {
  // 1000 HYPE @ $40 = $40,000 collateral; LTV 0.50 → capacity $20,000
  // debt $12,000 → headroom $8,000
  const v = computeUniversalDeployable({
    spotBalances: [
      { coin: 'HYPE', total: 1000, hold: 0 },
      { coin: 'USDC', total: -12000, hold: 0 },
    ],
    perp: { accountValue: 100, marginUsed: 0, withdrawable: 100 },
    prices: PRICES,
  });
  assert.strictEqual(v.account_type, 'pm');
  assert.strictEqual(v.spot_free, 0); // negative stables NEVER free
  assert.strictEqual(v.pm_borrow_headroom, 40000 * 0.5 - 12000);
  // projected liq if full headroom borrowed: (12000+8000)/(0.7125*1000)
  const expectedLiq = 20000 / (LIQ_THRESHOLD_HYPE * 1000);
  assert.ok(Math.abs(v.pm.projected_liq - expectedLiq) < 1e-9);
  assert.strictEqual(v.total_deployable, 100 + 8000);
});

test('fixture PM over-borrowed: debt > capacity → headroom clamps to 0, never negative', () => {
  const v = computeUniversalDeployable({
    spotBalances: [
      { coin: 'HYPE', total: 100, hold: 0 }, // $4,000 → capacity $2,000
      { coin: 'USDC', total: -4800, hold: 0 }, // debt $4,800 → raw −$2,800
    ],
    perp: { accountValue: 0, marginUsed: 0, withdrawable: 0 },
    prices: PRICES,
  });
  assert.strictEqual(v.account_type, 'pm');
  assert.strictEqual(v.pm_borrow_headroom, 0);
  assert.ok(v.total_deployable >= 0);
});

test('PM multi-asset collateral: headroom computed, liq projection skipped', () => {
  const v = computeUniversalDeployable({
    spotBalances: [
      { coin: 'HYPE', total: 500, hold: 0 },
      { coin: 'UBTC', total: 0.1, hold: 0 },
      { coin: 'USDC', total: -1000, hold: 0 },
    ],
    perp: null,
    prices: PRICES,
  });
  assert.strictEqual(v.account_type, 'pm');
  // UBTC has no LTV in the default map → contributes 0 capacity, flagged
  assert.strictEqual(v.pm_borrow_headroom, 500 * 40 * 0.5 - 1000);
  assert.strictEqual(v.pm.projected_liq, null);
  assert.strictEqual(v.pm.liq_skipped_reason, 'multi_asset_collateral');
  assert.deepStrictEqual(v.pm.unknown_ltv_assets, ['UBTC']);
});

test('non-stable collateral WITHOUT debt is not PM; unified w/ headroom info', () => {
  const v = computeUniversalDeployable({
    spotBalances: [
      { coin: 'HYPE', total: 100, hold: 0 },
      { coin: 'USDC', total: 500, hold: 0 },
    ],
    perp: { accountValue: 1000, marginUsed: 0, withdrawable: 1000 },
    prices: PRICES,
  });
  assert.strictEqual(v.account_type, 'unified');
  assert.strictEqual(v.spot_free, 500);
});

// ───────────────────────────── failure semantics ─────────────────────────────

test('both legs failed → error:true (render "fetch error", never $0)', () => {
  const v = computeUniversalDeployable({ spotBalances: null, perp: null, prices: null });
  assert.strictEqual(v.error, true);
  const lines = formatDeployableView(v, '0x' + 'b'.repeat(40));
  assert.match(lines.join('\n'), /fetch error/);
  assert.doesNotMatch(lines.join('\n'), /\$0\.00/);
});

test('spot failed, perp ok → spot renders fetch error, total = perp leg only', () => {
  const v = computeUniversalDeployable({
    spotBalances: null,
    perp: { accountValue: 2000, marginUsed: 0, withdrawable: 2000 },
    prices: PRICES,
  });
  assert.strictEqual(v.error, false);
  assert.strictEqual(v.spot_free, null);
  assert.strictEqual(v.total_deployable, 2000);
  const txt = formatDeployableView(v, '0x' + 'c'.repeat(40)).join('\n');
  assert.match(txt, /Spot free stables: fetch error/);
});

test('PM with unpriced debt asset → headroom null (unknown), not fabricated', () => {
  const v = computeUniversalDeployable({
    spotBalances: [
      { coin: 'HYPE', total: 100, hold: 0 },
      { coin: 'WEIRDCOIN', total: -50, hold: 0 },
    ],
    perp: null,
    prices: PRICES, // WEIRDCOIN has no price
  });
  assert.strictEqual(v.account_type, 'pm');
  assert.strictEqual(v.pm_borrow_headroom, null);
});

test('negative stable is never counted as spot free (borrow, not balance)', () => {
  const v = computeUniversalDeployable({
    spotBalances: [
      { coin: 'USDC', total: -500, hold: 0 },
      { coin: 'USDT0', total: 300, hold: 0 },
    ],
    perp: null,
    prices: PRICES,
  });
  assert.strictEqual(v.spot_free, 300);
  assert.strictEqual(v.account_type, 'pm'); // negative balance ⇒ PM
});

test('DEFAULT_LTV documents HYPE at 0.50', () => {
  assert.strictEqual(DEFAULT_LTV.HYPE, 0.5);
});

test('PM_LTV_MAP env override extends the LTV map', () => {
  process.env.PM_LTV_MAP = '{"UBTC":0.7}';
  try {
    const v = computeUniversalDeployable({
      spotBalances: [
        { coin: 'UBTC', total: 0.1, hold: 0 },
        { coin: 'USDC', total: -1000, hold: 0 },
      ],
      perp: null,
      prices: PRICES,
    });
    assert.strictEqual(v.pm_borrow_headroom, 0.1 * 100000 * 0.7 - 1000);
  } finally {
    delete process.env.PM_LTV_MAP;
  }
});

// ───────────── R-PUBLIC-FUNDS-FIX1 — production identifier shape ─────────────
// The real 0xc7ae payload: balances carry `token` (canonical index) and a
// per-balance `ltv` string; ctxs are NOT positionally aligned with universe,
// so prices must resolve via the IDX:<token> key, never a name-collision mid.

test('FIX1: HYPE recognized under exact production identifier (token 150 + api ltv 0.5)', () => {
  const view = computeUniversalDeployable({
    spotBalances: [
      { coin: 'USDC', token: 0, total: -91722.704, hold: -91722.704, api_ltv: 0 },
      { coin: 'HYPE', token: 150, total: 3006.2846298, hold: 3006.2846298, api_ltv: 0.5 },
      { coin: 'USDH', token: 360, total: 0.35931473, hold: 0 },
      { coin: 'MAX', token: 734, total: 3376.100606, hold: 0 },
    ],
    perp: { accountValue: 0, marginUsed: 0, withdrawable: 0 },
    // IDX key = real HYPE/USDC mid; name key poisoned with a fake dup price —
    // the engine must prefer the token-index key.
    prices: { 'IDX:150': 59.42, HYPE: 0.144015 },
  });
  assert.strictEqual(view.account_type, 'pm');
  const hype = view.pm.collateral.find((c) => c.coin === 'HYPE');
  assert.strictEqual(hype.tokens, 3006.2846298);
  assert.strictEqual(hype.price, 59.42);          // NOT the 0.144 dup artifact
  assert.strictEqual(hype.ltv, 0.5);              // from api_ltv, no map needed
  const expCap = 3006.2846298 * 59.42 * 0.5;      // ≈ $89,317
  assert.ok(Math.abs(view.pm.capacity - expCap) < 1);
  // Real headroom number, not a mapping artifact: max(0, cap − debt).
  assert.strictEqual(view.pm_borrow_headroom, Math.max(0, view.pm.capacity - view.pm.debt));
  // MAX (no api ltv, not in map) stays conservative $0 WITH the note.
  assert.ok(view.pm.unknown_ltv_assets.includes('MAX'));
});

test('FIX1: alert can fire once HYPE capacity clears debt + threshold', () => {
  const view = computeUniversalDeployable({
    spotBalances: [
      { coin: 'USDC', token: 0, total: -91722.704, hold: -91722.704 },
      { coin: 'HYPE', token: 150, total: 3006.2846298, hold: 3006.2846298, api_ltv: 0.5 },
    ],
    perp: null,
    prices: { 'IDX:150': 70.0 }, // HYPE pump: cap ≈ $105.2K > debt $91.7K
  });
  assert.ok(view.pm_borrow_headroom > 13000);
  assert.ok(view.total_deployable > 13000);
});

test('FIX1: PM_LTV_MAP override works by token INDEX key and beats api_ltv', () => {
  process.env.PM_LTV_MAP = '{"150":0.4}';
  try {
    const view = computeUniversalDeployable({
      spotBalances: [
        { coin: 'USDC', token: 0, total: -1000, hold: -1000 },
        { coin: 'HYPE', token: 150, total: 100, hold: 100, api_ltv: 0.5 },
      ],
      perp: null,
      prices: { 'IDX:150': 50 },
    });
    const hype = view.pm.collateral.find((c) => c.coin === 'HYPE');
    assert.strictEqual(hype.ltv, 0.4); // env index-key override wins
    assert.ok(Math.abs(view.pm.capacity - 100 * 50 * 0.4) < 1e-6);
  } finally {
    delete process.env.PM_LTV_MAP;
  }
});

test('FIX1: PM_LTV_MAP override still works by NAME key', () => {
  process.env.PM_LTV_MAP = '{"ubtc":0.7}';
  try {
    const view = computeUniversalDeployable({
      spotBalances: [
        { coin: 'USDC', token: 0, total: -1000, hold: -1000 },
        { coin: 'UBTC', token: 197, total: 0.5, hold: 0.5 },
      ],
      perp: null,
      prices: { 'IDX:197': 100000 },
    });
    const u = view.pm.collateral.find((c) => c.coin === 'UBTC');
    assert.strictEqual(u.ltv, 0.7);
  } finally {
    delete process.env.PM_LTV_MAP;
  }
});

test('FIX1: legacy fixtures without token/api_ltv still resolve HYPE via DEFAULT_LTV', () => {
  const view = computeUniversalDeployable({
    spotBalances: [
      { coin: 'USDC', total: -1000, hold: -1000 },
      { coin: 'HYPE', total: 10, hold: 10 },
    ],
    perp: null,
    prices: { HYPE: 40 },
  });
  const hype = view.pm.collateral.find((c) => c.coin === 'HYPE');
  assert.strictEqual(hype.ltv, 0.5);
  assert.strictEqual(hype.price, 40);
});
