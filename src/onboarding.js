'use strict';

/**
 * R-START — Onboarding helper.
 *
 * Persists "seen users" (per chatId/userId) so we can render a different
 * /start message the first time a user types /start vs subsequent calls.
 * Also wires the language_code → TZ auto-detect path when ONBOARDING_AUTO_TZ
 * is enabled (default true), so first-time users get a sensible TZ pre-set
 * without forcing them to type /timezone.
 *
 * Storage shape (JSON, same /app/data Volume used by walletTracker):
 *   {
 *     "{userId}": { first_seen_at: 173..., last_seen_at: 173..., starts: 7 }
 *   }
 *
 * Pure module; ZERO Telegram coupling. The /start handler in commandsStart.js
 * decides what to send based on the boolean returned by markSeen().
 */

const fs = require('fs');
const path = require('path');
const tzMgr = require('./timezoneManager');

function _resolveDbPath() {
  return (
    process.env.ONBOARDING_DB_PATH ||
    path.join(
      process.env.RAILWAY_VOLUME_MOUNT_PATH || '/app/data',
      'onboarding_users.json'
    )
  );
}

const DB_PATH = _resolveDbPath();
let _store = null;

function _ensureDir() {
  try {
    fs.mkdirSync(path.dirname(DB_PATH), { recursive: true });
  } catch (_) {}
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
    console.error(
      '[onboarding] load failed, starting empty:',
      e && e.message ? e.message : e
    );
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
    console.error(
      '[onboarding] save failed:',
      e && e.message ? e.message : e
    );
  }
}

/**
 * Returns true if this userId has /start'd before.
 */
function isFirstTime(userId) {
  if (userId == null) return true;
  const s = _load();
  return !s[String(userId)];
}

/**
 * Records the user as seen. Returns { wasFirstTime, starts }.
 * Idempotent: repeated calls bump `starts` and `last_seen_at`.
 */
function markSeen(userId) {
  if (userId == null) return { wasFirstTime: false, starts: 0 };
  const s = _load();
  const key = String(userId);
  const wasFirstTime = !s[key];
  if (wasFirstTime) {
    s[key] = {
      first_seen_at: Date.now(),
      last_seen_at: Date.now(),
      starts: 1,
    };
  } else {
    s[key].last_seen_at = Date.now();
    s[key].starts = (s[key].starts || 0) + 1;
  }
  _save();
  return { wasFirstTime, starts: s[key].starts };
}

function getUserRecord(userId) {
  if (userId == null) return null;
  const s = _load();
  return s[String(userId)] || null;
}

/**
 * Auto-detect timezone from Telegram language_code and persist via
 * timezoneManager. No-op if ONBOARDING_AUTO_TZ=false or user already has
 * a TZ recorded (so we never clobber a manual override).
 *
 * Returns the IANA TZ that was set (or already in place).
 */
function autoDetectTzIfFirstTime(userId, languageCode) {
  if (
    (process.env.ONBOARDING_AUTO_TZ || 'true').toLowerCase() === 'false'
  ) {
    return tzMgr.getUserTz(userId);
  }
  if (userId == null) return tzMgr.getUserTz(userId);
  // If the user already has a record in the TZ store, leave it alone.
  // We can detect this by comparing against DEFAULT_TZ — if it's still
  // the default, we have permission to set it.
  const current = tzMgr.getUserTz(userId);
  if (current && current !== tzMgr.DEFAULT_TZ) return current;
  const detected = tzMgr.detectFromLangCode(languageCode);
  if (!detected || detected === tzMgr.DEFAULT_TZ) return current;
  try {
    tzMgr.setUserTz(userId, detected);
    return detected;
  } catch (_) {
    return current;
  }
}

function _resetForTests() {
  _store = {};
  try {
    if (fs.existsSync(DB_PATH)) fs.unlinkSync(DB_PATH);
  } catch (_) {}
}

module.exports = {
  DB_PATH,
  isFirstTime,
  markSeen,
  getUserRecord,
  autoDetectTzIfFirstTime,
  _resetForTests,
};
