'use strict';

/**
 * R-AUTOCOPY-MENU — Unified alert template for the 3 copy-trading sources.
 *
 * Builds the message body + inline keyboard for copy-trading alerts. The
 * template is shared across:
 *
 *   • BCD_WALLET   — when 0xc7ae...1505 opens a basket (HL poller)
 *   • BCD_SIGNALS  — when @BlackCatDeFiSignals publishes a Pear URL
 *   • CUSTOM_WALLET — any user-added wallet that opens a basket
 *
 * The {source} label tells the user where the signal came from.
 *
 * Output:
 *   { text, keyboard }   — pass directly to bot.sendMessage(opts)
 */

const tzMgr = require('./timezoneManager');
const { buildPearUrlFromSides } = require('./signalsChannelParser');
const store = require('./copyTradingStore');

function _money(n) {
  if (!Number.isFinite(n)) return '$0';
  return `$${Math.round(n).toLocaleString()}`;
}

function _formatPositionLine(pos) {
  if (!pos) return '';
  const side = (pos.side || '').toUpperCase();
  return `  • ${pos.coin} ${side}`;
}

function _sourceLabel(source) {
  switch (source) {
    case 'BCD_WALLET':
      return 'BCD Wallet';
    case 'BCD_SIGNALS':
      return 'BCD Signals';
    case 'CUSTOM_WALLET':
      return 'Custom';
    default:
      return source || 'Signal';
  }
}

function _buildPearUrl({ pearUrl, longTokens, shortTokens, positions }) {
  if (pearUrl) return pearUrl;
  if (Array.isArray(longTokens) || Array.isArray(shortTokens)) {
    return buildPearUrlFromSides({ longTokens, shortTokens });
  }
  if (Array.isArray(positions)) {
    const longs = positions.filter((p) => p.side === 'LONG').map((p) => p.coin);
    const shorts = positions.filter((p) => p.side === 'SHORT').map((p) => p.coin);
    return buildPearUrlFromSides({ longTokens: longs, shortTokens: shorts });
  }
  return null;
}

/**
 * buildAlert(spec)
 *   spec: {
 *     source:       'BCD_WALLET' | 'BCD_SIGNALS' | 'CUSTOM_WALLET',
 *     userId:       number,
 *     capital:      number,            // user-configured capital_usdc
 *     mode:         'MANUAL' | 'AUTO',
 *     sourceLabel:  string?,           // override (e.g. custom wallet label)
 *     positions:    [{coin, side}],    // OR pearUrl pre-built OR side arrays
 *     pearUrl:      string?,
 *     longTokens:   string[]?,
 *     shortTokens:  string[]?,
 *     leverage:     number?,
 *     sl_pct:       number,
 *     trailing_pct: number,
 *     trailing_activation_pct: number,
 *     event:        'OPEN' | 'CLOSE'   // defaults to OPEN
 *   }
 *
 * Returns: { text, keyboard }
 */
function buildAlert(spec) {
  const event = (spec.event || 'OPEN').toUpperCase();
  const lines = [];
  const sourceLabel = spec.sourceLabel || _sourceLabel(spec.source);
  const cap = Math.max(0, Number(spec.capital) || 0);
  const sl = spec.sl_pct ?? 50;
  const trailing = spec.trailing_pct ?? 10;
  const trailingAct = spec.trailing_activation_pct ?? 30;

  // Resolve positions list for rendering. Accept either explicit positions[],
  // or sides arrays, or just a Pear URL (in which case we parse tokens from it).
  let positions = Array.isArray(spec.positions) ? spec.positions.slice() : null;
  if (!positions && (spec.longTokens || spec.shortTokens)) {
    positions = [];
    for (const t of spec.longTokens || []) positions.push({ coin: t, side: 'LONG' });
    for (const t of spec.shortTokens || []) positions.push({ coin: t, side: 'SHORT' });
  }
  if (!positions) positions = [];

  if (event === 'CLOSE') {
    lines.push(`✅ *BASKET CERRADA — ${sourceLabel}*`);
  } else {
    lines.push(`🚀 *NUEVA BASKET — ${sourceLabel}*`);
  }
  lines.push('');
  if (positions.length > 0) {
    lines.push(`📊 Composición (${positions.length}):`);
    for (const p of positions) lines.push(_formatPositionLine(p));
  }
  if (event === 'OPEN') {
    if (spec.leverage) {
      lines.push('');
      lines.push(`⚡ Leverage: ${spec.leverage}x · Notional sugerido: ${_money(cap)}`);
    } else if (cap > 0) {
      lines.push('');
      lines.push(`💰 Capital configurado: ${_money(cap)}`);
    }
    lines.push(
      `🎯 Risk: SL ${sl}% / Trailing ${trailing}% activación ${trailingAct}%`
    );
    if (spec.mode === 'AUTO') {
      lines.push('');
      lines.push(
        '_Mode AUTO — link pre-armado, vos firmás en tu wallet._'
      );
    } else {
      lines.push('');
      lines.push(
        '_Tocá el botón para abrir Pear con la basket pre-cargada._'
      );
    }
  } else {
    lines.push('');
    lines.push(`Source: ${sourceLabel}`);
  }
  lines.push('');
  lines.push(`🕐 ${tzMgr.formatLocalTime(spec.userId)}`);

  const keyboard = { inline_keyboard: [] };
  if (event === 'OPEN') {
    const pearUrl = _buildPearUrl(spec);
    if (pearUrl) {
      keyboard.inline_keyboard.push([
        { text: `🍐 Copiar en Pear (${_money(cap)})`, url: pearUrl },
      ]);
    }
    keyboard.inline_keyboard.push([
      { text: '⏭️ Skip', callback_data: 'copytrade:skip' },
      { text: '⚙️ Config', callback_data: 'copytrade:menu' },
    ]);
  } else {
    keyboard.inline_keyboard.push([
      { text: '⚙️ Config', callback_data: 'copytrade:menu' },
    ]);
  }

  return { text: lines.join('\n'), keyboard };
}

module.exports = {
  buildAlert,
  _sourceLabel,
  _buildPearUrl,
  _formatPositionLine,
  _money,
};
