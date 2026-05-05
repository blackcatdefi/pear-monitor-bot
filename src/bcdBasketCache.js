'use strict';

/**
 * R-PUBLIC-SIMPLIFY — Cached active-basket fetcher for the BCD trading wallet.
 *
 * Pulls HyperLiquid positions for the canonical fund wallet and caches them
 * for BCD_BASKET_CACHE_TTL_MS (default 30 seconds). Every /start call hits
 * this module to build the hero "COPY MY ACTIVE BASKET" Pear URL — keeping
 * latency below ~150 ms for the cached path.
 *
 * Returns an array shaped for `pearUrlBuilder.buildPearCopyUrl()`:
 *   [{ coin, side, notional }, ...]
 *
 * Failure mode: returns the previous cache (stale-tolerant) when the
 * Hyperliquid API errors. /start NEVER blocks waiting on the network — it
 * shows the fallback Pear hero URL instead.
 */

const HyperliquidApi = require('./hyperliquidApi');

const BCD_WALLET = (
  process.env.BCD_WALLET ||
  '0xc7ae23316b47f7e75f455f53ad37873a18351505'
).toLowerCase();

const TTL_MS = parseInt(process.env.BCD_BASKET_CACHE_TTL_MS || '30000', 10);
const FETCH_TIMEOUT_MS = parseInt(
  process.env.BCD_BASKET_FETCH_TIMEOUT_MS || '4000',
  10
);

let _cache = { positions: [], at: 0, error: null };
let _api = null;

function _getApi() {
  if (!_api) _api = new HyperliquidApi();
  return _api;
}

function _withTimeout(promise, ms) {
  return Promise.race([
    promise,
    new Promise((_, reject) =>
      setTimeout(
        () => reject(new Error(`bcdBasketCache fetch timeout ${ms}ms`)),
        ms
      )
    ),
  ]);
}

function _shape(positions) {
  if (!Array.isArray(positions)) return [];
  return positions
    .map((p) => {
      const size = Math.abs(Number(p.size) || 0);
      // Prefer markPrice when available; fall back to entryPrice; final
      // fallback 0 so the entry is filtered out.
      const px = Number(p.markPrice) || Number(p.entryPrice) || 0;
      const notional = size * px;
      return {
        coin: String(p.coin || '').toUpperCase(),
        side: p.side || (Number(p.size) < 0 ? 'SHORT' : 'LONG'),
        notional,
      };
    })
    .filter((p) => p.coin && p.notional > 0);
}

async function getActiveBasket(opts) {
  const o = opts || {};
  const force = Boolean(o.force);
  const now = Date.now();
  if (!force && now - _cache.at < TTL_MS && _cache.positions.length > 0) {
    return _cache.positions;
  }
  try {
    const states = await _withTimeout(
      _getApi().getAllClearinghouseStates(BCD_WALLET),
      FETCH_TIMEOUT_MS
    );
    const positions = _getApi().aggregatePositions(states);
    const shaped = _shape(positions);
    _cache = { positions: shaped, at: now, error: null };
    return shaped;
  } catch (e) {
    const msg = e && e.message ? e.message : String(e);
    console.error('[bcdBasketCache] fetch failed:', msg);
    _cache = { ..._cache, at: now, error: msg };
    return _cache.positions || [];
  }
}

function getStats() {
  return {
    walletId: BCD_WALLET,
    cachedAt: _cache.at,
    cacheAgeMs: _cache.at ? Date.now() - _cache.at : null,
    cachedCount: _cache.positions.length,
    lastError: _cache.error,
    ttlMs: TTL_MS,
  };
}

function _setCacheForTests(positions) {
  _cache = { positions: positions || [], at: Date.now(), error: null };
}

function _resetForTests() {
  _cache = { positions: [], at: 0, error: null };
  _api = null;
}

module.exports = {
  BCD_WALLET,
  TTL_MS,
  FETCH_TIMEOUT_MS,
  getActiveBasket,
  getStats,
  _shape,
  _setCacheForTests,
  _resetForTests,
};
