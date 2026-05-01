'use strict';

/**
 * R-PUBLIC — Sanitizer.
 *
 * Helper that gathers all user-facing strings exported by the bot's modules
 * and exposes them to regression tests. Tests assert that no personal /
 * fund-private references leak into output sent to Pear Protocol public users.
 *
 * The sanitizer itself is not invoked at runtime — modules have already been
 * cleaned at source. This module exists so the test suite can keep watch.
 *
 * Allowed appearances of "BlackCatDeFi":
 *   • As the literal string `BlackCatDeFi` inside a Pear referral URL or as
 *     the value of the env var PEAR_REFERRAL_CODE.
 * Disallowed:
 *   • Any other appearance (persona, fund, "el fondo", etc.)
 */

const path = require('path');
const fs = require('fs');

// Personal / private terms that must not appear in any user-facing string.
const FORBIDDEN_TERMS = [
  /norber/i,
  /criptonorberbtc/i,
  /\bdalio\b/i,
  /druckenmiller/i,
  /\bthiel\b/i,
  /\bruarte\b/i,
  /lady\s*market/i,
  /\blmec\b/i,
  /hormuz/i,
  /\biran\b/i,
  /\bbrent\b/i,
  /stage\s*6/i,
  /\baipear\b/i,
  /supergrok/i,
  /\bcowork\b/i,
  /tesis del fondo/i,
  /el fondo/i,
  /fondo black\s*cat/i,
  /modus operandi/i,
  /war trade/i,
  /\bdca\b.*piso/i,
];

// "BlackCatDeFi" / "BCD" / "Black Cat" allowed ONLY when used as referral code
// inside a Pear URL, env var value, or as the official Telegram channel handle
// "@BlackCatDeFiSignals" (R-AUTOCOPY).
function isAllowedBlackCat(line) {
  if (typeof line !== 'string') return false;
  // Allow when used as referral query param value or as referral env var/key
  if (/referral=BlackCatDeFi/i.test(line)) return true;
  if (/PEAR_REFERRAL_CODE/.test(line)) return true;
  if (/BCD_PEAR_REFERRAL_CODE/.test(line)) return true;
  if (/BCD_PEAR_REFERRAL_LINK/.test(line)) return true;
  // Allow the bare literal referral-code value (default fallback in modules)
  if (/^\s*BlackCatDeFi\s*$/.test(line)) return true;
  // R-AUTOCOPY — public Telegram channel handle is allowed (it's the
  // canonical signals channel, not a persona reference).
  if (/@BlackCatDeFiSignals/i.test(line)) return true;
  if (/SIGNALS_CHANNEL/i.test(line)) return true;
  if (/blackcatdefisignals/i.test(line)) return true;
  return false;
}

function findForbiddenInString(s) {
  if (typeof s !== 'string') return [];
  const hits = [];
  for (const re of FORBIDDEN_TERMS) {
    if (re.test(s)) hits.push(re.toString());
  }
  // BlackCat as persona: any "BlackCat" / "Black Cat" / "BCD" not in URL/env context
  if (/black\s*cat/i.test(s) && !isAllowedBlackCat(s)) hits.push('BlackCat-as-persona');
  if (/\bBCD\b/.test(s) && !isAllowedBlackCat(s)) hits.push('BCD-as-persona');
  return hits;
}

/**
 * Collects user-facing strings from the dynamic outputs the bot can produce.
 * Returns array of strings. Used by sanitizer.test.js.
 */
function collectAllUserFacingStrings() {
  const strings = [];

  // i18n dictionary
  try {
    const { MESSAGES } = require('./i18n');
    for (const lang of Object.keys(MESSAGES)) {
      for (const v of Object.values(MESSAGES[lang])) {
        if (typeof v === 'string') strings.push(v);
      }
    }
  } catch (_) {}

  // Branding footer
  try {
    const branding = require('./branding');
    if (branding.getFooter) strings.push(branding.getFooter());
  } catch (_) {}

  // openAlerts formatters
  try {
    const open = require('./openAlerts');
    const dummyPositions = [
      { coin: 'BTC', side: 'SHORT', size: 1, entryPrice: 50000, leverage: 4 },
      { coin: 'ETH', side: 'SHORT', size: 10, entryPrice: 3000, leverage: 4 },
      { coin: 'SOL', side: 'SHORT', size: 100, entryPrice: 150, leverage: 4 },
    ];
    strings.push(open.formatBasketOpenAlert('Test Wallet', dummyPositions));
    strings.push(open.formatIndividualOpenAlert('Test Wallet', dummyPositions[0]));
  } catch (_) {}

  // compoundingDetector output
  try {
    const cd = require('./compoundingDetector');
    if (cd.formatCompoundAlert) {
      strings.push(
        cd.formatCompoundAlert('Test', {
          prevNotional: 1000,
          currentNotional: 1200,
          growth: 0.2,
          coins: ['BTC'],
        })
      );
    }
  } catch (_) {}

  // External wallet tracker formatters
  try {
    const ext = require('./externalWalletTracker');
    const cfg = { address: '0x' + 'a'.repeat(40), label: 'Whale 1' };
    const pos = { coin: 'BTC', side: 'LONG', size: 1, entryPx: 50000, notional: 50000, unrealizedPnl: 100 };
    strings.push(ext.formatExternalOpenAlert(cfg, pos));
    strings.push(ext.formatExternalCloseAlert(cfg, pos));
  } catch (_) {}

  // Source-level scan: read all src/*.js files, extract string literals from
  // top of each line for completeness.
  try {
    const dir = path.join(__dirname);
    const files = fs.readdirSync(dir).filter(
      (f) =>
        f.endsWith('.js') &&
        f !== 'sanitizer.js' &&
        !f.endsWith('.test.js')
    );
    for (const f of files) {
      const txt = fs.readFileSync(path.join(dir, f), 'utf-8');
      // Strip comment lines so we don't false-flag terms that appear in
      // historical /** ... */ blocks. Tests still validate the runtime
      // user-facing strings collected above.
      const noComments = txt
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .replace(/(^|[^:])\/\/.*$/gm, '$1');
      // Pull double-quoted, single-quoted, and template-literal contents
      const regexes = [
        /"([^"\\\n]*(?:\\.[^"\\\n]*)*)"/g,
        /'([^'\\\n]*(?:\\.[^'\\\n]*)*)'/g,
        /`([^`\\]*(?:\\.[^`\\]*)*)`/g,
      ];
      for (const re of regexes) {
        let m;
        while ((m = re.exec(noComments)) !== null) {
          if (m[1] && m[1].length >= 3) strings.push(m[1]);
        }
      }
    }
  } catch (_) {}

  return strings;
}

module.exports = {
  FORBIDDEN_TERMS,
  isAllowedBlackCat,
  findForbiddenInString,
  collectAllUserFacingStrings,
};
