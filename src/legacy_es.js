'use strict';

/**
 * R-EN — Legacy Spanish dictionary, kept for reference ONLY.
 *
 * DEFAULT LANGUAGE: English.
 *
 * This file previously contained the Spanish MESSAGES dict.  It is NOT
 * imported by any production code path — confirmed by tests/i18n_en_only.test.js.
 *
 * To make it completely safe even if accidentally imported, t() now delegates
 * to the English i18n shim (src/i18n.js) and isSpanish() always returns false.
 *
 * Do NOT add Spanish strings here. All user-facing strings live in src/i18n/en.js.
 */

// Default language is English — no 'es' fallback.
const DEFAULT_LANGUAGE = 'en';

// Delegate to the English shim so any accidental caller gets English strings.
const enShim = require('./i18n');

function t(key, lang) {
  // Ignore lang arg — English only.
  void lang;
  void DEFAULT_LANGUAGE;
  return enShim.t(key);
}

/**
 * Always returns false — the bot is English-only.
 */
function isSpanish() {
  return false;
}

// Kept for structural compat (nothing should read this dict, but just in case).
const MESSAGES = { en: enShim.MESSAGES && enShim.MESSAGES.en ? enShim.MESSAGES.en : {} };

module.exports = { t, MESSAGES, isSpanish };
