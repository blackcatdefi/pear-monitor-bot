'use strict';

/**
 * R-AUTOCOPY-MENU + R-CTAOPTIMIZE — Unified alert template for the 3
 * copy-trading sources.
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
 * R-CTAOPTIMIZE additions (OPEN events):
 *   - hero CTA label includes pre-fill amount: "🍐 Copiar $500 en Pear"
 *   - hero URL carries pre-fill (?amount=…) + anonymized utm tracking
 *   - quick-amount row [0.5x · 1x · 2x] when capital is configured
 *   - source-aware utm_source ('tg-alert-bcd-wallet' / 'tg-signal-channel' /
 *     'tg-alert-custom') so we can attribute conversions per source.
 *
 * Output:
 *   { text, keyboard }   — pass directly to bot.sendMessage(opts)
 */

const tzMgr = require('./timezoneManager');
const { buildPearUrlFromSides } = require('./signalsChannelParser');
const store = require('./copyTradingStore');
const pearUrl = require('./pearUrlBuilder');
const buttons = require('./alertButtons');

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

function _buildPearUrl(spec) {
  // Backward-compatible fallback used when no pre-fill / utm is required.
  // Prefer _buildPearUrlWithOpts for OPEN alerts.
  const { pearUrl: pre, longTokens, shortTokens, positions } = spec || {};
  if (pre) return pre;
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

// R-CTAOPTIMIZE — map the alert source to a UTM-friendly identifier.
function _utmSource(source) {
  switch (source) {
    case 'BCD_WALLET':
      return 'tg-alert-bcd-wallet';
    case 'BCD_SIGNALS':
      return 'tg-signal-channel';
    case 'CUSTOM_WALLET':
      return 'tg-alert-custom';
    default:
      return 'tg-alert';
  }
}

// R-CTAOPTIMIZE — build the Pear URL for a given side, carrying capital
// pre-fill + UTM tracking. Returns null if no positions match the side.
function _ctaUrlForSide(positions, side, opts) {
  const o = opts || {};
  if (!Array.isArray(positions) || positions.length === 0) return null;
  const cap = Number.isFinite(Number(o.capital)) && Number(o.capital) > 0
    ? Number(o.capital)
    : null;
  const ctaOpts = {};
  if (cap) ctaOpts.capital = cap;
  if (Number.isFinite(Number(o.leverage)) && Number(o.leverage) > 0) {
    ctaOpts.leverage = Number(o.leverage);
  }
  if (o.userId !== undefined && o.userId !== null && o.userId !== '') {
    ctaOpts.userId = o.userId;
  }
  if (o.source) ctaOpts.source = _utmSource(o.source);
  return pearUrl.buildPearCopyUrl(positions, side, ctaOpts);
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
    lines.push(`✅ *BASKET CLOSED — ${sourceLabel}*`);
  } else {
    lines.push(`🚀 *NEW BASKET — ${sourceLabel}*`);
  }
  lines.push('');
  if (positions.length > 0) {
    lines.push(`📊 Composition (${positions.length}):`);
    for (const p of positions) lines.push(_formatPositionLine(p));
  }
  if (event === 'OPEN') {
    if (spec.leverage) {
      lines.push('');
      lines.push(`⚡ Leverage: ${spec.leverage}x · Suggested notional: ${_money(cap)}`);
    } else if (cap > 0) {
      lines.push('');
      lines.push(`💰 Capital set: ${_money(cap)}`);
    }
    lines.push(
      `🎯 Risk: SL ${sl}% / Trailing ${trailing}% activation ${trailingAct}%`
    );
    if (spec.mode === 'AUTO') {
      lines.push('');
      lines.push(
        '_AUTO mode — link pre-armed, you sign from your wallet._'
      );
    } else {
      lines.push('');
      lines.push(
        '_Tap the button to open Pear with the basket pre-loaded._'
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
    // R-CTAOPTIMIZE — capital-aware hero rows + optional quick-amount row.
    // We try to build per-side URLs first (with pre-fill + UTM); fall back to
    // the legacy single URL if those return null (e.g. mixed-side basket
    // pulled from a pre-built pearUrl string).
    const ctaOpts = {
      capital: cap,
      leverage: spec.leverage,
      userId: spec.userId,
      source: spec.source,
    };
    const shortsUrl = _ctaUrlForSide(positions, 'SHORT', ctaOpts);
    const longsUrl = _ctaUrlForSide(positions, 'LONG', ctaOpts);
    const capLabel = buttons.formatAmount(cap);

    const heroLabelFor = (side) => {
      const tag = side === 'SHORT' ? 'SHORTs' : 'LONGs';
      if (capLabel) return `🍐 Copy ${tag} ${capLabel} on Pear`;
      return `🍐 Copy ${tag} on Pear`;
    };
    const heroLabelSingle = capLabel
      ? `🍐 Copy ${capLabel} on Pear`
      : '🍐 Copy on Pear';

    if (shortsUrl && longsUrl) {
      keyboard.inline_keyboard.push([
        { text: heroLabelFor('SHORT'), url: shortsUrl },
      ]);
      keyboard.inline_keyboard.push([
        { text: heroLabelFor('LONG'), url: longsUrl },
      ]);
    } else if (shortsUrl || longsUrl) {
      const url = shortsUrl || longsUrl;
      const side = shortsUrl ? 'SHORT' : 'LONG';
      keyboard.inline_keyboard.push([{ text: heroLabelSingle, url }]);

      // Quick-amount row — only when capital known + single-side basket.
      if (cap > 0 && buttons.QUICK_AMOUNT_MULTIPLIERS.length > 0
        && (spec.showQuickAmounts !== false)
        && buttons.QUICK_AMOUNTS_ENABLED) {
        const quickRow = [];
        for (const m of buttons.QUICK_AMOUNT_MULTIPLIERS) {
          const amt = buttons._roundQuickAmount(cap * m);
          if (amt <= 0) continue;
          const url2 = pearUrl.buildPearCopyUrl(positions, side, {
            ...buttons._ctaOpts({
              leverage: spec.leverage,
              userId: spec.userId,
              source: _utmSource(spec.source),
            }),
            capital: amt,
          });
          if (!url2) continue;
          const mLabel = m === Math.floor(m)
            ? `${m}x`
            : `${m.toFixed(1).replace(/\.0$/, '')}x`;
          quickRow.push({
            text: `${mLabel} (${buttons.formatAmount(amt)})`,
            url: url2,
          });
        }
        if (quickRow.length > 0) keyboard.inline_keyboard.push(quickRow);
      }
    } else {
      // Fallback: pre-built Pear URL passed in spec, or buildPearUrlFromSides
      const fallbackUrl = _buildPearUrl(spec);
      if (fallbackUrl) {
        keyboard.inline_keyboard.push([
          { text: capLabel ? `🍐 Copy ${capLabel} on Pear` : '🍐 Copy on Pear', url: fallbackUrl },
        ]);
      }
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
  _ctaUrlForSide,
  _utmSource,
  _formatPositionLine,
  _money,
};
