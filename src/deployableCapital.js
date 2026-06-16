'use strict';

/**
 * FIX 2 — Deployable capital ("funds available to trade"), computed honestly.
 *
 * The old alert reported `agg.totalWithdrawable` = the PERP free margin only
 * (Σ state.withdrawable across perp dexes), which UNDER-reports because it
 * ignores the spot stablecoin pool the fund's baskets actually draw from
 * ($535 / $212 fired in prod while the real withdrawable was ~$2,606).
 *
 * The opposite error is just as forbidden: the wallet shows USDC ~2,606,
 * USDT0 ~2,606 and USDH ~2,606. These are NOT three balances — they are the
 * SAME single withdrawable pool shown converted into each stablecoin. Summing
 * them ($7.8K) triples real capital. This is the identical double-count the
 * fund already bans for HyperDash Unified Account totals: alternative views of
 * one pool are never added.
 *
 * Empirical HL findings (wallet 0xc7ae…1505, verified live against the API):
 *   • clearinghouseState.withdrawable  = authoritative PERP free margin scalar
 *     (0 when over-borrow: accountValue < totalMarginUsed).
 *   • spotClearinghouseState.balances  = per-coin spot {total, hold}. Drawn
 *     borrow shows as a NEGATIVE stable `total` (e.g. USDC total = −54,300).
 *   • This fund runs Portfolio Margin: spot and perp share ONE collateral
 *     pool. Perp free margin is therefore NOT a second additive bucket by
 *     default — it is already part of the unified portfolio. Adding it would
 *     double-count. (Override with FUNDS_PERP_SPOT_ADDITIVE=true only if a
 *     wallet genuinely runs isolated spot/perp sub-accounts.)
 *
 * Therefore TOTAL deployable = the single spot stable pool, taken ONCE
 * (max across denominations, never summed). The figure is labelled "drawn dry
 * powder for baskets", never "risk-free headroom" or "new borrow room".
 */

const DEFAULT_STABLES = [
  'USDC',
  'USDT0',
  'USDH',
  'USDE',
  'USDT',
  'USDP',
  'DAI',
  'FEUSD',
  'USDXL',
];

function _stableSet() {
  const extra = (process.env.FUNDS_STABLE_SYMBOLS || '')
    .split(',')
    .map((s) => s.trim().toUpperCase())
    .filter(Boolean);
  return new Set([...DEFAULT_STABLES, ...extra]);
}

function _num(v) {
  const n = typeof v === 'number' ? v : parseFloat(v);
  return Number.isFinite(n) ? n : 0;
}

function isAdditive() {
  return (
    String(process.env.FUNDS_PERP_SPOT_ADDITIVE || 'false').toLowerCase() ===
    'true'
  );
}

/**
 * Collapse spot stablecoin rows into one withdrawable pool.
 *
 * Per-stable withdrawable = max(total − max(hold, 0), 0). The pool is the MAX
 * across denominations (a single pool viewed in different stables), NEVER the
 * sum. We also return the sum purely so callers/tests can assert pool ≤ sum
 * and log the per-coin detail for the smoke test.
 */
function computeSpotPool(spotBalances) {
  if (!Array.isArray(spotBalances)) {
    return { pool: null, perCoin: [], sumOfStables: 0, anyNegativeStable: false };
  }
  const stables = _stableSet();
  const perCoin = [];
  let pool = 0;
  let sumOfStables = 0;
  let anyNegativeStable = false;

  for (const b of spotBalances) {
    const coin = String(b.coin || '').toUpperCase();
    if (!stables.has(coin)) continue;
    const total = _num(b.total);
    const hold = _num(b.hold);
    const withdrawable = Math.max(total - Math.max(hold, 0), 0);
    if (total < 0) anyNegativeStable = true;
    sumOfStables += withdrawable;
    if (withdrawable > pool) pool = withdrawable; // single pool = max, not sum
    perCoin.push({ coin, total, hold, withdrawable });
  }

  return { pool, perCoin, sumOfStables, anyNegativeStable };
}

