'use strict';

/**
 * R-PUBLIC-FUNDS — /funds + /fundsalert commands.
 *
 *   /funds                → deployable-capital view for every wallet the user
 *                           tracks via /track (any HL account type, PM incl.)
 *   /funds 0x…            → view for an explicit address
 *   /fundsalert           → show current opt-in status
 *   /fundsalert 500       → opt in at $500 threshold
 *   /fundsalert on        → opt in at the default ($500)
 *   /fundsalert off       → opt out
 *
 * The alert itself is dispatched by fundsAlertScheduler; this module only
 * manages opt-in state and the on-demand view. All strings English
 * (R-EN rule). Branding footer identical to existing alerts.
 */

const walletTracker = require('./walletTracker');
const fundsEngine = require('./fundsEngine');
const fundsAlertStore = require('./fundsAlertStore');
const { appendFooter } = require('./branding');

const MAX_WALLETS_RENDERED = 5;

function _walletsFor(userId) {
  return walletTracker.getUserWallets(userId).map((w) => w.address);
}

// ───────────────────────────── /funds ─────────────────────────────

async function handleFunds(bot, msg) {
  const chatId = msg.chat.id;
  const userId = msg.from && msg.from.id ? msg.from.id : chatId;
  const m = (msg.text || '').match(/^\/funds(?:@\w+)?\s*(.*)$/i);
  const arg = m ? (m[1] || '').trim() : '';

  let wallets;
  if (arg) {
    if (!walletTracker.isValidAddress(arg)) {
      await bot.sendMessage(
        chatId,
        '⚠️ Invalid address — must be 0x + 40 hex chars.\n\nUsage: `/funds` (your tracked wallets) or `/funds 0x…`',
        { parse_mode: 'Markdown' }
      );
      return;
    }
    wallets = [arg];
  } else {
    wallets = _walletsFor(userId);
    if (wallets.length === 0) {
      await bot.sendMessage(
        chatId,
        '👛 No wallet registered yet.\n\nAdd one with /track, or run `/funds 0x…` for any address.',
        { parse_mode: 'Markdown' }
      );
      return;
    }
  }

  const chunks = ['💰 *DEPLOYABLE CAPITAL*', ''];
  for (const w of wallets.slice(0, MAX_WALLETS_RENDERED)) {
    let view;
    try {
      view = await fundsEngine.getDeployableView(w);
    } catch (e) {
      view = { error: true };
    }
    chunks.push(...fundsEngine.formatDeployableView(view, w), '');
  }
  if (wallets.length > MAX_WALLETS_RENDERED) {
    chunks.push(`_…and ${wallets.length - MAX_WALLETS_RENDERED} more — run /funds 0x… for a specific one._`);
  }
  const cfg = fundsAlertStore.getConfig(userId);
  chunks.push(
    cfg && cfg.enabled
      ? `🔔 Funds alert: ON at $${Math.round(cfg.threshold).toLocaleString('en-US')} (change: \`/fundsalert <usd>\`, stop: \`/fundsalert off\`)`
      : '🔕 Funds alert: OFF — get pinged when capital frees up: `/fundsalert 500`'
  );
  await bot.sendMessage(chatId, appendFooter(chunks.join('\n')), {
    parse_mode: 'Markdown',
  });
}

// ───────────────────────────── /fundsalert ─────────────────────────────

function _statusBody(cfg, walletCount) {
  if (cfg && cfg.enabled) {
    return [
      '🔔 *Funds alert: ON*',
      '',
      `Threshold: $${Math.round(cfg.threshold).toLocaleString('en-US')}`,
      `Wallets watched: ${walletCount} (managed via /track)`,
      '',
      'Fires when total deployable capital — or PM borrow headroom on Portfolio Margin accounts — crosses your threshold. Anti-spam: re-arms below 50% of threshold or after 12h.',
      '',
      'Change: `/fundsalert <usd>` · Stop: `/fundsalert off`',
    ].join('\n');
  }
  return [
    '🔕 *Funds alert: OFF*',
    '',
    'Get a ping the moment your wallet has capital ready to deploy (spot stables, perp withdrawable, or Portfolio Margin borrow headroom).',
    '',
    'Enable: `/fundsalert 500` (any USD threshold, default $500)',
  ].join('\n');
}

async function handleFundsAlert(bot, msg) {
  const chatId = msg.chat.id;
  const userId = msg.from && msg.from.id ? msg.from.id : chatId;
  const m = (msg.text || '').match(/^\/fundsalert(?:@\w+)?\s*(.*)$/i);
  const arg = m ? (m[1] || '').trim().toLowerCase() : '';

  if (!arg) {
    const cfg = fundsAlertStore.getConfig(userId);
    await bot.sendMessage(chatId, _statusBody(cfg, _walletsFor(userId).length), {
      parse_mode: 'Markdown',
    });
    return;
  }

  if (arg === 'off' || arg === 'stop' || arg === 'disable') {
    fundsAlertStore.optOut(userId);
    await bot.sendMessage(chatId, '🔕 Funds alert disabled. Re-enable anytime: `/fundsalert 500`', {
      parse_mode: 'Markdown',
    });
    return;
  }

  let threshold;
  if (arg === 'on' || arg === 'enable') {
    threshold = fundsAlertStore.DEFAULT_THRESHOLD_USD;
  } else {
    threshold = parseFloat(arg.replace(/[$,\s]/g, ''));
    if (!Number.isFinite(threshold)) {
      await bot.sendMessage(
        chatId,
        '⚠️ Could not parse that amount.\n\nUsage: `/fundsalert 500` · `/fundsalert off`',
        { parse_mode: 'Markdown' }
      );
      return;
    }
  }

  const wallets = _walletsFor(userId);
  if (wallets.length === 0) {
    await bot.sendMessage(
      chatId,
      '👛 Add a wallet first with /track — the funds alert watches your tracked wallets.',
      { parse_mode: 'Markdown' }
    );
    return;
  }

  try {
    const cfg = fundsAlertStore.optIn(userId, threshold);
    await bot.sendMessage(
      chatId,
      [
        `🔔 *Funds alert ON* at $${Math.round(cfg.threshold).toLocaleString('en-US')}`,
        '',
        `Watching ${wallets.length} wallet${wallets.length === 1 ? '' : 's'}. You will get a ping when deployable capital (or PM borrow headroom) crosses the threshold.`,
      ].join('\n'),
      { parse_mode: 'Markdown' }
    );
  } catch (e) {
    await bot.sendMessage(chatId, `⚠️ ${e.message}`, { parse_mode: 'Markdown' });
  }
}

// ───────────────────────────── attach ─────────────────────────────

function attach(bot) {
  bot.onText(/^\/funds(?:@\w+)?(?:\s|$)/i, async (msg) => {
    try { await handleFunds(bot, msg); }
    catch (e) {
      console.error('[commandsFunds] /funds failed:', e && e.message ? e.message : e);
    }
  });
  bot.onText(/^\/fundsalert(?:@\w+)?(?:\s|$)/i, async (msg) => {
    try { await handleFundsAlert(bot, msg); }
    catch (e) {
      console.error('[commandsFunds] /fundsalert failed:', e && e.message ? e.message : e);
    }
  });
  try {
    const hs = require('./healthServer');
    hs.registerHandler('funds');
    hs.registerHandler('fundsalert');
  } catch (_) {}
  console.log('[commandsFunds] attached: /funds /fundsalert');
}

module.exports = { attach, handleFunds, handleFundsAlert, _statusBody };
