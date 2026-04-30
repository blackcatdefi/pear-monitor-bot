'use strict';

/**
 * R(v3) — Timestamp helper.
 *
 * Adds an explicit `🕐 dd mmm yyyy - HH:MM UTC` line to alerts so BCD can
 * correlate Telegram messages with on-chain events without scrolling
 * Telegram metadata. Mirrors the Python bot's footer convention so the
 * Node alerts and the Python reports stay visually consistent.
 *
 * Spanish day/month abbreviations, UTC always (BCD operates in UTC).
 */

const DAYS_ES = ['dom', 'lun', 'mar', 'mié', 'jue', 'vie', 'sáb'];
const MONTHS_ES = [
  'ene',
  'feb',
  'mar',
  'abr',
  'may',
  'jun',
  'jul',
  'ago',
  'sep',
  'oct',
  'nov',
  'dic',
];

function isEnabled() {
  return (
    (process.env.TIMESTAMP_ON_MESSAGES || 'true').toLowerCase() !== 'false'
  );
}

function formatTimestamp(date = new Date()) {
  const d = date instanceof Date ? date : new Date(date);
  const day = DAYS_ES[d.getUTCDay()];
  const month = MONTHS_ES[d.getUTCMonth()];
  const dt = d.getUTCDate();
  const yr = d.getUTCFullYear();
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const mm = String(d.getUTCMinutes()).padStart(2, '0');
  return `🕐 ${day} ${dt} ${month} ${yr} - ${hh}:${mm} UTC`;
}

/**
 * Append (or prepend) an italicised UTC timestamp to a message.
 *
 *   position = 'top'    → timestamp at the very top
 *   position = 'bottom' → timestamp at the very bottom (default)
 */
function withTimestamp(message, position = 'bottom') {
  if (!isEnabled()) return message;
  if (typeof message !== 'string') return message;
  const ts = formatTimestamp();
  if (position === 'top') {
    return `${ts}\n\n${message}`;
  }
  return `${message}\n\n_${ts}_`;
}

module.exports = {
  isEnabled,
  formatTimestamp,
  withTimestamp,
  DAYS_ES,
  MONTHS_ES,
};
