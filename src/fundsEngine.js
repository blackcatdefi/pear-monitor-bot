'use strict';

/**
 * R-PUBLIC-FUNDS — Universal Deployable Capital engine.
 *
 * Computes "capital available to deploy" for ANY HyperLiquid account type:
 *
 *   • unified   — spot + perp balances present, no borrow
 *   • perp_only — clearinghouseState only
 *   • spot_only — spotClearinghouseState only
 *   • pm        — Portfolio Margin: borrowed (negative) spot balances,
 *                 typically non-stable collateral (e.g. HYPE) backing a
 *                 stable debt. THE NEW PART: borrow headroom.
 *   • empty     — wallet exists but holds nothing
 *
 * Legs:
 *   spot_free          = free stables from spotClearinghouseState.
 *                        Per stable: free = max(total − max(hold,0), 0).
 *                        The pool is the MAX across USDC/USDT0/USDH/…
 *                        denominations, NEVER the sum (they are alternative
 *                        views of one pool — summing triples capital).
 *                        Negative totals are borrows and are NEVER counted
 *                        as free. (Reuses computeSpotPool from
 *                        deployableCapital.js — the R-DATA-INTEGRITY rule.)
 *   perp_withdrawable  = clearinghouseState.withdrawable (authoritative
 *                        perp free-margin scalar; 0 when over-levered).
 *   pm_borrow_headroom = Σ(collateral_value × max_borrow_LTV) − debt,
 *                        clamped at 0. Collateral = positive NON-stable
 *                        spot balances valued at live spot mids. Debt =
 *                        abs(negative balances): stables at $1, non-stables
 *                        at live mids.
 *
 * LTV source: HL's public info API does not expose a per-asset max-borrow
 * LTV list (verified against spotMeta / spotMetaAndAssetCtxs — token entries
 * carry szDecimals/weiDecimals/index, no risk params). We therefore ship a
 * MAINTAINED CONSTANT MAP (DEFAULT_LTV, HYPE=0.50) overridable at runtime
 * via env PM_LTV_MAP='{"HYPE":0.5,"UBTC":0.7}'. Assets with no known LTV
 * contribute ZERO borrow capacity (conservative — never overstate headroom).
 *
 * Projected liquidation if the user borrows the FULL headroom:
 *   single-asset HYPE collateral → liq = total_debt / (0.7125 × tokens)
 *   (0.7125 = HYPE liquidation threshold; env PM_LIQ_THRESHOLD_HYPE).
 *   Multi-asset or non-HYPE collateral → headroom only, liq skipped
 *   (never guess).
 *
 * Fetch failure semantics: a failed leg resolves to null and renders as
 * "fetch error" — NEVER $0.00. If every leg fails → { error: true }.
 */

const axios = require('axios');
const { computeSpotPool, DEFAULT_STABLES } = require('./deployableCapital');

const HL_API = process.env.HYPERLIQUID_API_URL || 'https://api.hyperliquid.xyz';

const DEFAULT_LTV = { HYPE: 0.5 };
const LIQ_THRESHOLD_HYPE = parseFloat(
  process.env.PM_LIQ_THRESHOLD_HYPE || '0.7125'
);

function _ltvMap() {
  let map = { ...DEFAULT_LTV };
  try {
    if (process.env.PM_LTV_MAP) {
      const extra = JSON.parse(process.env.PM_LTV_MAP);
      for (const [k, v] of Object.entries(extra)) {
        const n = parseFloat(v);
        if (Number.isFinite(n) && n >= 0 && n <= 1) map[k.toUpperCase()] = n;
      }
    }
  } catch (_) {
    /* malformed env → defaults */
  }
  return map;
}

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

// ───────────────────────────── fetch layer ─────────────────────────────

async function _post(body) {
  const { data } = await axios.post(`${HL_API}/info`, body, { timeout: 15000 });
  return data;
}

// Spot mid-price map { COIN → usd }. Cached (default 5 min) because the
// scheduler scans many wallets per cycle and prices are wallet-independent.
let _priceCache = { at: 0, map: null };
const PRICE_CACHE_MS = parseInt(
  process.env.FUNDS_PRICE_CACHE_MS || String(5 * 60 * 1000), 10
);

async function fetchSpotPrices() {
  if (_priceCache.map && Date.now() - _priceCache.at < PRICE_CACHE_MS) {
    return _priceCache.map;
  }
  try {
    const data = await _post({ type: 'spotMetaAndAssetCtxs' });
    const meta = data && data[0];
    const ctxs = data && data[1];
    if (!meta || !Array.isArray(meta.universe) || !Array.isArray(ctxs)) {
      return _priceCache.map; // stale better than nothing
    }
    const tokens = meta.tokens || [];
    const map = {};
    for (let i = 0; i < meta.universe.length; i++) {
      const pair = meta.universe[i];
      const ctx = ctxs[i];
      if (!pair || !ctx) continue;
      const mid = parseFloat(ctx.midPx || ctx.markPx || 0);
      if (!Number.isFinite(mid) || mid <= 0) continue;
      // pair.tokens = [baseTokenIndex, quoteTokenIndex]; quote is USDC(0)
      const baseIdx = Array.isArray(pair.tokens) ? pair.tokens[0] : null;
      const tok = baseIdx != null ? tokens[baseIdx] : null;
      const name = tok && tok.name ? String(tok.name).toUpperCase() : null;
      if (name && !(name in map)) map[name] = mid;
    }
    // Stables are $1 by definition for debt/collateral valuation.
    for (const s of DEFAULT_STABLES) map[s] = map[s] || 1;
    _priceCache = { at: Date.now(), map };
    return map;
  } catch (e) {
    console.error('[fundsEngine] spot price fetch failed:', e && e.message ? e.message : e);
    return _priceCache.map; // possibly null → caller degrades gracefully
  }
}

