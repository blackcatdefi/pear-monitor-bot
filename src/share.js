'use strict';

/**
 * R-AUTOCOPY — Referral / share + premium upgrade.
 *
 * Each user has a unique deep-link of shape `t.me/<bot_username>?start=ref_<userId>`.
 * When a new user types /start with this `ref_` payload, share.recordReferral
 * is called → referrer gets +1 credit. Once they hit PREMIUM_REFERRAL_THRESHOLD
 * (default 3), share.isPremium(userId) returns true and walletTracker bumps
 * their slot cap to PREMIUM_TRACK_SLOTS (default 25).
 *
 * Storage: JSON
 *   {
 *     "{referrerUserId}": {
 *       count: 7,
 *       referees: ["12345", "67890"],
 *       premium: true,
 *       _updated_at: 173...
 *     }
 *   }
 */

const fs = require('fs');
const path = require('path');

const PREMIUM_THRESHOLD = parseInt(process.env.PREMIUM_REFERRAL_THRESHOLD || '3', 10);
const PREMIUM_SLOTS = parseInt(process.env.PREMIUM_TRACK_SLOTS || '25', 10);
const DEFAULT_SLOTS = parseInt(process.env.DEFAULT_TRACK_SLOTS || '10', 10);

function _resolveDbPath() {
  return (
    process.env.SHARE_DB_PATH ||
    path.join(
      process.env.RAILWAY_VOLUME_MOUNT_PATH || '/app/data',
      'referrals.json'
    )
  );
}

const DB_PATH = _resolveDbPath();
let _store = null;

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
    console.error('[share] load failed:', e && e.message ? e.message : e);
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
    console.error('[share] save failed:', e && e.message ? e.message : e);
  }
}

/**
 * Build a referral deep-link for a given user.
 *   buildReferralLink(123456, 'PearProtocolAlertsBot')
 *   → 'https://t.me/PearProtocolAlertsBot?start=ref_123456'
 */
function buildReferralLink(userId, botUsername) {
  const u = botUsername || process.env.TELEGRAM_BOT_USERNAME || 'PearProtocolAlertsBot';
  const cleaned = String(u || '').replace(/^@/, '');
  return `https://t.me/${cleaned}?start=ref_${userId}`;
}

/**
 * Parse a /start payload into a referrer userId, or null.
 *   parseStartPayload('ref_123456') → '123456'
 */
function parseStartPayload(payload) {
  if (!payload || typeof payload !== 'string') return null;
  const m = payload.match(/^ref_(\d+)$/);
  return m ? m[1] : null;
}

function getStats(userId) {
  const s = _load();
  const rec = s[String(userId)] || { count: 0, referees: [], premium: false };
  return {
    userId: String(userId),
    count: rec.count || 0,
    referees: Array.isArray(rec.referees) ? rec.referees.slice() : [],
    premium: Boolean(rec.premium) || (rec.count || 0) >= PREMIUM_THRESHOLD,
  };
}

function isPremium(userId) {
  return getStats(userId).premium;
}

function getMaxSlots(userId) {
  return isPremium(userId) ? PREMIUM_SLOTS : DEFAULT_SLOTS;
}

/**
 * Record a referral. Idempotent — if `refereeUserId` is already in the
 * referrer's list, this is a no-op. Self-referrals are rejected.
 */
function recordReferral(referrerUserId, refereeUserId) {
  if (referrerUserId == null || refereeUserId == null) return false;
  const r1 = String(referrerUserId);
  const r2 = String(refereeUserId);
  if (r1 === r2) return false;
  const s = _load();
  if (!s[r1]) s[r1] = { count: 0, referees: [], premium: false };
  if (!Array.isArray(s[r1].referees)) s[r1].referees = [];
  if (s[r1].referees.includes(r2)) return false;
  s[r1].referees.push(r2);
  s[r1].count = s[r1].referees.length;
  s[r1].premium = s[r1].count >= PREMIUM_THRESHOLD;
  s[r1]._updated_at = Date.now();
  _save();
  return true;
}

function _resetForTests(customPath) {
  _store = {};
  try {
    const p = customPath || DB_PATH;
    if (fs.existsSync(p)) fs.unlinkSync(p);
  } catch (_) {}
}

module.exports = {
  PREMIUM_THRESHOLD,
  PREMIUM_SLOTS,
  DEFAULT_SLOTS,
  DB_PATH,
  buildReferralLink,
  parseStartPayload,
  getStats,
  isPremium,
  getMaxSlots,
  recordReferral,
  _resetForTests,
};
