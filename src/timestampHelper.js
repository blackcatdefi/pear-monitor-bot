'use strict';

/**
 * R(v3) — Timestamp helper.
 *
 * Adds an explicit `🕐 dd mmm yyyy - HH:MM TZ` line to alerts so users can
 * correlate Telegram messages with on-chain events without scrolling
 * Telegram metadata.
 *
 * R-PUBLIC: when a userId is supplied, the timestamp is rendered in that
 * user's locally-stored IANA timezone (via timezoneManager). When omitted,
 * falls back to UTC for backward compatibility (group chats, schedulers
 * with no specific user).
 */

const DAYS_EN = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const MONTHS_EN = [
  'Jan',
  'Feb',
  'Mar',
  'Apr',
  'May',
  'Jun',
  'Jul',
  'Aug',
  'Sep',
  'Oct',
  'Nov',
  'Dec',
];

function isEnabled() {
  return (
    (process.env.TIMESTAMP_ON_MESSAGES || 'true').toLowerCase() !== 'false'
  );
}

function formatTimestamp(date = new Date(), userId = null) {
  const d = date instanceof Date ? date : new Date(date);
  if (userId != null) {
    try {
      const tzMgr = require('./timezoneManager');
      return `🕐 ${tzMgr.formatLocalTime(userId, d.toISOString())}`;
    } catch (_) {
      // fall through to UTC if timezoneManager is unavailable
    }
  }
  const day = DAYS_EN[d.getUTCDay()];
  const month = MONTHS_EN[d.getUTCMonth()];
  const dt = d.getUTCDate();
  const yr = d.getUTCFullYear();
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const mm = String(d.getUTCMinutes()).padStart(2, '0');
  return `🕐 ${day} ${dt} ${month} ${yr} - ${hh}:${mm} UTC`;
}

/**
 * Append (or prepend) an italicised local-time timestamp to a message.
 *
 *   position = 'top'    → timestamp at the very top
 *   position = 'bottom' → timestamp at the very bottom (default)
 *   userId   = number   → render in that user's TZ (R-PUBLIC); falls back
 *                         to UTC when null/undefined.
 */
function withTimestamp(message, position = 'bottom', userId = null) {
  if (!isEnabled()) return message;
  if (typeof message !== 'string') return message;
  const ts = formatTimestamp(new Date(), userId);
  if (position === 'top') {
    return `${ts}\n\n${message}`;
  }
  return `${message}\n\n_${ts}_`;
}

module.exports = {
  isEnabled,
  formatTimestamp,
  withTimestamp,
  DAYS_EN,
  MONTHS_EN,
};