/**
 * Fetch raw account state for one wallet. Each leg fails independently.
 * Returns { spotBalances|null, perp|null, prices|null }.
 */
async function fetchAccountState(wallet) {
  let spotBalances = null;
  let perp = null;
  try {
    const spot = await _post({ type: 'spotClearinghouseState', user: wallet });
    if (spot && Array.isArray(spot.balances)) {
      spotBalances = spot.balances.map((b) => ({
        coin: b.coin,
        total: _num(b.total),
        hold: _num(b.hold),
      }));
    }
  } catch (e) {
    if (!String(e.message || '').includes('429')) {
      console.error(`[fundsEngine] spot fetch failed for ${wallet}:`, e.message);
    }
  }
  try {
    const st = await _post({ type: 'clearinghouseState', user: wallet });
    if (st && st.marginSummary) {
      perp = {
        accountValue: _num(st.marginSummary.accountValue),
        marginUsed: _num(st.marginSummary.totalMarginUsed),
        withdrawable: _num(st.withdrawable),
      };
    }
  } catch (e) {
    if (!String(e.message || '').includes('429')) {
      console.error(`[fundsEngine] perp fetch failed for ${wallet}:`, e.message);
    }
  }
  const prices = await fetchSpotPrices();
  return { spotBalances, perp, prices };
}

// ───────────────────────────── pure compute ─────────────────────────────

/**
 * computeUniversalDeployable({ spotBalances, perp, prices })
 *
 * Pure — fully unit-testable with fixtures. See module header for the
 * contract. Never throws.
 */
function computeUniversalDeployable({ spotBalances, perp, prices }) {
  const spotFetched = Array.isArray(spotBalances);
  const perpFetched = !!perp;
  if (!spotFetched && !perpFetched) {
    return { error: true, account_type: 'unknown' };
  }

  const stables = _stableSet();
  const ltvMap = _ltvMap();
  const priceOf = (coin) => {
    const c = String(coin || '').toUpperCase();
    if (stables.has(c)) return 1;
    const p = prices && Number.isFinite(prices[c]) ? prices[c] : null;
    return p && p > 0 ? p : null;
  };

  // Leg 1 — spot free stables (MAX rule, negatives never counted).
  const spotPool = spotFetched ? computeSpotPool(spotBalances) : null;
  const spot_free = spotFetched ? spotPool.pool || 0 : null;

  // Leg 2 — perp withdrawable.
  const perp_withdrawable = perpFetched
    ? Math.max(_num(perp.withdrawable), 0)
    : null;

  // PM detection: any borrowed (negative) spot balance.
  const negatives = spotFetched
    ? spotBalances.filter((b) => _num(b.total) < 0)
    : [];
  const nonStableCollateral = spotFetched
    ? spotBalances.filter(
        (b) =>
          _num(b.total) > 0 &&
          !stables.has(String(b.coin || '').toUpperCase())
      )
    : [];
  const isPM = negatives.length > 0;

  // Leg 3 — PM borrow headroom.
  let pm = null;
  let pm_borrow_headroom = null;
  if (isPM || nonStableCollateral.length > 0) {
    let debt = 0;
    let debtUnpriced = false;
    for (const b of negatives) {
      const px = priceOf(b.coin);
      if (px == null) { debtUnpriced = true; continue; }
      debt += Math.abs(_num(b.total)) * px;
    }
    let capacity = 0;
    const collateral = [];
    let unknownLtv = [];
    for (const b of nonStableCollateral) {
      const coin = String(b.coin).toUpperCase();
      const px = priceOf(coin);
      const tokens = _num(b.total);
      const value = px != null ? tokens * px : null;
      const ltv = coin in ltvMap ? ltvMap[coin] : null;
      if (value != null && ltv != null) {
        capacity += value * ltv;
      } else if (ltv == null) {
        unknownLtv.push(coin);
      }
      collateral.push({ coin, tokens, price: px, value, ltv });
    }
    const headroom = debtUnpriced ? null : Math.max(0, capacity - debt);

    // Projected liq if full headroom is borrowed — single-asset HYPE only.
    let projected_liq = null;
    let liq_skipped_reason = null;
    const pricedColl = collateral.filter((c) => (c.value || 0) > 1); // ignore dust
    if (headroom != null && pricedColl.length === 1 && pricedColl[0].coin === 'HYPE') {
      const tokens = pricedColl[0].tokens;
      if (tokens > 0) {
        projected_liq = (debt + headroom) / (LIQ_THRESHOLD_HYPE * tokens);
      }
    } else if (pricedColl.length > 1) {
      liq_skipped_reason = 'multi_asset_collateral';
    } else if (pricedColl.length === 1) {
      liq_skipped_reason = 'unsupported_collateral_asset';
    }

    pm = {
      debt,
      debt_unpriced: debtUnpriced,
      capacity,
      collateral,
      unknown_ltv_assets: unknownLtv,
      projected_liq,
      liq_skipped_reason,
    };
    pm_borrow_headroom = isPM || capacity > 0 ? headroom : null;
  }

  // Account type.
  const spotHasAnything =
    spotFetched &&
    spotBalances.some((b) => Math.abs(_num(b.total)) > 1e-9);
  const perpHasAnything =
    perpFetched && (perp.accountValue > 0.01 || perp.marginUsed > 0.01);
  let account_type;
  if (isPM) account_type = 'pm';
  else if (spotHasAnything && perpHasAnything) account_type = 'unified';
  else if (perpHasAnything) account_type = 'perp_only';
  else if (spotHasAnything) account_type = 'spot_only';
  else account_type = 'empty';

  // Total deployable = sum of the fetched, non-null legs. For PM accounts
  // the borrow headroom is genuinely additional deployable capital; the
  // borrowed stables themselves are never counted (negatives clamp to 0).
  let total_deployable = 0;
  if (spot_free != null) total_deployable += spot_free;
  if (perp_withdrawable != null) total_deployable += perp_withdrawable;
  if (pm_borrow_headroom != null) total_deployable += pm_borrow_headroom;

  return {
    error: false,
    account_type,
    spot_free,
    perp_withdrawable,
    pm_borrow_headroom,
    total_deployable,
    pm,
    fetch: {
      spot: spotFetched ? 'ok' : 'error',
      perp: perpFetched ? 'ok' : 'error',
      prices: prices ? 'ok' : 'error',
    },
  };
}

