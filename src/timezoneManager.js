'use strict';

/**
 * R-PUBLIC — Timezone Manager.
 *
 * Per-user IANA timezone, persisted to JSON on disk (same pattern as
 * basketDedup.js — avoids better-sqlite3 native build dep that breaks Alpine
 * deploys). All bot outputs that include a timestamp call formatLocalTime
 * with the recipient's userId so each user sees their own local time.
 *
 * Storage shape:
 *   {
 *     "{userId}": { "tz": "America/Argentina/Buenos_Aires", "set_at": 173... },
 *     ...
 *   }
 *
 * Default TZ: 'UTC' (configurable via DEFAULT_TZ env var).
 */

const fs = require('fs');
const path = require('path');

const DEFAULT_TZ = process.env.DEFAULT_TZ || 'UTC';

function _resolveDbPath() {
  return (
    process.env.USER_TZ_DB_PATH ||
    path.join(
      process.env.RAILWAY_VOLUME_MOUNT_PATH || '/app/data',
      'user_timezones.json'
    )
  );
}

const DB_PATH = _resolveDbPath();
let _store = null; // lazy-loaded

function _ensureDir() {
  try {
    fs.mkdirSync(path.dirname(DB_PATH), { recursive: true });
  } catch (_) {}
}

function _load() {
  if (_store !== null) return _store;
  try {
    if (fs.existsSync(DB_PATH)) {
      _store = JSON.parse(fs.readFileSync(DB_PATH, 'utf-8'));
      if (typeof _store !== 'object' || Array.isArray(_store)) _store = {};
    } else {
      _store = {};
    }
  } catch (e) {
    console.error(
      '[timezoneManager] load failed, starting empty:',
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
      '[timezoneManager] save failed:',
      e && e.message ? e.message : e
    );
  }
}

/**
 * Validate IANA TZ string. Returns true if Intl.DateTimeFormat accepts it.
 */
function isValidTimezone(tz) {
  if (!tz || typeof tz !== 'string') return false;
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: tz });
    return true;
  } catch (_) {
    return false;
  }
}

/**
 * Get TZ string for a user. Returns DEFAULT_TZ if unset.
 */
function getUserTz(userId) {
  if (userId == null) return DEFAULT_TZ;
  const s = _load();
  const rec = s[String(userId)];
  return rec && rec.tz ? rec.tz : DEFAULT_TZ;
}

/**
 * Persist user's TZ. Validates the TZ; throws on invalid.
 */
function setUserTz(userId, tz) {
  if (userId == null) throw new Error('userId required');
  if (!isValidTimezone(tz)) throw new Error(`Invalid timezone: ${tz}`);
  const s = _load();
  s[String(userId)] = { tz, set_at: Date.now() };
  _save();
  return tz;
}

function clearUserTz(userId) {
  const s = _load();
  delete s[String(userId)];
  _save();
}

/**
 * Map Telegram user.language_code (BCP-47) → best-effort IANA TZ.
 * Heuristic only; user can always override with /timezone <region>.
 */
const LANG_CODE_TO_TZ = {
  'es-AR': 'America/Argentina/Buenos_Aires',
  'es-MX': 'America/Mexico_City',
  'es-CL': 'America/Santiago',
  'es-CO': 'America/Bogota',
  'es-PE': 'America/Lima',
  'es-VE': 'America/Caracas',
  'es-UY': 'America/Montevideo',
  'es-EC': 'America/Guayaquil',
  'es-BO': 'America/La_Paz',
  'es-PY': 'America/Asuncion',
  'es-ES': 'Europe/Madrid',
  'en-US': 'America/New_York',
  'en-GB': 'Europe/London',
  'en-CA': 'America/Toronto',
  'en-AU': 'Australia/Sydney',
  'pt-BR': 'America/Sao_Paulo',
  'pt-PT': 'Europe/Lisbon',
  'fr-FR': 'Europe/Paris',
  'de-DE': 'Europe/Berlin',
  'it-IT': 'Europe/Rome',
  'ja': 'Asia/Tokyo',
  'ko': 'Asia/Seoul',
  'zh': 'Asia/Shanghai',
  'es': 'America/Argentina/Buenos_Aires',
  'en': 'America/New_York',
  'pt': 'America/Sao_Paulo',
};

