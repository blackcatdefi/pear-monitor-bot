'use strict';

/**
 * R-START + R-CTAOPTIMIZE — Alert keyboard builder.
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
 * R-CTAOPTIMIZE additions:
 *   - hero label is capital-aware: "🍐 Copiar $500 en Pear" when capital is
 *     known, falls back to "🍐 Copiar en Pear" otherwise.
 *   - optional quick-amount row: [0.5x · 1x · 2x] each pre-filling a different
 *     amount in the Pear URL. Disabled when capital is not provided.
 *   - opts.userId / opts.source are forwarded to pearUrlBuilder so the URL
 *     carries anonymized utm_id and utm_source, enabling conversion tracking.
 *
 * Public surface:
 *   buildAlertKeyboard(positions, type, opts)
 *     positions : Array<{ coin, side, size, entryPrice/markPrice, notional? }>
 *     type      : 'open' | 'close'  (close events get only secondary buttons)
 *     opts      : {
 *                   wallet?: string,         // enables 🔕 Silenciar wallet
 *                   capital?: number,        // enables capital-aware hero label
 *                   leverage?: number,
 *                   userId?: string|number,  // anonymized via utm_id
 *                   source?: string,         // utm_source, e.g. 'tg-alert'
 *                   showQuickAmounts?: bool, // override env QUICK_AMOUNTS_ENABLED
 *                 }
 *
 *   getCopyCtaText() — returns the configurable invitation text.
 *
 *   getHeroUrl(positions, side, opts) — exposes the underlying Pear URL builder
 *     so handlers can include the URL in message bodies if they need to.
 *
 *   formatAmount(n) — "$500" / "$1k" / "$5k" / "$10k" formatter for labels.
 */

const pearUrl = require('./pearUrlBuilder');

const DEFAULT_COPY_CTA = '⚡ Replicá esta operación con un toque:';

// R-CTAOPTIMIZE — quick-amount multipliers. 0.5x / 1x / 2x of user capital.
// Override via env QUICK_AMOUNT_MULTIPLIERS="0.5,1,2" (comma-separated).
const QUICK_AMOUNT_MULTIPLIERS = (
  process.env.QUICK_AMOUNT_MULTIPLIERS || '0.5,1,2'
)
  .split(',')
  .map((s) => Number(String(s).trim()))
  .filter((n) => Number.isFinite(n) && n > 0);

// R-CTAOPTIMIZE — quick-amount row enabled by default; turn off via env.
const QUICK_AMOUNTS_ENABLED =
  String(process.env.QUICK_AMOUNTS_ENABLED || 'true').toLowerCase() !== 'false';

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
 * Format a USD amount for button labels.
 *  500       → "$500"
 *  1000      → "$1k"
 *  1500      → "$1.5k"
 *  10000     → "$10k"
 *  100000    → "$100k"
 *  1500000   → "$1.5M"
 * Returns null for non-positive / non-finite input.
 */
function formatAmount(n) {
  const v = Number(n);
  if (!Number.isFinite(v) || v <= 0) return null;
  if (v >= 1_000_000) {
    const m = v / 1_000_000;
    return `$${(Math.round(m * 10) / 10).toString().replace(/\.0$/, '')}M`;
  }
  if (v >= 1_000) {
    const k = v / 1_000;
    return `$${(Math.round(k * 10) / 10).toString().replace(/\.0$/, '')}k`;
  }
  return `$${Math.round(v)}`;
}

/**
 * Round a multiplier-derived amount to a sensible number of significant
 * digits. Avoids labels like "$249.99 in Pear".
 */
function _roundQuickAmount(v) {
  if (!Number.isFinite(v) || v <= 0) return 0;
  if (v >= 10_000) return Math.round(v / 100) * 100;
  if (v >= 1_000) return Math.round(v / 10) * 10;
  if (v >= 100) return Math.round(v / 5) * 5;
  return Math.round(v);
}

function _ctaOpts(o) {
  // Filter the subset of opts we want to forward to pearUrlBuilder.
  const out = {};
  if (Number.isFinite(Number(o.leverage)) && Number(o.leverage) > 0) {
    out.leverage = Number(o.leverage);
  }
  if (o.userId !== undefined && o.userId !== null && o.userId !== '') {
    out.userId = o.userId;
  }
  if (o.source) out.source = String(o.source).slice(0, 32);
  if (o.medium) out.medium = String(o.medium).slice(0, 32);
  if (o.campaign) out.campaign = String(o.campaign).slice(0, 32);
  return out;
}

/**
 * Build the hero CTA row(s). Returns an array of rows, each row is an
 * array of inline-keyboard buttons. Empty array if no positions matched.
 *
 * Single-side basket → 1 hero row + optional quick-amount row.
 * Mixed-side basket  → 2 hero rows (one per side); quick amounts skipped
 *                      to keep the keyboard compact.
 */
