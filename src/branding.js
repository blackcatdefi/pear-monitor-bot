'use strict';

/**
 * R-PUBLIC — Branding footer for the public Pear Protocol Alerts bot.
 * Appended to alert bodies. Referral code is the literal string used in
 * Pear's URL `?referral=` query param — it is not a personal identifier.
 * Kill switch: BRANDING_ENABLED=false.
 */

const { t } = require('./i18n');

// Referral code accepted by Pear Protocol. Override via PEAR_REFERRAL_CODE.
// Legacy env var BCD_PEAR_REFERRAL_CODE is still honored for backward compat.
const REFERRAL_CODE =
  process.env.PEAR_REFERRAL_CODE ||
  process.env.BCD_PEAR_REFERRAL_CODE ||
  'BlackCatDeFi';
const REFERRAL_LINK =
  process.env.PEAR_REFERRAL_LINK ||
  process.env.BCD_PEAR_REFERRAL_LINK ||
  `https://app.pear.garden/?referral=${REFERRAL_CODE}`;

function isEnabled() {
  return (process.env.BRANDING_ENABLED || 'true').toLowerCase() !== 'false';
}

function getFooter() {
  if (!isEnabled()) return '';
  return [
    '',
    '─────────────────',
    `📌 ${t('AMBASSADOR_TAGLINE')}`,
    `${t('REFERRAL_CTA')} (\`${REFERRAL_CODE}\`)`,
    `🔗 ${REFERRAL_LINK}`,
  ].join('\n');
}

function appendFooter(message, isPrimaryWallet = true) {
  if (!isEnabled() || !isPrimaryWallet) return message;
  return `${message}${getFooter()}`;
}

module.exports = {
  isEnabled,
  getFooter,
  appendFooter,
  REFERRAL_CODE,
  // legacy alias for any caller that still imports BCD_REFERRAL_CODE
  BCD_REFERRAL_CODE: REFERRAL_CODE,
  REFERRAL_LINK,
};
