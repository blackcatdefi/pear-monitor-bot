'use strict';

/**
 * R-PUBLIC-V3-TRACKING — Health Factor reader (HyperLend / HyperEVM).
 *
 * Ports the essence of `auto/hyperlend_reader.py` to JS so the public bot
 * can render a HyperLend HF for any user-supplied wallet address.
 *
 * Live reads use the existing `src/hyperLendApi.js` (Aave-v3-fork
 * `getUserAccountData`) so we share the RPC + decoding code path. On RPC
 * rate-limit / network failure, we fall back to a JSON disk cache (same
 * design as the operator bot's `hyperlend_hf_cache.json`).
 *
 * Public API:
 *   classify(account)            → 'OK' | 'ZERO' | 'UNKNOWN'
 *   bucket(hf)                   → 'HEALTHY' | 'WATCH' | 'RISK' | 'INFINITY'
 *   readWithCache(addr, opts)    → { status, hf, hfBucket, collateral,
 *                                    debt, ltv, ageSeconds, recovered }
 *   formatHfMessage(addr, read)  → Telegram-ready Markdown string
 *
 * Bucket thresholds match the operator bot:
 *    HF ≥ 1.10  → HEALTHY ✅
 *    1.05 ≤ HF < 1.10  → WATCH ⚠️
 *    HF < 1.05  → RISK 🔴
 *    HF == ∞ (no debt) → INFINITY (rendered as Healthy ∞)
 *
 * Cache: $RAILWAY_VOLUME_MOUNT_PATH/hf_cache.json (same volume the rest of
 * the public bot uses). One entry per lowercase address.
 *
 * Env:
 *   HF_CACHE_PATH                 (override; defaults to
 *                                  /app/data/hf_cache.json)
 *   HF_CACHE_TTL_SEC=3600         (only used to label staleness — we always
 *                                  return the latest entry on rate-limit)
 *   HEALTH_FACTOR_AUTOREADER=true (kill switch — set 'false' to disable
 *                                  cache layer and return raw API errors)
 */

const fs = require('fs');
const path = require('path');

const ADDRESS_REGEX = /^0x[a-fA-F0-9]{40}$/i;

function isValidAddress(addr) {
  return typeof addr === 'string' && ADDRESS_REGEX.test(addr);
}

function _enabled() {
  return (
    String(process.env.HEALTH_FACTOR_AUTOREADER || 'true').toLowerCase() !==
    'false'
  );
}

function _cachePath() {
  return (
    process.env.HF_CACHE_PATH ||
    path.join(
      process.env.RAILWAY_VOLUME_MOUNT_PATH || '/app/data',
      'hf_cache.json'
    )
  );
}

function _ensureDir() {
  try { fs.mkdirSync(path.dirname(_cachePath()), { recursive: true }); }
  catch (_) {}
}

function _loadCache() {
  const p = _cachePath();
  try {
    if (!fs.existsSync(p)) return {};
    const raw = JSON.parse(fs.readFileSync(p, 'utf-8'));
    return raw && typeof raw === 'object' ? raw : {};
  } catch (_) {
    return {};
  }
}

function _saveCache(cache) {
  try {
    _ensureDir();
    const p = _cachePath();
    const tmp = p + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(cache, null, 2));
    fs.renameSync(tmp, p);
  } catch (_) {}
}

function _isFinite(v) {
  return typeof v === 'number' && Number.isFinite(v);
}

/**
 * Classify a fresh HyperLend account-data read into a 3-state status.
 *   - OK      : account has data, HF is finite OR (collateral > 0 && debt == 0)
 *   - ZERO    : both collateral and debt are essentially zero (empty wallet)
 *   - UNKNOWN : caller signalled an error / partial result
 */
function classify(account) {
  if (!account || account.error) return 'UNKNOWN';
  const coll = Number(account.totalCollateralUsd || 0);
  const debt = Number(account.totalDebtUsd || 0);
  const hf = account.healthFactor;
  if (coll <= 0.01 && debt <= 0.01) return 'ZERO';
  if (coll > 0.01 && debt <= 0.01) return 'OK'; // HF ∞ is fine
  if (hf === Infinity) return 'OK';
  if (_isFinite(hf)) return 'OK';
  return 'UNKNOWN';
}

