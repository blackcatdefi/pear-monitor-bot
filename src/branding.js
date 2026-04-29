'use strict';

/**
 * Round v2 — Branding footer for embajador BCD.
 * Appended to alert bodies that target BCD's primary wallet only.
 * Kill switch: BRANDING_ENABLED=false.
 */

const { t } = require('./i18n');

const BCD_REFERRAL_CODE =
  process.env.BCD_PEAR_REFERRAL_CODE || 'BLACKCATDEFI';
const REFERRAL_LINK =
  process.env.BCD_PEAR_REFERRAL_LINK ||
  `https://app.pear.garden/?ref=${BCD_REFERRAL_CODE}`;

function isEnabled() {
  return (process.env.BRANDING_ENABLED || 'true').toLowerCase() !== 'false';
}

function getFooter() {
  if (!isEnabled()) return '';
  return [
    '',
    '─────────────────',
    `📌 ${t('AMBASSADOR_TAGLINE')}`,
    `${t('REFERRAL_CTA')} (\`${BCD_REFERRAL_CODE}\`)`,
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
  BCD_REFERRAL_CODE,
  REFERRAL_LINK,
};
