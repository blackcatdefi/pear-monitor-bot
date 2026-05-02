'use strict';

/**
 * R-AUTOCOPY — Read-only portfolio fetcher.
 *
 * Public users connect a wallet *read-only* by pasting their address. We
 * call HyperLiquid's public `/info` endpoint with `clearinghouseState` to
 * pull equity + open positions. No keys, no signing — purely read access.
 *
 * Public surface:
 *   fetchPortfolio(address)  → { equity, marginUsed, freeCollateral, positions:[...] }
 *   formatPortfolio(p, label) → Telegram-ready Markdown body
 *
 * Returns { error: '...' } shape on transport failure (caller renders the
 * error inline).
 */

const axios = require('axios');

const ADDRESS_REGEX = /^0x[a-fA-F0-9]{40}$/;
const HL_API = process.env.HYPERLIQUID_API_URL || 'https://api.hyperliquid.xyz';

function isValidAddress(addr) {
  return typeof addr === 'string' && ADDRESS_REGEX.test(addr.trim());
}

function _shortAddr(a) {
  if (!a) return '?';
  const s = String(a);
  return `${s.slice(0, 6)}...${s.slice(-4)}`;
}

function _fmtUsd(n) {
  if (!Number.isFinite(n)) return '$0';
  const abs = Math.abs(n);
  const sign = n < 0 ? '-' : '';
  if (abs >= 1000000) return `${sign}$${(abs / 1000000).toFixed(2)}M`;
  if (abs >= 1000) return `${sign}$${Math.round(abs).toLocaleString()}`;
  return `${sign}$${abs.toFixed(2)}`;
}

/**
 * Fetch a user's HyperLiquid clearinghouse state. Returns:
 *   {
 *     ok: true,
 *     equity, marginUsed, freeCollateral,
 *     positions: [{coin, side, size, entryPx, notional, leverage, upnl}]
 *   }
 * or
 *   { ok: false, error: 'message' }
 */
async function fetchPortfolio(address, opts) {
  const o = opts || {};
  if (!isValidAddress(address)) {
    return { ok: false, error: 'Invalid address (must be 0x + 40 hex chars)' };
  }
  try {
    const url = `${HL_API}/info`;
    const { data } = await axios.post(
      url,
      { type: 'clearinghouseState', user: address },
      { timeout: o.timeoutMs || 12000 }
    );
    if (!data || typeof data !== 'object') {
      return { ok: false, error: 'Empty response from HyperLiquid' };
    }
    const ms = data.marginSummary || {};
    const equity = parseFloat(ms.accountValue || '0');
    const marginUsed = parseFloat(ms.totalMarginUsed || '0');
    const freeCollateral = equity - marginUsed;
    const positions = Array.isArray(data.assetPositions)
      ? data.assetPositions
          .map((ap) => {
            const p = ap && ap.position;
            if (!p) return null;
            const size = parseFloat(p.szi || '0');
            if (!Number.isFinite(size) || size === 0) return null;
            return {
              coin: p.coin,
              side: size > 0 ? 'LONG' : 'SHORT',
              size: Math.abs(size),
              entryPx: parseFloat(p.entryPx || '0'),
              notional: parseFloat(p.positionValue || '0'),
              leverage: p.leverage && p.leverage.value
                ? parseInt(p.leverage.value, 10)
                : null,
              upnl: parseFloat(p.unrealizedPnl || '0'),
            };
          })
          .filter(Boolean)
      : [];
    return {
      ok: true,
      address,
      equity,
      marginUsed,
      freeCollateral,
      positions,
    };
  } catch (e) {
    const msg = e && e.message ? e.message : 'fetch failed';
    return { ok: false, error: `HyperLiquid: ${msg}` };
  }
}

/**
 * Render a portfolio object into Telegram-ready Markdown.
 */
function formatPortfolio(p, label) {
  if (!p) return '⚠️ No data.';
  if (p.ok === false) return `⚠️ ${p.error || 'Unknown error'}`;
  const lines = [
    `📊 *Your portfolio*`,
    '',
    `Wallet: \`${_shortAddr(p.address)}\`${label ? ` — ${label}` : ''}`,
    '',
    `🏦 Equity: ${_fmtUsd(p.equity)}`,
    `📉 Margin used: ${_fmtUsd(p.marginUsed)}`,
    `💰 Free collateral: ${_fmtUsd(p.freeCollateral)}`,
    '',
    `📊 Active positions: ${p.positions.length}`,
  ];
  if (p.positions.length > 0) {
    lines.push('');
    let totalUpnl = 0;
    for (const pos of p.positions) {
      totalUpnl += pos.upnl || 0;
      const upnlStr = (pos.upnl >= 0 ? '+' : '') + _fmtUsd(pos.upnl);
      lines.push(
        `  • *${pos.coin}* ${pos.side} ${_fmtUsd(pos.notional)} → ${upnlStr}`
      );
    }
    lines.push('');
    lines.push(`💵 Unrealized PnL: ${(totalUpnl >= 0 ? '+' : '')}${_fmtUsd(totalUpnl)}`);
  } else {
    lines.push('');
    lines.push('_No open positions right now._');
  }
  return lines.join('\n');
}

module.exports = {
  ADDRESS_REGEX,
  isValidAddress,
  fetchPortfolio,
  formatPortfolio,
  _fmtUsd,
  _shortAddr,
};