function _buildHeroRows(positions, opts) {
  const o = opts || {};
  const cap = Number.isFinite(Number(o.capital)) && Number(o.capital) > 0
    ? Number(o.capital)
    : null;
  const capLabel = formatAmount(cap);
  const ctaOpts = _ctaOpts(o);

  const shortsUrl = pearUrl.buildPearCopyUrl(positions, 'SHORT', cap ? { ...ctaOpts, capital: cap } : ctaOpts);
  const longsUrl = pearUrl.buildPearCopyUrl(positions, 'LONG', cap ? { ...ctaOpts, capital: cap } : ctaOpts);

  const heroLabelFor = (side) => {
    const sideTag = side === 'SHORT' ? 'SHORTs' : 'LONGs';
    if (capLabel) return `🍐 Copiar ${sideTag} ${capLabel} en Pear`;
    return `🍐 Copiar ${sideTag} en Pear`;
  };
  const heroLabelSingle = capLabel
    ? `🍐 Copiar ${capLabel} en Pear`
    : '🍐 Copiar en Pear';

  const rows = [];
  if (shortsUrl && longsUrl) {
    rows.push([{ text: heroLabelFor('SHORT'), url: shortsUrl }]);
    rows.push([{ text: heroLabelFor('LONG'), url: longsUrl }]);
    return rows;
  }

  const singleUrl = shortsUrl || longsUrl;
  const singleSide = shortsUrl ? 'SHORT' : 'LONG';
  if (!singleUrl) return rows;

  rows.push([{ text: heroLabelSingle, url: singleUrl }]);

  // Quick-amount row — only when capital known + enabled + single side.
  const quickEnabled =
    o.showQuickAmounts !== undefined ? Boolean(o.showQuickAmounts) : QUICK_AMOUNTS_ENABLED;
  if (cap && quickEnabled && QUICK_AMOUNT_MULTIPLIERS.length > 0) {
    const quickRow = [];
    for (const m of QUICK_AMOUNT_MULTIPLIERS) {
      const amt = _roundQuickAmount(cap * m);
      if (amt <= 0) continue;
      const url = pearUrl.buildPearCopyUrl(positions, singleSide, {
        ...ctaOpts,
        capital: amt,
      });
      if (!url) continue;
      // Label: clean multiplier ("1x") + amount in parens ("$500").
      const mLabel = m === Math.floor(m) ? `${m}x` : `${m.toFixed(1).replace(/\.0$/, '')}x`;
      quickRow.push({
        text: `${mLabel} (${formatAmount(amt)})`,
        url,
      });
    }
    if (quickRow.length > 0) rows.push(quickRow);
  }

  return rows;
}

/**
 * Build the inline keyboard for an alert.
 *
 * Row 1 (hero):    🍐 Copiar $500 en Pear      [URL]
 *   (or)           🍐 Copiar SHORTs $500 en Pear  +  🍐 Copiar LONGs $500 en Pear
 *                  if the basket is mixed-side.
 *
 * Row 2 (quick amounts, single-side OPEN only):
 *                  0.5x ($250) · 1x ($500) · 2x ($1k)
 *
 * Row 3 (secondary, OPEN events only):
 *                  🔕 Silenciar wallet    [callback mute:<addr>]
 */
function buildAlertKeyboard(positions, type, opts) {
  const o = opts || {};
  const wallet = o.wallet || null;
  const rows = [];

  // Hero rows — only for OPEN events (closes don't need a copy button).
  if (type === 'open' || type == null) {
    const heroRows = _buildHeroRows(positions, o);
    for (const r of heroRows) rows.push(r);
  }

  // Secondary controls (mute wallet)
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
 * Pear copy-trade URL without rebuilding the whole keyboard. Forwards opts
 * (capital/leverage/userId/source) so the surfaced URL is conversion-ready.
 */
function getHeroUrl(positions, side, opts) {
  const o = opts || {};
  const cap = Number.isFinite(Number(o.capital)) && Number(o.capital) > 0
    ? Number(o.capital)
    : null;
  const ctaOpts = _ctaOpts(o);
  return pearUrl.buildPearCopyUrl(
    positions,
    side || 'SHORT',
    cap ? { ...ctaOpts, capital: cap } : ctaOpts
  );
}

module.exports = {
  DEFAULT_COPY_CTA,
  QUICK_AMOUNT_MULTIPLIERS,
  QUICK_AMOUNTS_ENABLED,
  getCopyCtaText,
  buildAlertKeyboard,
  getHeroUrl,
  formatAmount,
  _shortAddr,
  _buildHeroRows,
  _roundQuickAmount,
  _ctaOpts,
};
