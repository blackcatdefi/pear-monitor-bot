'use strict';

/**
 * R-AUTOCOPY — Copy-auto dispatcher.
 *
 * When a new signal is detected on @BlackCatDeFiSignals, the channel
 * listener calls dispatchSignal(signal). We:
 *   1. Read the list of users with copy_auto.enabled=1.
 *   2. For each user, render a Pear copy URL (positions × side) with
 *      referral=BlackCatDeFi (referral hidden in the URL — same pattern
 *      used by alertButtons.js).
 *   3. Format a MANUAL or AUTO message depending on user.mode.
 *   4. Send via the wrapped notifier passed in at startup.
 *
 * The actual copy execution happens in the user's wallet (Pear has no
 * public exec API). This module only delivers the pre-armed URL — user
 * signs in their wallet.
 */

const store = require('./copyAutoStore');
const pearUrl = require('./pearUrlBuilder');
const tzMgr = require('./timezoneManager');
const alertsConfig = require('./alertsConfig');

let _notify = null;

function attach(notify) {
  _notify = notify;
}

function _formatBasket(positions) {
  return positions.map((p) => `  • ${p.coin} ${p.side}`).join('\n');
}

function _splitSidesUrls(positions) {
  // pearUrlBuilder accepts {coin, side, notional} — give equal-weight notional
  // so all tokens make it through the sort + truncation.
  const padded = positions.map((p, idx) => ({
    coin: p.coin,
    side: p.side,
    notional: 1000 - idx, // descending so basket order is preserved
  }));
  return {
    shorts: pearUrl.buildPearCopyUrl(padded, 'SHORT'),
    longs: pearUrl.buildPearCopyUrl(padded, 'LONG'),
  };
}

function _heroButtons(positions, capital) {
  const { shorts, longs } = _splitSidesUrls(positions);
  const rows = [];
  const cap = `$${Math.round(Number(capital) || 0).toLocaleString()}`;
  if (shorts && longs) {
    rows.push([{ text: `🍐 Copy SHORTs (${cap})`, url: shorts }]);
    rows.push([{ text: `🍐 Copy LONGs (${cap})`, url: longs }]);
  } else if (shorts) {
    rows.push([{ text: `🍐 Copy on Pear (${cap})`, url: shorts }]);
  } else if (longs) {
    rows.push([{ text: `🍐 Copy on Pear (${cap})`, url: longs }]);
  }
  return rows;
}

function buildManualMessage(signal, cfg, userId) {
  const lines = [
    `🚀 *NEW OFFICIAL SIGNAL${signal.signal_id ? ` #${signal.signal_id}` : ''}*`,
    '',
    `📊 Basket (${signal.positions.length} tokens):`,
    _formatBasket(signal.positions),
    '',
    `💰 Your set capital: $${Math.round(cfg.capital_usdc)} USDC`,
  ];
  if (signal.leverage) lines.push(`⚡ Leverage: ${signal.leverage}x`);
  lines.push(`🎯 SL ${cfg.sl_pct}% / Trailing ${cfg.trailing_pct}% activation ${cfg.trailing_activation_pct}%`);
  if (signal.twap_hours || signal.twap_bullets) {
    const twapParts = [];
    if (signal.twap_hours) twapParts.push(`${signal.twap_hours}h`);
    if (signal.twap_bullets) twapParts.push(`${signal.twap_bullets} bullets`);
    lines.push(`⏱️ TWAP: ${twapParts.join(', ')}`);
  }
  lines.push('');
  lines.push('_Tap the button to open Pear with the basket pre-loaded. Sign in your wallet to execute._');
  lines.push('');
  lines.push(`🕐 ${tzMgr.formatLocalTime(userId)}`);
  return lines.join('\n');
}

function buildAutoMessage(signal, cfg, userId) {
  const lines = [
    `🤖 *COPY AUTO — Signal${signal.signal_id ? ` #${signal.signal_id}` : ''}*`,
    '',
    'Direct link is ready. Click + sign in your wallet:',
    '',
    `  • $${Math.round(cfg.capital_usdc)} USDC capital`,
    `  • ${signal.tokens.join('+')} ${[...new Set(signal.sides)].join('/')}`,
  ];
  if (signal.leverage) lines.push(`  • ${signal.leverage}x leverage cross`);
  lines.push(`  • SL ${cfg.sl_pct}% + Trailing ${cfg.trailing_pct}%`);
  lines.push('');
  lines.push('_Pear has no public execution API — you always sign from your own wallet (only legit way)._');
  lines.push('');
  lines.push(`🕐 ${tzMgr.formatLocalTime(userId)}`);
  return lines.join('\n');
}

function buildKeyboard(signal, cfg) {
  const rows = _heroButtons(signal.positions, cfg.capital_usdc);
  rows.push([
    { text: '⏭️ Skip', callback_data: `copyauto:skip:${signal.signal_id || 'x'}` },
    { text: '⚙️ Capital', callback_data: 'copyauto:menu' },
  ]);
  return { inline_keyboard: rows };
}

/**
 * Dispatch a signal to all enabled subscribers.
 *
 *   signal: returned by signalsParser.parseSignal()
 *
 * Returns count of users notified.
 */
async function dispatchSignal(signal) {
  if (!signal || !Array.isArray(signal.positions) || signal.positions.length === 0) return 0;
  if (typeof _notify !== 'function') {
    console.warn('[copyAuto] dispatchSignal called before attach() — skipping');
    return 0;
  }
  const enabled = store.listEnabledUsers();
  let count = 0;
  for (const { userId, config } of enabled) {
    // Per-user opt-in check — alertsConfig may have signals=false even if
    // copy_auto is enabled (user wants config but no inbound spam).
    if (!alertsConfig.isAllowed(userId, 'signals')) continue;
    const isAuto = config.mode === 'AUTO';
    const text = isAuto
      ? buildAutoMessage(signal, config, userId)
      : buildManualMessage(signal, config, userId);
    const keyboard = buildKeyboard(signal, config);
    try {
      await _notify(parseInt(userId, 10), text, {
        parse_mode: 'Markdown',
        reply_markup: keyboard,
        disable_web_page_preview: true,
      });
      count += 1;
    } catch (e) {
      console.error('[copyAuto] notify failed for', userId, e && e.message ? e.message : e);
    }
  }
  return count;
}

module.exports = {
  attach,
  dispatchSignal,
  buildManualMessage,
  buildAutoMessage,
  buildKeyboard,
  _heroButtons,
  _splitSidesUrls,
  _formatBasket,
};