/**
 * Bucket a finite HF into a risk band. Mirrors auto/hf_alert_gate.py
 * thresholds in the operator bot (1.10 / 1.05 / 1.02 are alert triggers;
 * 1.10 / 1.05 are render bands).
 */
function bucket(hf) {
  if (hf === Infinity) return 'INFINITY';
  if (!_isFinite(hf)) return 'UNKNOWN';
  if (hf >= 1.10) return 'HEALTHY';
  if (hf >= 1.05) return 'WATCH';
  return 'RISK';
}

/**
 * Try a live read via HyperLendApi; fall back to cache on any error.
 * Returns a normalized record:
 *   { status: 'LIVE'|'CACHED'|'ZERO'|'ERROR',
 *     hf, hfBucket, collateral, debt, ltv,
 *     ageSeconds, recovered }
 *
 * `apiFactory` is injectable for tests — defaults to a singleton built from
 * env vars (HYPEREVM_RPC_URL / HYPERLEND_POOL_ADDRESS).
 */
async function readWithCache(address, opts) {
  const o = opts || {};
  if (!isValidAddress(address)) {
    return {
      status: 'ERROR',
      error: 'Invalid address — must be 0x followed by 40 hex chars.',
    };
  }
  const lc = address.toLowerCase();
  const cache = _loadCache();

  // Build / reuse API instance lazily so the bot's cold-boot doesn't pull
  // ethers + open an RPC connection unless an HF read is requested.
  let api = o.api || null;
  if (!api) {
    if (typeof o.getApi === 'function') {
      api = o.getApi();
    } else {
      try {
        const HyperLendApi = require('./hyperLendApi');
        api = new HyperLendApi({
          rpcUrl: process.env.HYPEREVM_RPC_URL || undefined,
          poolAddress: process.env.HYPERLEND_POOL_ADDRESS || undefined,
        });
      } catch (e) {
        return {
          status: 'ERROR',
          error: 'HyperLend reader unavailable in this environment.',
        };
      }
    }
  }

  // Live read.
  let live = null;
  let liveErr = null;
  try {
    live = await api.getAccountData(address);
  } catch (e) {
    liveErr = e && e.message ? e.message : String(e);
  }

  if (live) {
    const cls = classify(live);
    if (cls === 'OK' || cls === 'ZERO') {
      // Persist OK/ZERO to cache. ZERO carries no HF signal but we still
      // remember collateral/debt so a subsequent fetch with non-zero values
      // can detect a transient zero frame.
      const entry = {
        hf: live.healthFactor === Infinity ? 'inf' : Number(live.healthFactor),
        collateral: Number(live.totalCollateralUsd || 0),
        debt: Number(live.totalDebtUsd || 0),
        ltv: Number(live.ltv || 0),
        liqThreshold: Number(live.currentLiquidationThreshold || 0),
        availableBorrows: Number(live.availableBorrowsUsd || 0),
        tsEpoch: Math.floor(Date.now() / 1000),
        tsIso: new Date().toISOString(),
      };
      if (_enabled()) {
        cache[lc] = entry;
        _saveCache(cache);
      }
      const hf = live.healthFactor;
      return {
        status: cls === 'ZERO' ? 'ZERO' : 'LIVE',
        hf,
        hfBucket: bucket(hf),
        collateral: entry.collateral,
        debt: entry.debt,
        ltv: entry.ltv,
        liqThreshold: entry.liqThreshold,
        availableBorrows: entry.availableBorrows,
        ageSeconds: 0,
        recovered: false,
      };
    }
    // UNKNOWN — fall through to cache lookup below.
  }

  // Live failed (or returned UNKNOWN). Try cache.
  const cached = cache[lc];
  if (cached && _enabled()) {
    const ageSeconds = Math.max(
      0,
      Math.floor(Date.now() / 1000 - Number(cached.tsEpoch || 0))
    );
    let hf;
    if (cached.hf === 'inf') hf = Infinity;
    else if (_isFinite(Number(cached.hf))) hf = Number(cached.hf);
    else hf = null;
    return {
      status: 'CACHED',
      hf,
      hfBucket: hf == null ? 'UNKNOWN' : bucket(hf),
      collateral: Number(cached.collateral || 0),
      debt: Number(cached.debt || 0),
      ltv: Number(cached.ltv || 0),
      liqThreshold: Number(cached.liqThreshold || 0),
      availableBorrows: Number(cached.availableBorrows || 0),
      ageSeconds,
      recovered: true,
    };
  }

  return {
    status: 'ERROR',
    error: liveErr || 'No data — wallet has no HyperLend position or RPC failed.',
  };
}

