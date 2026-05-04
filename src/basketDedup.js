'use strict';

/**
 * R(v4) — Basket Dedup
 *
 * Prevents duplicate "NUEVA BASKET ABIERTA" alerts.
 *
 * A basket is identified by SHA-256 hash of:
 *   wallet (lowercase) + sorted([(coin, side, entryPx_rounded_6dp)])
 *
 * Persistence: JSON file at $RAILWAY_VOLUME_MOUNT_PATH/data/basket_dedup.json
 * (or local data/basket_dedup.json if no Railway volume). Survives bot
 * restarts — that's the entire point. TTL default 7 days.
 *
 * Root cause this module fixes (apr-30 2026):
 *   `extensions.js` keeps `lastSeenSnapshots` as an in-memory Map. After a
 *   bot restart (which happened at 15:50 UTC and again ~19:00 UTC on
 *   apr-30 due to R(v3)+R21 deploys) the snapshot resets to []. The next
 *   poll then classifies all 5 currently-active positions as "new" and
 *   fires `BASKET_OPEN`. The in-memory `shouldSendAlert` 60s window also
 *   resets, so it lets the alert through. BCD got the same v6 basket
 *   alerted twice in 3h 15min.
 *
 * Fix shape: gate `BASKET_OPEN` dispatch on a hash that survives restarts.
 *
 * Storage shape (basket_dedup.json):
 *   {
 *     "<sha256_hex>": {
 *       "wallet": "0x...",
 *       "sentAt": <ms>,
 *       "ttlDays": 7,
 *       "positions": [{coin, side, entryPx}, ...]
 *     },
 *     ...
 *   }
 *
 * NOTE: Uses JSON-file persistence (not SQLite) to match the existing
 * `store.js` pattern in this repo and avoid a node-gyp native dependency
 * on the alpine Docker image.
 */

const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

// R-PUBLIC-BASKET-SPAM-NUCLEAR — forensic counter for the SHA-256 dedup hits.
// Lazy-loaded with a no-op fallback so basketDedup.test.js doesn't need to
// stub healthServer (pure unit tests stay free of side effects).
const _healthCounters = (() => {
  try {
    return require('./healthServer');
  } catch (_) {
    return { recordEventDeduplicated: () => {} };
  }
})();

const VOLUME = process.env.RAILWAY_VOLUME_MOUNT_PATH;
const DATA_DIR = VOLUME
  ? path.join(VOLUME, 'data')
  : path.join(__dirname, '..', 'data');

const DEFAULT_DB_PATH = path.join(DATA_DIR, 'basket_dedup.json');
const DB_PATH = process.env.DEDUP_DB_PATH || DEFAULT_DB_PATH;

const TTL_DAYS = parseInt(process.env.BASKET_DEDUP_TTL_DAYS || '7', 10);
const ENABLED =
  (process.env.BASKET_DEDUP_ENABLED || 'true').toLowerCase() !== 'false';

const CLEANUP_INTERVAL_MS = 6 * 60 * 60 * 1000; // 6h

let _cleanupTimer = null;
let _initialized = false;

function _ensureDir() {
  const dir = path.dirname(DB_PATH);
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
}

function _readDb() {
  _ensureDir();
  if (!fs.existsSync(DB_PATH)) return {};
  try {
    const raw = fs.readFileSync(DB_PATH, 'utf8');
    if (!raw.trim()) return {};
    return JSON.parse(raw);
  } catch (e) {
    console.error(
      `[basketDedup] failed to read ${DB_PATH}, starting fresh:`,
      e && e.message ? e.message : e
    );
    return {};
  }
}