/**
 * Compute the deployable-capital picture.
 *
 * @param {object} args
 *   spotBalances : normalized spot rows or null (null => spot fetch failed)
 *   perp         : { accountValue, marginUsed, withdrawable } or null
 * @returns {object} result (see fields below). `error:true` when nothing
 *   could be computed — callers must render "fetch error", never $0.00.
 */
function computeDeployable({ spotBalances, perp }) {
  const spot = computeSpotPool(spotBalances);
  const perpAvailable = perp ? Math.max(_num(perp.withdrawable), 0) : null;
  const accountValue = perp ? _num(perp.accountValue) : null;
  const marginUsed = perp ? _num(perp.marginUsed) : null;

  const spotFetched = Array.isArray(spotBalances);
  const perpFetched = !!perp;

  if (!spotFetched && !perpFetched) {
    return { error: true };
  }

  const additive = isAdditive();
  const spotPool = spotFetched ? spot.pool : null;

  // TOTAL deployable:
  //   • unified Portfolio Margin (default): the single spot pool.
  //   • additive override: spot pool + perp free margin (isolated accounts).
  //   • spot fetch failed but perp present: fall back to perp free margin so
  //     we still surface *some* verified number rather than nothing.
  let total;
  if (spotFetched && additive) {
    total = (spotPool || 0) + (perpAvailable || 0);
  } else if (spotFetched) {
    total = spotPool || 0;
  } else {
    total = perpAvailable || 0; // spot unavailable → perp authoritative figure
  }

  // Borrow utilization & over-max-borrow flag.
  let borrowUtilizationPct = null;
  if (perpFetched && accountValue > 0) {
    borrowUtilizationPct = (marginUsed / accountValue) * 100;
  }
  const overMaxBorrow =
    (perpFetched && accountValue > 0 && marginUsed >= accountValue) ||
    (perpFetched && perpAvailable <= 0 && marginUsed > 0) ||
    !!spot.anyNegativeStable;

  return {
    error: false,
    totalDeployable: total,
    spotPool, // null if spot fetch failed
    spotFetched,
    perpFreeMargin: perpAvailable, // null if perp fetch failed
    perpFetched,
    additive,
    accountValue,
    marginUsed,
    borrowUtilizationPct,
    overMaxBorrow,
    perCoin: spot.perCoin,
    sumOfStables: spot.sumOfStables,
  };
}

function _fmtUsd(n) {
  if (!Number.isFinite(n)) return 'n/d';
  return `$${n.toFixed(2)}`;
}

function _fmtPct(n) {
  if (!Number.isFinite(n)) return 'n/d';
  return `${n.toFixed(1)}%`;
}

/**
 * Itemized, honest breakdown block (array of lines, no footer/header).
 * Used by both the monitor alert and the /balance command.
 */
function formatDeployableLines(result) {
  if (!result || result.error) {
    return ['⚠️ Deployable capital: fetch error (data unavailable)'];
  }
  const lines = [];
  if (result.spotFetched) {
    lines.push(`💵 Withdrawable (single pool, any stable): ${_fmtUsd(result.spotPool)}`);
  } else {
    lines.push('💵 Withdrawable (spot): n/d (fetch error)');
  }
  if (result.additive && result.perpFetched) {
    lines.push(`➕ Perp free margin (additive): ${_fmtUsd(result.perpFreeMargin)}`);
  } else if (result.perpFetched) {
    lines.push(`🔹 Perp free margin (already in pool): ${_fmtUsd(result.perpFreeMargin)}`);
  }
  lines.push(`🎯 TOTAL deployable: ${_fmtUsd(result.totalDeployable)}`);
  lines.push('   ↳ takeable in USDC, USDT0 *or* USDH — not all three at once');
  if (result.overMaxBorrow) {
    lines.push(
      `🚨 OVER MAX-BORROW (util ${_fmtPct(result.borrowUtilizationPct)}) — drawn dry powder for baskets, not new borrow room`
    );
  } else if (Number.isFinite(result.borrowUtilizationPct)) {
    lines.push(`📈 Borrow utilization: ${_fmtPct(result.borrowUtilizationPct)}`);
  }
  return lines;
}

module.exports = {
  computeSpotPool,
  computeDeployable,
  formatDeployableLines,
  isAdditive,
  DEFAULT_STABLES,
};
