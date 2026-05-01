'use strict';

/**
 * R-START — Alert keyboard builder.
 *
 * Layouts the inline keyboard for OPEN/CLOSE alerts so the Pear "Copy in Pear"
 * button sits in the FIRST row (hero CTA). Secondary actions (silence wallet)
 * go in the second row.
 *
 * The hero button label intentionally does NOT contain the word "referral" or
 * "BlackCat" — Telegram only shows the label, the URL (which carries the
 * referral) is only visible after the user clicks. Keeps the bot
 * sanitizer-clean (no personal-name leaks) AND respects the standard Telegram
 * pattern of hidden affiliate links.
 *
 * Public surface:
 *   buildAlertKeyboard(positions, type, opts)
 *     positions : Array<{ coin, side, size, entryPrice/markPrice, notional? }>
 *     type      : 'open' | 'close'  (close events get only secondary buttons)
 *     opts      : { wallet?: string }   (wallet enables 🔕 Silenciar wallet)
 *
 *   getCopyCtaText() — returns the configurable invitation text.
 *
 *   getHeroUrl(positions, side) — exposes the underlying Pear URL builder
 *     so handlers can include the URL in message bodies if they need to.
 */

const pearUrl = require('./pearUrlBuilder');

const DEFAULT_COPY_CTA = '⚡ Replicá esta operación con un toque:';

function getCopyCtaText() {
  const fromEnv = process.env.COPY_CTA_TEXT;
  if (fromEnv && String(fromEnv).trim()) return String(fromEnv);
  return DEFAULT_COPY_CTA;
}

function _shortAddr(a) {
  if (!a) return null;
  const s = String(a);
  if (s.length < 12) return s.toLowerCase();
  return `${s.slice(0, 6)}...${s.slice(-4)}`.toLowerCase();
}

/**
 * Build the inline keyboard for an alert.
 *
 * Row 1 (hero):    🍐 Copiar en Pear      [URL]
 *   (or)           🍐 Copiar SHORTs en Pear  +  🍐 Copiar LONGs en Pear
 *                  if the basket is mixed-side.
 *
 * Row 2 (secondary, OPEN events only):
 *                  🔕 Silenciar wallet    [callback mute:<addr>]
 */
function buildAlertKeyboard(positions, type, opts) {
  const o = opts || {};
  const wallet = o.wallet || null;
  const rows = [];

  // Row 1 — hero CTAs (only for OPEN events; closes don't need a copy button)
  if (type === 'open' || type == null) {
    const shortsUrl = pearUrl.buildPearCopyUrl(positions, 'SHORT');
    const longsUrl = pearUrl.buildPearCopyUrl(positions, 'LONG');
    if (shortsUrl && longsUrl) {
      rows.push([{ text: '🍐 Copiar SHORTs en Pear', url: shortsUrl }]);
      rows.push([{ text: '🍐 Copiar LONGs en Pear', url: longsUrl }]);
    } else if (shortsUrl) {
      rows.push([{ text: '🍐 Copiar en Pear', url: shortsUrl }]);
    } else if (longsUrl) {
      rows.push([{ text: '🍐 Copiar en Pear', url: longsUrl }]);
    }
  }

  // Row 2 — secondary controls (mute wallet)
  if (wallet) {
    const muteCb = `mute:${String(wallet).toLowerCase()}`;
    rows.push([
      { text: '🔕 Silenciar wallet', callback_data: muteCb.slice(0, 64) },
    ]);
  }

  if (rows.length === 0) return null;
  return { inline_keyboard: rows };
}

/**
 * Convenience for handlers that need to inspect / surface the underlying
 * Pear copy-trade URL without rebuilding the whole keyboard.
 */
function getHeroUrl(positions, side) {
  return pearUrl.buildPearCopyUrl(positions, side || 'SHORT');
}

module.exports = {
  DEFAULT_COPY_CTA,
  getCopyCtaText,
  buildAlertKeyboard,
  getHeroUrl,
  _shortAddr,
};
