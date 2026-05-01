'use strict';

/**
 * R-PUBLIC + R-CTAOPTIMIZE — Pear copy-trade URL builder.
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
 *
 * R-CTAOPTIMIZE additions
 * -----------------------
 * Optional `opts` argument:
 *   { capital?: number, leverage?: number, userId?: string|number,
 *     source?: string }
 *
 *   capital   → emits ?amount=<n> (and aliases ?size=<n>, ?capital=<n>)
 *               so whichever param Pear honours pre-fills the position.
 *               Harmless if Pear ignores all three.
 *   leverage  → emits ?leverage=<n>.
 *   userId    → hashed via sha256 to anonymized 8-char id and emitted as
 *               ?utm_id=<hash>; never raw userId.
 *   source    → emits ?utm_source=<source> (e.g. "tg-alert", "tg-signal").
 */

const crypto = require('crypto');

const REFERRAL_CODE = process.env.PEAR_REFERRAL_CODE || 'BlackCatDeFi';
const PEAR_BASE_URL = process.env.PEAR_BASE_URL || 'https://app.pear.garden';
const MAX_TOKENS = parseInt(process.env.PEAR_MAX_COPY_TOKENS || '10', 10);

// R-CTAOPTIMIZE — pre-fill param names. Pear's behaviour around accepted
// query params is unspecified; we emit a small alias set so whichever name
// the deeplink handler honours wins. All harmless if ignored.
const PEAR_PREFILL_PARAMS = (
  process.env.PEAR_PREFILL_PARAMS || 'amount,size,capital'
)
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean);

const PEAR_LEVERAGE_PARAM = process.env.PEAR_LEVERAGE_PARAM || 'leverage';

// Salt for sha256(userId) anonymization. Default is fine; rotating the salt
// invalidates historical utm_id mappings, so we keep it stable unless the
// operator explicitly overrides.
const ANALYTICS_HASH_SALT =
  process.env.ANALYTICS_HASH_SALT || 'pear-monitor-bot-v1';

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
 * Anonymize a userId for analytics. SHA-256 + 8-char prefix, salted with
 * ANALYTICS_HASH_SALT so the hash isn't a global identifier.
 *
 * Returns null for empty input.
 */
function hashUserId(userId) {
  if (userId === undefined || userId === null || userId === '') return null;
  const raw = `${ANALYTICS_HASH_SALT}:${String(userId)}`;
  return crypto.createHash('sha256').update(raw).digest('hex').slice(0, 8);
}

/**
 * Build the param suffix from R-CTAOPTIMIZE opts. Always includes referral.
 */
function _buildQuery(opts) {
  const o = opts || {};
  const params = new URLSearchParams();
  // Referral always present and first-position so it's preserved if any
  // intermediate handler truncates the query.
  params.set('referral', REFERRAL_CODE);

  // Capital → emit aliased pre-fill params.
  if (Number.isFinite(Number(o.capital)) && Number(o.capital) > 0) {
    const v = Number(o.capital);
    // Use as integer if whole, else keep up to 2 decimals.
    const amountStr =
      Number.isInteger(v) ? String(v) : v.toFixed(2).replace(/\.?0+$/, '');
    for (const name of PEAR_PREFILL_PARAMS) {
      params.set(name, amountStr);
    }
  }

  // Leverage
  if (Number.isFinite(Number(o.leverage)) && Number(o.leverage) > 0) {
    params.set(PEAR_LEVERAGE_PARAM, String(o.leverage));
  }

  // UTM tracking (anonymized).
  const utmId = hashUserId(o.userId);
  if (utmId) params.set('utm_id', utmId);
  if (o.source && typeof o.source === 'string') {
    params.set('utm_source', o.source.slice(0, 32));
  }
  if (o.medium && typeof o.medium === 'string') {
    params.set('utm_medium', o.medium.slice(0, 32));
  }
  if (o.campaign && typeof o.campaign === 'string') {
    params.set('utm_campaign', o.campaign.slice(0, 32));
  }

  return params.toString();
}

/**
 * buildPearCopyUrl(positions, side='SHORT', opts?)
 * Returns URL string or null if no matching positions.
 *
 * R-CTAOPTIMIZE: opts is forward-compatible — old call sites
 * (no opts) emit the same URL shape as before (just referral).
 */
function buildPearCopyUrl(positions, side = 'SHORT', opts) {
  if (!Array.isArray(positions) || positions.length === 0) return null;
  const filtered = _filterAndSort(positions, side);
  if (filtered.length === 0) return null;
  const tokens = filtered
    .map((p) => encodeURIComponent(p.coin))
    .join('+');
  const query = _buildQuery(opts);
  return `${PEAR_BASE_URL}/trade/hl/USDC-${tokens}?${query}`;
}

/**
 * Convenience: returns array of {label, url} buttons for a basket.
 * If basket has only one side → single button. Mixed → 2 buttons.
 */
function buildCopyButtons(positions, opts) {
  const buttons = [];
  const shorts = buildPearCopyUrl(positions, 'SHORT', opts);
  const longs = buildPearCopyUrl(positions, 'LONG', opts);
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
function buildInlineKeyboard(positions, opts) {
  const btns = buildCopyButtons(positions, opts);
  if (btns.length === 0) return null;
  return { inline_keyboard: btns.map((b) => [b]) };
}

module.exports = {
  REFERRAL_CODE,
  PEAR_BASE_URL,
  MAX_TOKENS,
  PEAR_PREFILL_PARAMS,
  PEAR_LEVERAGE_PARAM,
  buildPearCopyUrl,
  buildCopyButtons,
  buildInlineKeyboard,
  hashUserId,
};