function detectFromLangCode(langCode) {
  if (!langCode || typeof langCode !== 'string') return DEFAULT_TZ;
  const normalized = langCode.replace('_', '-');
  if (LANG_CODE_TO_TZ[normalized]) return LANG_CODE_TO_TZ[normalized];
  const base = normalized.split('-')[0];
  if (LANG_CODE_TO_TZ[base]) return LANG_CODE_TO_TZ[base];
  return DEFAULT_TZ;
}

const _MONTH_EN = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
];
const _DAY_EN = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

/**
 * Pick a short timezone abbreviation (e.g. 'ART', 'BRT', 'EST').
 * Falls back to GMT±N if no abbrev resolvable.
 */
function _shortTzAbbrev(tz, date) {
  try {
    const fmt = new Intl.DateTimeFormat('en-US', {
      timeZone: tz,
      timeZoneName: 'short',
    });
    const parts = fmt.formatToParts(date);
    const tzPart = parts.find((p) => p.type === 'timeZoneName');
    if (tzPart && tzPart.value) return tzPart.value;
  } catch (_) {}
  return tz;
}

/**
 * Format a timestamp in the user's local time as
 *   "{day} {DD} {mon} {YYYY} - {HH:MM} {TZ}"
 * If utcIso is omitted, uses now.
 *
 * Optional 3rd arg overrideTz lets callers (and tests) pass a tz directly
 * instead of looking it up from the userId store.
 */
function formatLocalTime(userId, utcIso, overrideTz) {
  const tz = overrideTz || getUserTz(userId);
  const date = utcIso ? new Date(utcIso) : new Date();
  if (Number.isNaN(date.getTime())) {
    return `(invalid date) ${utcIso || ''}`;
  }
  let parts;
  try {
    const fmt = new Intl.DateTimeFormat('en-GB', {
      timeZone: tz,
      year: 'numeric',
      month: 'numeric',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
      weekday: 'short',
    });
    const segs = fmt.formatToParts(date);
    const get = (t) => (segs.find((p) => p.type === t) || {}).value || '';
    const weekday = get('weekday'); // Mon, Tue, ...
    const day = get('day').padStart(2, '0');
    const mIdx = parseInt(get('month'), 10) - 1;
    const year = get('year');
    let hour = get('hour').padStart(2, '0');
    const min = get('minute').padStart(2, '0');
    if (hour === '24') hour = '00'; // Intl edge-case fix
    // English short weekday is already returned by Intl en-GB above.
    const dayEn = weekday || _DAY_EN[date.getUTCDay()] || '?';
    const monEn = _MONTH_EN[mIdx] || '?';
    const tzAbbrev = _shortTzAbbrev(tz, date);
    parts = `${dayEn} ${day} ${monEn} ${year} - ${hour}:${min} ${tzAbbrev}`;
  } catch (_) {
    parts = date.toISOString().replace('T', ' ').slice(0, 16) + ' UTC';
  }
  return parts;
}

/**
 * Stamp the bottom of a message with the user's local time, prefixed
 * with the clock emoji (so existing wrapNotifier idempotency still works).
 */
function stampMessage(userId, message, utcIso) {
  if (typeof message !== 'string') return message;
  if (message.includes('🕐')) return message; // already stamped
  return `${message}\n\n🕐 ${formatLocalTime(userId, utcIso)}`;
}

function _resetForTests() {
  _store = {};
  try {
    if (fs.existsSync(DB_PATH)) fs.unlinkSync(DB_PATH);
  } catch (_) {}
}

module.exports = {
  DEFAULT_TZ,
  DB_PATH,
  isValidTimezone,
  getUserTz,
  setUserTz,
  clearUserTz,
  detectFromLangCode,
  formatLocalTime,
  stampMessage,
  LANG_CODE_TO_TZ,
  _resetForTests,
};