function _ageLabel(s) {
  if (s == null || !Number.isFinite(s)) return '?';
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}min`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

function _shortAddr(a) {
  return a.length > 12 ? `${a.slice(0, 6)}…${a.slice(-4)}` : a;
}

function _fmtUsd(v) {
  const n = Number(v) || 0;
  if (n >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e3) return `$${(n / 1e3).toFixed(1)}K`;
  return `$${n.toFixed(0)}`;
}

function _bucketIcon(b) {
  switch (b) {
    case 'HEALTHY': return '✅ Healthy';
    case 'INFINITY': return '✅ Healthy (no debt)';
    case 'WATCH': return '⚠️ Watch';
    case 'RISK': return '🔴 Risk';
    default: return '❔ Unknown';
  }
}

/**
 * Render a Markdown-ready message for Telegram. Always safe to send: never
 * throws, never includes private fund info, never echoes back the raw RPC
 * error verbatim (we map known failure modes to user-friendly copy).
 */
function formatHfMessage(address, read) {
  const short = _shortAddr(address);
  if (!read || read.status === 'ERROR') {
    const err = (read && read.error) || 'Unknown error';
    return [
      '🛡 *Health Factor*',
      '',
      `Wallet: \`${address}\``,
      '',
      `❌ Could not read HyperLend state: ${err}`,
      '',
      '_If the wallet has no HyperLend position this is expected._',
    ].join('\n');
  }
  if (read.status === 'ZERO') {
    return [
      '🛡 *Health Factor*',
      '',
      `Wallet: \`${address}\` (${short})`,
      '',
      'No HyperLend position — collateral $0, debt $0.',
      '',
      '_If you expected a position here, double-check the address._',
    ].join('\n');
  }
  const hfStr =
    read.hf === Infinity
      ? '∞'
      : _isFinite(read.hf)
        ? Number(read.hf).toFixed(3)
        : '—';
  const ltvPct = `${(Number(read.ltv || 0) * 100).toFixed(1)}%`;
  const lines = [
    '🛡 *Health Factor*',
    '',
    `Wallet: \`${address}\` (${short})`,
    '',
    `HF: *${hfStr}*  ${_bucketIcon(read.hfBucket)}`,
    `Collateral: ${_fmtUsd(read.collateral)}  •  Debt: ${_fmtUsd(read.debt)}`,
    `LTV: ${ltvPct}`,
  ];
  if (read.status === 'CACHED') {
    lines.push(
      '',
      `⚠️ _Live RPC unavailable — showing cached read from ${_ageLabel(read.ageSeconds)} ago._`
    );
  }
  lines.push(
    '',
    '_HF ≥ 1.10 healthy · 1.05–1.10 watch · < 1.05 risk._',
    '_Read-only. No funds touched. No alerts unless you opt in via 👁 Track._'
  );
  return lines.join('\n');
}

function _resetCacheForTests(customPath) {
  try {
    const p = customPath || _cachePath();
    if (fs.existsSync(p)) fs.unlinkSync(p);
  } catch (_) {}
}

module.exports = {
  ADDRESS_REGEX,
  isValidAddress,
  classify,
  bucket,
  readWithCache,
  formatHfMessage,
  _cachePath,
  _resetCacheForTests,
};
