'use strict';

/**
 * R-AUTOCOPY — /signals command (subscribe to channel + copy auto link).
 */

const SIGNALS_USERNAME = (process.env.SIGNALS_CHANNEL || '@BlackCatDeFiSignals').replace(/^@/, '');

function _menuKeyboard() {
  return {
    inline_keyboard: [
      [{ text: '📲 Suscribirme al canal', url: `https://t.me/${SIGNALS_USERNAME}` }],
      [{ text: '🤖 Activar copy auto', callback_data: 'signals:goto_copyauto' }],
    ],
  };
}

function _bodyText() {
  const lines = [
    '📡 *Signals oficiales*',
    '',
    `Canal: @${SIGNALS_USERNAME}`,
    '',
    'Cuando hay signal nueva, te aviso al instante con:',
    '  • Composición de la basket',
    '  • Tokens y leverage',
    '  • Botón 1-toque para copiar en Pear',
    '  • SL 50% + Trailing 10% (activación 30%) — preset',
    '',
    'Ese aviso es independiente del canal — vos podés suscribirte al canal igual para ver el contexto completo.',
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
        await bot.sendMessage(chatId, 'Tocá /copy_auto para configurar.', { parse_mode: 'Markdown' });
      }
    }
  });

  console.log('[commandsSignals] attached: /signals');
}

module.exports = { attach, _bodyText, _menuKeyboard };