function _writeDb(db) {
  _ensureDir();
  // Atomic write: write to .tmp then rename (avoids torn reads if process
  // is killed mid-write). On Linux fs.renameSync is atomic.
  const tmp = `${DB_PATH}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(db, null, 2));
  fs.renameSync(tmp, DB_PATH);
}

function _initOnce() {
  if (_initialized) return;
  _initialized = true;
  _ensureDir();
  if (ENABLED) {
    console.log(
      `[basketDedup] initialized at ${DB_PATH} (TTL=${TTL_DAYS}d, ENABLED=true)`
    );
    // Run cleanup every 6h. Use unref so it doesn't block process exit.
    if (!_cleanupTimer) {
      _cleanupTimer = setInterval(() => {
        try {
          cleanupExpired();
        } catch (e) {
          console.error(
            '[basketDedup] cleanup interval failed:',
            e && e.message ? e.message : e
          );
        }
      }, CLEANUP_INTERVAL_MS);
      if (typeof _cleanupTimer.unref === 'function') _cleanupTimer.unref();
    }
  } else {
    console.log('[basketDedup] DISABLED via BASKET_DEDUP_ENABLED=false');
  }
}

/**
 * Compute deterministic SHA-256 hash for a basket.
 *
 * Order-independent: positions are sorted by `coin:side:entryPx` before
 * hashing. Wallet is lowercased. entryPx is rounded to 6dp to absorb
 * floating-point jitter from Hyperliquid API responses (which sometimes
 * returns 0.157570 vs 0.15757 for the same trade).
 *
 * @param {string} wallet
 * @param {Array<{coin, side, entryPx}>} positions
 * @returns {string} SHA-256 hex digest
 */
function computeBasketHash(wallet, positions) {
  if (!wallet) throw new Error('computeBasketHash: wallet required');
  if (!Array.isArray(positions) || positions.length === 0) {
    throw new Error('computeBasketHash: positions must be non-empty array');
  }

  const sorted = positions
    .map((p) => {
      const px = Number(p.entryPx ?? p.entryPrice);
      const pxStr = Number.isFinite(px) ? px.toFixed(6) : '0.000000';
      const side = String(p.side || '').toUpperCase();
      const coin = String(p.coin || '').toUpperCase();
      return `${coin}:${side}:${pxStr}`;
    })
    .sort()
    .join('|');

  const payload = `${String(wallet).toLowerCase()}|${sorted}`;
  return crypto.createHash('sha256').update(payload).digest('hex');
}

/**
 * Check if this exact basket was already alerted within TTL window.
 *
 * @returns {{wasAlerted: boolean, alertedAt: number|null, hash: string|null}}
 */
function checkAlreadyAlerted(wallet, positions) {
  if (!ENABLED) {
    return { wasAlerted: false, alertedAt: null, hash: null };
  }
  _initOnce();

  let hash;
  try {
    hash = computeBasketHash(wallet, positions);
  } catch (e) {
    console.error(
      '[basketDedup] checkAlreadyAlerted hash failed:',
      e && e.message ? e.message : e
    );
    return { wasAlerted: false, alertedAt: null, hash: null };
  }

  const db = _readDb();
  const entry = db[hash];
  if (!entry) {
    return { wasAlerted: false, alertedAt: null, hash };
  }

  const ttlMs = (Number(entry.ttlDays) || TTL_DAYS) * 24 * 60 * 60 * 1000;
  const expiresAt = Number(entry.sentAt) + ttlMs;
  if (Date.now() > expiresAt) {
    return { wasAlerted: false, alertedAt: null, hash };
  }

  // R-PUBLIC-BASKET-SPAM-NUCLEAR — Bug D forensic. If this counter never
  // moves while BCD is repeatedly opening identical baskets (e.g. /track
  // a wallet and re-running monitor), the dedup persistence is broken
  // (volume not mounted, write failed silently, etc.).
  try {
    if (typeof _healthCounters.recordEventDeduplicated === 'function') {
      _healthCounters.recordEventDeduplicated(
        `basketDedup.hit:${String(wallet).slice(0, 10)}:${hash.slice(0, 8)}`
      );
    }
  } catch (_) {
    /* never let telemetry break the gate */
  }

  return { wasAlerted: true, alertedAt: Number(entry.sentAt), hash };
}

/**
 * Record that we just alerted for this basket. Idempotent — overwrites
 * any prior entry for the same hash with a fresh timestamp.
 */
function markAsAlerted(wallet, positions) {
  if (!ENABLED) return null;
  _initOnce();

  let hash;
  try {
    hash = computeBasketHash(wallet, positions);
  } catch (e) {
    console.error(
      '[basketDedup] markAsAlerted hash failed:',
      e && e.message ? e.message : e
    );
    return null;
  }

  const db = _readDb();
  // Normalize positions for storage: only the fields that matter, lowercased.
  const normalized = positions.map((p) => ({
    coin: String(p.coin || '').toUpperCase(),
    side: String(p.side || '').toUpperCase(),
    entryPx: Number(p.entryPx ?? p.entryPrice) || 0,
  }));
  db[hash] = {
    wallet: String(wallet).toLowerCase(),
    sentAt: Date.now(),
    ttlDays: TTL_DAYS,
    positions: normalized,
  };
  _writeDb(db);

  console.log(
    `[basketDedup] marked alerted hash=${hash.slice(0, 12)}... wallet=${wallet}`
  );
  return hash;
}

/**
 * Remove entries older than TTL. Returns count cleaned.
 */
function cleanupExpired() {
  if (!ENABLED) return 0;
  _initOnce();

  const db = _readDb();
  const now = Date.now();
  let cleaned = 0;
  for (const [hash, entry] of Object.entries(db)) {
    const ttlMs = (Number(entry.ttlDays) || TTL_DAYS) * 24 * 60 * 60 * 1000;
    const expiresAt = Number(entry.sentAt) + ttlMs;
    if (now > expiresAt) {
      delete db[hash];
      cleaned += 1;
    }
  }
  if (cleaned > 0) {
    _writeDb(db);
    console.log(`[basketDedup] cleaned ${cleaned} expired entries`);
  }
  return cleaned;
}

/**
 * Get all current dedup entries (for /dedup_status). Returned newest-first.
 *
 * @returns {Array<{hash, wallet, sentAt, ttlDays, positions}>}
 */
function getAllEntries() {
  if (!ENABLED) return [];
  _initOnce();

  const db = _readDb();
  const out = Object.entries(db).map(([hash, entry]) => ({
    hash,
    wallet: entry.wallet,
    sentAt: Number(entry.sentAt) || 0,
    ttlDays: Number(entry.ttlDays) || TTL_DAYS,
    positions: Array.isArray(entry.positions) ? entry.positions : [],
  }));
  out.sort((a, b) => b.sentAt - a.sentAt);
  return out;
}

/**
 * Test-only: wipe DB. Used by tests/basketDedup.test.js.
 */
function _resetForTests() {
  try {
    if (fs.existsSync(DB_PATH)) fs.unlinkSync(DB_PATH);
  } catch (_) {
    /* ignore */
  }
  if (_cleanupTimer) {
    clearInterval(_cleanupTimer);
    _cleanupTimer = null;
  }
  _initialized = false;
}

/**
 * Test-only: backdate an existing entry's sentAt by `daysAgo` days.
 * Used to validate TTL expiry without sleeping.
 */
function _backdateForTests(hash, daysAgo) {
  const db = _readDb();
  if (!db[hash]) return false;
  db[hash].sentAt = Date.now() - daysAgo * 24 * 60 * 60 * 1000;
  _writeDb(db);
  return true;
}

module.exports = {
  computeBasketHash,
  checkAlreadyAlerted,
  markAsAlerted,
  cleanupExpired,
  getAllEntries,
  TTL_DAYS,
  ENABLED,
  DB_PATH,
  // test-only
  _resetForTests,
  _backdateForTests,
};
