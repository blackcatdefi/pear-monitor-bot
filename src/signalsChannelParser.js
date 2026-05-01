'use strict';

/**
 * R-AUTOCOPY-MENU — Pear URL parser.
 *
 * Pear basket URLs have one of these shapes:
 *
 *   https://app.pear.garden/trade/hl/USDC-WLD+STRK+ENA+TIA+ARB?referral=BlackCatDeFi
 *     → SHORT basket (collateral=USDC, all RHS tokens are SHORT)
 *
 *   https://app.pear.garden/trade/hl/HYPE+LIT-WLFI?referral=BlackCatDeFi
 *     → LONG HYPE+LIT, SHORT WLFI (LHS tokens LONG, RHS tokens SHORT)
 *
 *   https://app.pear.garden/trade/hl/BLUR+DYDX+ARB-USDC?referral=...
 *     → LONG basket vs USDC collateral on right
 *
 * Parsing rules:
 *   1. Take the path segment after /trade/hl/ → strip query string
 *   2. Split on first '-' into LHS / RHS
 *   3. If LHS is "USDC" → LHS=collateral, RHS tokens are SHORT
 *   4. If RHS is "USDC" → RHS=collateral, LHS tokens are LONG
 *   5. Otherwise: LHS tokens are LONG, RHS tokens are SHORT
 *   6. Tokens are A-Z0-9 separated by +
 *
 * The parser ALWAYS returns a normalized URL with referral=BlackCatDeFi.
 *
 * Returns null when the URL doesn't match the expected shape.
 */

const PEAR_REFERRAL_CODE = process.env.PEAR_REFERRAL_CODE || 'BlackCatDeFi';
const PEAR_BASE_URL = process.env.PEAR_BASE_URL || 'https://app.pear.garden';

// e.g. https://app.pear.garden/trade/hl/USDC-WLD+STRK?referral=BlackCatDeFi
const PEAR_URL_RX =
  /^(https?:\/\/app\.pear\.garden\/trade\/hl\/)([A-Z0-9+\-]+)(\?[^#]*)?$/i;

const TOKEN_RX = /^[A-Z0-9]{1,12}$/;
const COLLATERAL_TOKENS = new Set(['USDC', 'USDC.E', 'USDT', 'USDT0', 'USDH', 'USDE', 'UETH']);

function _splitTokens(part) {
  if (!part) return [];
  return part
    .toUpperCase()
    .split('+')
    .map((s) => s.trim())
    .filter((s) => TOKEN_RX.test(s));
}

/**
 * Pure parse: returns
 *   { tokens, longTokens, shortTokens, collateral, urlWithReferral, raw }
 * or null if the URL doesn't match.
 */
function extractFromPearUrl(url) {
  if (typeof url !== 'string') return null;
  const trimmed = url.trim();
  const match = trimmed.match(PEAR_URL_RX);
  if (!match) return null;

  const basePath = match[1];
  const symbols = match[2];

  // Split on first '-' boundary that separates collateral or sides.
  // Robust against URLs that have no '-' (single side basket — unusual but
  // handle it as long-only).
  let lhs;
  let rhs;
  if (symbols.indexOf('-') >= 0) {
    const idx = symbols.indexOf('-');
    lhs = symbols.slice(0, idx);
    rhs = symbols.slice(idx + 1);
  } else {
    lhs = symbols;
    rhs = '';
  }

  const lhsTokens = _splitTokens(lhs);
  const rhsTokens = _splitTokens(rhs);

  let longTokens = [];
  let shortTokens = [];
  let collateral = null;

  if (lhsTokens.length === 1 && COLLATERAL_TOKENS.has(lhsTokens[0])) {
    collateral = lhsTokens[0];
    shortTokens = rhsTokens;
  } else if (rhsTokens.length === 1 && COLLATERAL_TOKENS.has(rhsTokens[0])) {
    collateral = rhsTokens[0];
    longTokens = lhsTokens;
  } else {
    longTokens = lhsTokens;
    shortTokens = rhsTokens;
  }

  // Filter out collateral tokens from sides if they leaked through
  longTokens = longTokens.filter((t) => !COLLATERAL_TOKENS.has(t));
  shortTokens = shortTokens.filter((t) => !COLLATERAL_TOKENS.has(t));

  const tokens = [...longTokens, ...shortTokens];
  if (tokens.length === 0) return null;

  // Re-render URL forcing referral=BlackCatDeFi (overwrites any incoming
  // referral param — we never want a competitor referral propagating).
  const urlWithReferral = `${basePath}${symbols}?referral=${encodeURIComponent(PEAR_REFERRAL_CODE)}`;

  return {
    tokens,
    longTokens,
    shortTokens,
    collateral,
    urlWithReferral,
    raw: trimmed,
  };
}

/**
 * Inverse: build a Pear URL from explicit token sides.
 * Used by bcdWalletPoller / customWallet dispatcher when generating the
 * URL from on-chain HL positions.
 */
function buildPearUrlFromSides({ longTokens = [], shortTokens = [], collateral = 'USDC' } = {}) {
  const longs = (longTokens || [])
    .map((t) => String(t).toUpperCase())
    .filter((t) => TOKEN_RX.test(t));
  const shorts = (shortTokens || [])
    .map((t) => String(t).toUpperCase())
    .filter((t) => TOKEN_RX.test(t));
  if (longs.length === 0 && shorts.length === 0) return null;

  let lhs = '';
  let rhs = '';
  if (longs.length === 0) {
    lhs = collateral || 'USDC';
    rhs = shorts.join('+');
  } else if (shorts.length === 0) {
    lhs = longs.join('+');
    rhs = collateral || 'USDC';
  } else {
    lhs = longs.join('+');
    rhs = shorts.join('+');
  }
  return `${PEAR_BASE_URL}/trade/hl/${lhs}-${rhs}?referral=${encodeURIComponent(
    PEAR_REFERRAL_CODE
  )}`;
}

module.exports = {
  PEAR_REFERRAL_CODE,
  PEAR_BASE_URL,
  PEAR_URL_RX,
  COLLATERAL_TOKENS: Array.from(COLLATERAL_TOKENS),
  extractFromPearUrl,
  buildPearUrlFromSides,
  _splitTokens,
};
