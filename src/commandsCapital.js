'use strict';

/**
 * R-AUTOCOPY — /capital <amount> command.
 *
 *   /capital            → shows current value + min/max
 *   /capital 500        → sets to $500 USDC
 */

const store = require('./copyAutoStore');

function _bodyShow(cfg) {
  return [
    '💰 *Capital per signal*',
    '',
    `Current: $${Math.round(cfg.capital_usdc).toLocaleString()} USDC`,
    `Allowed range: $${store.MIN_CAPITAL} – $${store.MAX_CAPITAL.toLocaleString()}`,
    '',
    'To change: `/capital <amount>` (e.g. `/capital 500`)',
  ].join('\n');
}

function _bodyConfirm(amount) {
  return `✅ Capital set: $${Math.round(amount).toLocaleString()} USDC.\n\n_Will be applied on the next signals._`;
}

function _bodyError(msg) {
  return `⚠️ ${msg}\n\nUsage: \`/capital <amount>\` (e.g. \`/capital 500\`)`;
}

async function handle(bot, msg) {
  const chatId = msg.chat.id;
  const userId = msg.from && msg.from.id ? msg.from.id : chatId;
  const m = (msg.text || '').match(/^\/capital(?:@\w+)?\s*(.*)$/i);
  const arg = m ? (m[1] || '').trim() : '';
  if (!arg) {
    const cfg = store.getConfig(userId);
    await bot.sendMessage(chatId, _bodyShow(cfg), { parse_mode: 'Markdown' });
    return;
  }
  try {
    const cfg = store.setCapital(userId, arg.replace(/[\$,\s]/g, ''));
    await bot.sendMessage(chatId, _bodyConfirm(cfg.capital_usdc), { parse_mode: 'Markdown' });
  } catch (e) {
    await bot.sendMessage(chatId, _bodyError(e.message || 'Invalid amount'), { parse_mode: 'Markdown' });
  }
}

function attach(bot) {
  bot.onText(/^\/capital(?:@\w+)?(?:\s|$)/i, async (msg) => {
    try { await handle(bot, msg); }
    catch (e) {
      console.error('[commandsCapital] /capital failed:', e && e.message ? e.message : e);
    }
  });
  console.log('[commandsCapital] attached: /capital');
}

module.exports = { attach, handle, _bodyShow, _bodyConfirm, _bodyError };
