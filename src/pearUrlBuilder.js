'use strict';

/**
 * R-PUBLIC — Pear copy-trade URL builder.
 *
 * Pure function. Given a list of position objects and a target side, returns
 * a `https://app.pear.garden/trade/...` URL with referral param attached.
 * If no positions match the side returns null.
 *
 * Pear basket route shape (observed in app.pear.garden as of may 2026):
 *   /trade/hl/USDC-{TOKEN1}+{TOKEN2}+...?referral={code}
 *
 * Tokens are sorted by descending |notional| so the heaviest legs win when
 * we truncate at MAX_TOKENS (Pear URL practical limit).
 */

const REFERRAL_CODE = process.env.PEAR_REFERRAL_CODE || 'BlackCatDeFi';
const PEAR_BASE_URL = process.env.PEAR_BASE_URL || 'https://app.pear.garden';
const MAX_TOKENS = parseInt(process.env.PEAR_MAX_COPY_TOKENS || '10', 10);

function _normalizeSide(s) {
  if (!s) return null;
  const v = String(s).toUpperCase();
  if (v === 'LONG' || v === 'SHORT') return v;
  return null;
}

function _filterAndSort(positions, side) {
  const wanted = _normalizeSide(side) || 'SHORT';
  return positions
    .filter((p) => {
      const ps = _normalizeSide(p && p.side) || (p && p.size < 0 ? 'SHORT' : 'LONG');
      return ps === wanted && p && p.coin;
    })
    .map((p) => ({
      coin: String(p.coin).toUpperCase(),
      notional: Math.abs(Number(p.notional || 0)) || 0,
    }))
    .sort((a, b) => b.notional - a.notional)
    .slice(0, Math.max(1, MAX_TOKENS));
}

/**
 * buildPearCopyUrl(positions, side='SHORT')
 * Returns URL string or null if no matching positions.
 */
function buildPearCopyUrl(positions, side = 'SHORT') {
  if (!Array.isArray(positions) || positions.length === 0) return null;
  const filtered = _filterAndSort(positions, side);
  if (filtered.length === 0) return null;
  const tokens = filtered
    .map((p) => encodeURIComponent(p.coin))
    .join('+');
  return `${PEAR_BASE_URL}/trade/hl/USDC-${tokens}?referral=${encodeURIComponent(REFERRAL_CODE)}`;
}

/**
 * Convenience: returns array of {label, url} buttons for a basket.
 * If basket has only one side → single button. Mixed → 2 buttons.
 */
function buildCopyButtons(positions) {
  const buttons = [];
  const shorts = buildPearCopyUrl(positions, 'SHORT');
  const longs = buildPearCopyUrl(positions, 'LONG');
  if (shorts && longs) {
    buttons.push({ text: '🔗 Copiar SHORTs en Pear', url: shorts });
    buttons.push({ text: '🔗 Copiar LONGs en Pear', url: longs });
  } else if (shorts) {
    buttons.push({ text: '🔗 Copiar trade en Pear', url: shorts });
  } else if (longs) {
    buttons.push({ text: '🔗 Copiar trade en Pear', url: longs });
  }
  return buttons;
}

/**
 * Telegram inline_keyboard formatter — pass output directly into
 * bot.sendMessage opts.reply_markup.
 */
function buildInlineKeyboard(positions) {
  const btns = buildCopyButtons(positions);
  if (btns.length === 0) return null;
  return { inline_keyboard: btns.map((b) => [b]) };
}

module.exports = {
  REFERRAL_CODE,
  PEAR_BASE_URL,
  MAX_TOKENS,
  buildPearCopyUrl,
  buildCopyButtons,
  buildInlineKeyboard,
};
