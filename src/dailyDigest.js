'use strict';

/**
 * R-AUTOCOPY — Daily digest scheduler.
 *
 * Sends a once-a-day summary to every user that has alertsConfig.daily_summary
 * enabled, at the user's local 9 AM (configurable via DAILY_DIGEST_DEFAULT_HOUR).
 *
 * Cadence: a single setInterval poll every 5 min checks "is it 9 AM in this
 * user's TZ AND have we not sent today?" and sends if so. We persist the
 * last-sent timestamp per user to avoid double-sends across restarts.
 */

const fs = require('fs');
const path = require('path');

const wt = require('./walletTracker');
const tzMgr = require('./timezoneManager');
const alertsConfig = require('./alertsConfig');

const DEFAULT_HOUR = parseInt(process.env.DAILY_DIGEST_DEFAULT_HOUR || '9', 10);
const POLL_INTERVAL_MS = 5 * 60 * 1000; // 5 min

function _resolveDbPath() {
  return (
    process.env.DAILY_DIGEST_DB_PATH ||
    path.join(
      process.env.RAILWAY_VOLUME_MOUNT_PATH || '/app/data',
      'daily_digest.json'
    )
  );
}

const DB_PATH = _resolveDbPath();
let _store = null;
let _timer = null;
let _notify = null;

function _ensureDir() {
  try { fs.mkdirSync(path.dirname(DB_PATH), { recursive: true }); }
  catch (_) {}
}

function _load() {
  if (_store !== null) return _store;
  try {
    if (fs.existsSync(DB_PATH)) {
      const raw = JSON.parse(fs.readFileSync(DB_PATH, 'utf-8'));
      _store = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
    } else {
      _store = {};
    }
  } catch (e) {
    console.error('[dailyDigest] load failed:', e && e.message ? e.message : e);
    _store = {};
  }
  return _store;
}

function _save() {
  try {
    _ensureDir();
    const tmp = DB_PATH + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(_load(), null, 2));
    fs.renameSync(tmp, DB_PATH);
  } catch (e) {
    console.error('[dailyDigest] save failed:', e && e.message ? e.message : e);
  }
}

/**
 * Returns the local hour (0-23) for the given userId, using their TZ.
 * Falls back to UTC if TZ lookup fails.
 */
function _localHour(userId, now) {
  const tz = tzMgr.getUserTz(userId);
  const date = now || new Date();
  try {
    const fmt = new Intl.DateTimeFormat('en-US', {
      timeZone: tz,
      hour: '2-digit',
      hour12: false,
    });
    const parts = fmt.formatToParts(date);
    const h = parts.find((p) => p.type === 'hour');
    if (h) {
      let n = parseInt(h.value, 10);
      if (n === 24) n = 0;
      return n;
    }
  } catch (_) {}
  return date.getUTCHours();
}

function _localYmd(userId, now) {
  const tz = tzMgr.getUserTz(userId);
  const date = now || new Date();
  try {
    const fmt = new Intl.DateTimeFormat('en-CA', {
      timeZone: tz,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    });
    return fmt.format(date); // YYYY-MM-DD
  } catch (_) {
    return date.toISOString().slice(0, 10);
  }
}

function shouldSend(userId, now) {
  if (!alertsConfig.isAllowed(userId, 'daily_summary')) return false;
  const h = _localHour(userId, now);
  if (h !== DEFAULT_HOUR) return false;
  const s = _load();
  const ymd = _localYmd(userId, now);
  const lastYmd = s[String(userId)] && s[String(userId)].last_ymd;
  return lastYmd !== ymd;
}

function markSent(userId, now) {
  const s = _load();
  s[String(userId)] = { last_ymd: _localYmd(userId, now), ts: Date.now() };
  _save();
}

function buildDigest(userId) {
  const wallets = wt.getUserWallets(userId);
  const lines = [
    '📅 *Daily digest*',
    '',
    `🌎 ${tzMgr.formatLocalTime(userId)}`,
    '',
    `📡 Tracked wallets: ${wallets.length}`,
  ];
  if (wallets.length > 0) {
    lines.push('');
    for (const w of wallets.slice(0, 5)) {
      const lbl = w.label ? ` — ${w.label}` : '';
      const a = String(w.address);
      lines.push(`  • \`${a.slice(0, 6)}...${a.slice(-4)}\`${lbl}`);
    }
    if (wallets.length > 5) lines.push(`  _... and ${wallets.length - 5} more_`);
  }
  lines.push('');
  lines.push(
    '_To pick which alerts to receive: /alerts_config_'
  );
  lines.push('_To change the digest time: /timezone_');
  return lines.join('\n');
}

async function pollOnce(now) {
  if (typeof _notify !== 'function') return 0;
  const cfg = alertsConfig._load ? null : null; // alertsConfig has no public iterator
  // Iterate users who currently have wallets tracked OR have an alerts config record;
  // either is good enough proxy for "active user".
  const wallets = wt.getAllUniqueAddresses();
  const userSet = new Set();
  for (const a of wallets) {
    for (const sub of a.subscribers || []) userSet.add(sub.userId);
  }
  // Also include users from the digest store (in case wallets were removed
  // but they still want digests).
  const s = _load();
  for (const uid of Object.keys(s)) userSet.add(uid);
  let sent = 0;
  for (const uid of userSet) {
    try {
      const userIdNum = parseInt(uid, 10);
      if (!Number.isFinite(userIdNum)) continue;
      if (!shouldSend(userIdNum, now)) continue;
      const body = buildDigest(userIdNum);
      await _notify(userIdNum, body, { parse_mode: 'Markdown' });
      markSent(userIdNum, now);
      sent += 1;
    } catch (e) {
      console.error('[dailyDigest] send failed for', uid, e && e.message ? e.message : e);
    }
  }
  return sent;
}

function startSchedule({ notify }) {
  _notify = notify;
  if (_timer) clearInterval(_timer);
  _timer = setInterval(() => {
    pollOnce().catch((e) =>
      console.error('[dailyDigest] poll failed:', e && e.message ? e.message : e)
    );
  }, POLL_INTERVAL_MS);
  if (typeof _timer.unref === 'function') _timer.unref();
  console.log(`[dailyDigest] started, hour=${DEFAULT_HOUR} local, poll=${POLL_INTERVAL_MS / 60000}min`);
  return _timer;
}

function stopSchedule() {
  if (_timer) { clearInterval(_timer); _timer = null; }
}

function _resetForTests(customPath) {
  _store = {};
  try {
    const p = customPath || DB_PATH;
    if (fs.existsSync(p)) fs.unlinkSync(p);
  } catch (_) {}
}

module.exports = {
  DEFAULT_HOUR,
  POLL_INTERVAL_MS,
  DB_PATH,
  _localHour,
  _localYmd,
  shouldSend,
  markSent,
  buildDigest,
  pollOnce,
  startSchedule,
  stopSchedule,
  _resetForTests,
};
