'use strict';

/**
 * R-AUTOCOPY — Signals parser.
 *
 * Parses messages posted to @BlackCatDeFiSignals into a structured signal
 * object. The expected format (also documented in the channel pin):
 *
 *   🚀 SIGNAL OFICIAL #42
 *
 *   📊 Basket: 5 tokens
 *     DYDX SHORT
 *     OP SHORT
 *     ARB SHORT
 *     PYTH SHORT
 *     ENA SHORT
 *
 *   ⚡ Leverage: 4x
 *   🎯 SL: 50% basket / Trailing 10% activación 30%
 *   ⏱️ TWAP: 14h, 30 bullets
 *
 *   #signal #basket
 *
 * Returns null if the message doesn't look like a signal (no #signal hashtag
 * or no recognizable basket block) so the channel listener can skip non-signal
 * announcements without spamming users.
 */

const SIGNAL_HASHTAG_REGEX = /#signal\b/i;
const SIGNAL_HEADER_REGEX = /signal\s+(?:oficial|official)\s+#?(\d+)/i;
const ALT_SIGNAL_HEADER_REGEX = /signal\s+#?(\d+)/i;
const LEVERAGE_REGEX = /(?:leverage|leverag\w*|apalancamiento)[^\d]*?(\d+(?:\.\d+)?)\s*x/i;
const SL_REGEX = /SL[^\d%]*?(\d+(?:\.\d+)?)\s*%/i;
const TRAILING_REGEX = /trailing[^\d%]*?(\d+(?:\.\d+)?)\s*%/i;
const TRAILING_ACTIVATION_REGEX = /(?:activation|activaci[oó]n)[^\d%]*?(\d+(?:\.\d+)?)\s*%/i;
const TWAP_HOURS_REGEX = /TWAP[^\d]*?(\d+(?:\.\d+)?)\s*h/i;
const TWAP_BULLETS_REGEX = /(\d+)\s*bullets?/i;

const POSITION_LINE_REGEX = /^\s*[•·\-\*]?\s*([A-Z0-9]{2,12})\s+(LONG|SHORT)\s*(?:@[^\n]*)?$/i;

/**
 * Extract `tokens[]` and `sides[]` from the message body.
 * Accepts lines like "  DYDX SHORT" or "• ENA SHORT" or "- BTC LONG".
 */
function _parsePositions(body) {
  const out = [];
  const lines = body.split(/\r?\n/);
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) continue;
    const m = line.match(POSITION_LINE_REGEX);
    if (m) {
      const coin = m[1].toUpperCase();
      const side = m[2].toUpperCase();
      // Skip obvious header tokens
      if (['BASKET', 'LEVERAGE', 'SL', 'TWAP', 'SIDE'].includes(coin)) continue;
      out.push({ coin, side });
    }
  }
  return out;
}

/**
 * Parse a Telegram channel post into a signal object, or null if the post
 * doesn't look like a signal. Use:
 *
 *   const sig = parseSignal(text);
 *   if (!sig) return; // skip non-signals
 */
function parseSignal(text) {
  if (typeof text !== 'string' || text.length < 10) return null;
  const hasHashtag = SIGNAL_HASHTAG_REGEX.test(text);
  const headerMatch =
    text.match(SIGNAL_HEADER_REGEX) ||
    text.match(ALT_SIGNAL_HEADER_REGEX);
  if (!hasHashtag && !headerMatch) return null;

  const positions = _parsePositions(text);
  if (positions.length === 0) return null;

  const lev = text.match(LEVERAGE_REGEX);
  const sl = text.match(SL_REGEX);
  const tr = text.match(TRAILING_REGEX);
  const trAct = text.match(TRAILING_ACTIVATION_REGEX);
  const twapH = text.match(TWAP_HOURS_REGEX);
  const twapB = text.match(TWAP_BULLETS_REGEX);

  return {
    signal_id: headerMatch ? String(headerMatch[1]) : null,
    tokens: positions.map((p) => p.coin),
    sides: positions.map((p) => p.side),
    positions, // [{coin, side}]
    leverage: lev ? parseFloat(lev[1]) : null,
    sl_pct: sl ? parseFloat(sl[1]) : 50,
    trailing_pct: tr ? parseFloat(tr[1]) : 10,
    trailing_activation_pct: trAct ? parseFloat(trAct[1]) : 30,
    twap_hours: twapH ? parseFloat(twapH[1]) : null,
    twap_bullets: twapB ? parseInt(twapB[1], 10) : null,
    raw_text: text,
  };
}

/**
 * Quick boolean predicate without paying for full parse — used by the
 * channel listener to early-exit non-signal posts.
 */
function looksLikeSignal(text) {
  if (typeof text !== 'string') return false;
  if (SIGNAL_HASHTAG_REGEX.test(text)) return true;
  return SIGNAL_HEADER_REGEX.test(text) || ALT_SIGNAL_HEADER_REGEX.test(text);
}

module.exports = {
  parseSignal,
  looksLikeSignal,
  POSITION_LINE_REGEX,
  _parsePositions,
};
