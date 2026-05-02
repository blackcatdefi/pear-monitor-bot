'use strict';

/**
 * R-EN — Backward-compat shim.
 *
 * Public bot is now English-only. This file preserves the legacy `t(key)` /
 * `MESSAGES` / `isSpanish()` interface so existing consumers (branding.js,
 * messageFormatters.js, weeklySummary.js, sanitizer.js) keep working without
 * code changes — but the actual strings come from `src/i18n/en.js`.
 *
 * The original Spanish dictionary lives at `src/legacy_es.js` for reference
 * and is NOT imported by any code path.
 */

const en = require('./i18n/en');

// Build a flat MESSAGES.en map keyed by the legacy uppercase keys.
const MESSAGES = { en: { ...en.alerts } };

function t(key, lang) {
  // `lang` arg is ignored — only English supported.
  if (!key) return '';
  if (Object.prototype.hasOwnProperty.call(MESSAGES.en, key)) {
    return MESSAGES.en[key];
  }
  return key;
}

/**
 * Kept for source compatibility — always returns false now (bot is English).
 */
function isSpanish() {
  return false;
}

module.exports = { t, MESSAGES, isSpanish };
