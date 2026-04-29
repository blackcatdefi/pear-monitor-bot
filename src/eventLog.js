'use strict';

/**
 * Round v2 — Persistent event log (closes, opens, compounds).
 *
 * All historical events flow through here so /history /pnl /export commands
 * can query them. Stored as JSONL on the same persistent volume as the
 * existing state files (RAILWAY_VOLUME_MOUNT_PATH).
 *
 * No external sqlite dep — keep the bot's deploy footprint tiny. JSONL is
 * append-only friendly and easy to scan.
 */

const fs = require('fs');
const path = require('path');

const VOLUME = process.env.RAILWAY_VOLUME_MOUNT_PATH;
const DATA_DIR = VOLUME
  ? path.join(VOLUME, 'data')
  : path.join(__dirname, '..', 'data');
const LOG_FILE = path.join(DATA_DIR, 'events.jsonl');

function _ensure() {
  if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
}

function recordEvent(event) {
  try {
    _ensure();
    const row = {
      timestamp: new Date().toISOString(),
      ...event,
    };
    fs.appendFileSync(LOG_FILE, JSON.stringify(row) + '\n');
  } catch (e) {
    console.error('[eventLog] write failed:', e && e.message ? e.message : e);
  }
}

function _readAll() {
  if (!fs.existsSync(LOG_FILE)) return [];
  const out = [];
  try {
    const raw = fs.readFileSync(LOG_FILE, 'utf8');
    for (const line of raw.split('\n')) {
      const ln = line.trim();
      if (!ln) continue;
      try {
        out.push(JSON.parse(ln));
      } catch (_) {
        // skip corrupt lines
      }
    }
  } catch (e) {
    console.error('[eventLog] read failed:', e && e.message ? e.message : e);
  }
  return out;
}

function recentCloses(limit = 10, opts = {}) {
  const all = _readAll();
  let closes = all.filter(
    (e) => e.type === 'CLOSE' || e.type === 'FULL_CLOSE'
  );
  if (opts.wallet) {
    const w = String(opts.wallet).toLowerCase();
    closes = closes.filter((e) => (e.wallet || '').toLowerCase() === w);
  }
  closes.sort((a, b) => (b.timestamp || '').localeCompare(a.timestamp || ''));
  return closes.slice(0, limit);
}

function startOfPeriodMs(period) {
  const now = new Date();
  const p = String(period || 'today').toLowerCase();
  if (p === 'today') {
    return Date.UTC(
      now.getUTCFullYear(),
      now.getUTCMonth(),
      now.getUTCDate()
    );
  }
  if (p === 'week') {
    return now.getTime() - 7 * 24 * 60 * 60 * 1000;
  }
  if (p === 'month') {
    return now.getTime() - 30 * 24 * 60 * 60 * 1000;
  }
  if (p === 'ytd') {
    return Date.UTC(now.getUTCFullYear(), 0, 1);
  }
  if (p === 'all') return 0;
  return Date.UTC(
    now.getUTCFullYear(),
    now.getUTCMonth(),
    now.getUTCDate()
  );
}

function closesByPeriod(period) {
  const since = startOfPeriodMs(period);
  return _readAll().filter((e) => {
    if (e.type !== 'CLOSE' && e.type !== 'FULL_CLOSE') return false;
    const t = Date.parse(e.timestamp || '');
    return Number.isFinite(t) && t >= since;
  });
}

function closesSince(sinceMs) {
  return _readAll().filter((e) => {
    if (e.type !== 'CLOSE' && e.type !== 'FULL_CLOSE') return false;
    const t = Date.parse(e.timestamp || '');
    return Number.isFinite(t) && t >= sinceMs;
  });
}

function summarize(closes) {
  const wins = closes.filter((c) => (c.pnl || 0) > 0);
  const losses = closes.filter((c) => (c.pnl || 0) < 0);
  const totalPnl = closes.reduce((s, c) => s + (c.pnl || 0), 0);
  const totalNotional = closes.reduce(
    (s, c) => s + (c.entryNotional || 0),
    0
  );
  const totalFees = closes.reduce((s, c) => s + (c.fees || 0), 0);
  let best = null;
  let worst = null;
  for (const c of closes) {
    if (best === null || (c.pnl || 0) > (best.pnl || 0)) best = c;
    if (worst === null || (c.pnl || 0) < (worst.pnl || 0)) worst = c;
  }
  return {
    count: closes.length,
    wins: wins.length,
    losses: losses.length,
    win_rate_pct:
      closes.length > 0 ? (wins.length / closes.length) * 100 : 0,
    total_pnl: totalPnl,
    total_notional: totalNotional,
    total_fees: totalFees,
    best,
    worst,
  };
}

module.exports = {
  recordEvent,
  recentCloses,
  closesByPeriod,
  closesSince,
  summarize,
  startOfPeriodMs,
  LOG_FILE,
};