/** Convenience: fetch + compute for one wallet. */
async function getDeployableView(wallet) {
  const raw = await fetchAccountState(wallet);
  return computeUniversalDeployable(raw);
}

// ───────────────────────────── rendering ─────────────────────────────

function _usd(n) {
  if (n == null || !Number.isFinite(n)) return 'fetch error';
  return `$${n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

const TYPE_LABEL = {
  pm: 'Portfolio Margin',
  unified: 'Unified (spot + perp)',
  perp_only: 'Perp-only',
  spot_only: 'Spot-only',
  empty: 'Empty',
  unknown: 'Unknown',
};

/**
 * Compact breakdown lines shared by /funds and the funds-available alert.
 */
function formatDeployableView(view, wallet) {
  const short = wallet
    ? `${wallet.slice(0, 6)}...${wallet.slice(-4)}`
    : '';
  if (!view || view.error) {
    return [`⚠️ ${short} — fetch error (data unavailable, nothing fabricated)`];
  }
  const lines = [];
  if (short) lines.push(`👛 \`${short}\` — ${TYPE_LABEL[view.account_type] || view.account_type}`);
  lines.push(`💵 Spot free stables: ${_usd(view.spot_free)}`);
  lines.push(`📤 Perp withdrawable: ${_usd(view.perp_withdrawable)}`);
  if (view.pm_borrow_headroom != null || view.account_type === 'pm') {
    lines.push(`🏦 PM borrow headroom: ${_usd(view.pm_borrow_headroom)}`);
    if (view.pm && view.pm.debt > 0) {
      lines.push(`   ↳ current debt: ${_usd(view.pm.debt)}`);
    }
    if (view.pm && Number.isFinite(view.pm.projected_liq)) {
      lines.push(
        `   ↳ if you borrow it all → projected liq ≈ $${view.pm.projected_liq.toFixed(2)}`
      );
    } else if (view.pm && view.pm.liq_skipped_reason === 'multi_asset_collateral') {
      lines.push('   ↳ multi-asset collateral — liq projection skipped');
    }
    if (view.pm && view.pm.unknown_ltv_assets && view.pm.unknown_ltv_assets.length > 0) {
      lines.push(
        `   ↳ no LTV data for ${view.pm.unknown_ltv_assets.join(', ')} — counted as $0 borrow power`
      );
    }
  }
  lines.push(`🎯 TOTAL deployable: ${_usd(view.total_deployable)}`);
  return lines;
}

function _resetPriceCacheForTests() {
  _priceCache = { at: 0, map: null };
}

module.exports = {
  computeUniversalDeployable,
  fetchAccountState,
  fetchSpotPrices,
  getDeployableView,
  formatDeployableView,
  DEFAULT_LTV,
  LIQ_THRESHOLD_HYPE,
  TYPE_LABEL,
  _resetPriceCacheForTests,
};
