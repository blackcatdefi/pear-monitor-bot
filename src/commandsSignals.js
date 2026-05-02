'use strict';

/**
 * R-AUTOCOPY — /signals command (subscribe to channel + copy auto link).
 */

const SIGNALS_USERNAME = (process.env.SIGNALS_CHANNEL || '@BlackCatDeFiSignals').replace(/^@/, '');

function _menuKeyboard() {
  return {
    inline_keyboard: [
      [{ text: '📲 Subscribe to channel', url: `https://t.me/${SIGNALS_USERNAME}` }],
      [{ text: '🤖 Enable copy auto', callback_data: 'signals:goto_copyauto' }],
    ],
  };
}

function _bodyText() {
  const lines = [
    '📡 *Official signals*',
    '',
    `Channel: @${SIGNALS_USERNAME}`,
    '',
    'When a new signal lands, you get an instant alert with:',
    '  • Basket composition',
    '  • Tokens and leverage',
    '  • 1-tap "Copy on Pear" button',
    '  • SL 50% + Trailing 10% (activation 30%) — preset',
    '',
    'The alert is independent from the channel — feel free to subscribe to the channel for full context.',
  ];
  return lines.join('\n');
}

function attach(bot) {
  bot.onText(/^\/signals(?:@\w+)?$/i, async (msg) => {
    try {
      await bot.sendMessage(msg.chat.id, _bodyText(), {
        parse_mode: 'Markdown',
        reply_markup: _menuKeyboard(),
        disable_web_page_preview: true,
      });
    } catch (e) {
      console.error('[commandsSignals] /signals failed:', e && e.message ? e.message : e);
    }
  });

  bot.on('callback_query', async (cb) => {
    if (!cb.data || !cb.data.startsWith('signals:')) return;
    const chatId = cb.message && cb.message.chat ? cb.message.chat.id : null;
    if (!chatId) return;
    try { await bot.answerCallbackQuery(cb.id); } catch (_) {}
    if (cb.data === 'signals:goto_copyauto') {
      try {
        const commandsCopyAuto = require('./commandsCopyAuto');
        await commandsCopyAuto.showMenu(bot, chatId, cb.from && cb.from.id);
      } catch (_) {
        await bot.sendMessage(chatId, 'Tap /copy_auto to configure.', { parse_mode: 'Markdown' });
      }
    }
  });

  console.log('[commandsSignals] attached: /signals');
}

module.exports = { attach, _bodyText, _menuKeyboard };
