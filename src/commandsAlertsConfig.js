'use strict';

/**
 * R-AUTOCOPY — /alerts_config command.
 */

const ac = require('./alertsConfig');

const LABELS = {
  basket_open: 'New basket opened',
  basket_close: 'Basket closed (SL/TP)',
  signals: 'Official signals',
  compounding: 'Compounding detected',
  hf_critical: 'Critical HF on tracked wallets',
  daily_summary: 'Daily summary',
};

function _buildKeyboard(cfg) {
  const rows = [];
  for (const cat of ac.CATEGORIES) {
    const label = LABELS[cat] || cat;
    const checkmark = cfg[cat] ? '✅' : '☐';
    rows.push([
      { text: `${checkmark} ${label}`, callback_data: `ac:toggle:${cat}` },
    ]);
  }
  rows.push([{ text: '✖️ Close', callback_data: 'ac:close' }]);
  return { inline_keyboard: rows };
}

function _formatBody(cfg) {
  const lines = [
    '🔔 *Alert settings*',
    '',
    'Tap a category to toggle it:',
  ];
  return lines.join('\n');
}

async function _show(bot, chatId, userId) {
  const cfg = ac.getConfig(userId);
  await bot.sendMessage(chatId, _formatBody(cfg), {
    parse_mode: 'Markdown',
    reply_markup: _buildKeyboard(cfg),
  });
}

async function _handleCallback(bot, cb) {
  if (!cb.data || !cb.data.startsWith('ac:')) return;
  const chatId = cb.message && cb.message.chat ? cb.message.chat.id : null;
  const userId = cb.from && cb.from.id ? cb.from.id : chatId;
  if (!chatId) return;
  const parts = cb.data.split(':');
  try { await bot.answerCallbackQuery(cb.id); } catch (_) {}
  if (parts[1] === 'toggle' && parts[2]) {
    try {
      const cfg = ac.toggle(userId, parts[2]);
      try {
        await bot.editMessageReplyMarkup(_buildKeyboard(cfg), {
          chat_id: chatId,
          message_id: cb.message.message_id,
        });
      } catch (_) {
        // editMessageReplyMarkup throws if markup unchanged or message-not-found;
        // safe to ignore.
      }
    } catch (e) {
      await bot.sendMessage(chatId, `⚠️ ${e.message}`, { parse_mode: 'Markdown' });
    }
    return;
  }
  if (parts[1] === 'close') {
    try {
      await bot.deleteMessage(chatId, cb.message.message_id);
    } catch (_) {}
  }
}

function attach(bot) {
  bot.onText(/^\/alerts?_?config(?:@\w+)?$/i, async (msg) => {
    try { await _show(bot, msg.chat.id, msg.from && msg.from.id); }
    catch (e) {
      console.error('[commandsAlertsConfig] failed:', e && e.message ? e.message : e);
    }
  });

  bot.on('callback_query', async (cb) => {
    if (!cb.data || !cb.data.startsWith('ac:')) return;
    try { await _handleCallback(bot, cb); }
    catch (e) {
      console.error('[commandsAlertsConfig] cb failed:', e && e.message ? e.message : e);
    }
  });

  console.log('[commandsAlertsConfig] attached: /alerts_config');
}

module.exports = { attach, _show, _buildKeyboard, _formatBody, _handleCallback, LABELS };
