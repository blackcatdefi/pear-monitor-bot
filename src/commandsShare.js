'use strict';

/**
 * R-AUTOCOPY — /share command.
 */

const share = require('./share');

function _body(userId) {
  const link = share.buildReferralLink(userId);
  const stats = share.getStats(userId);
  const remaining = Math.max(0, share.PREMIUM_THRESHOLD - stats.count);
  const lines = [
    '🎁 *Compartí el bot*',
    '',
    'Tu link único:',
    `\`${link}\``,
    '',
    'Cuando alguien se une con tu link:',
    '  • Vos sumás 1 referido',
    `  • Después de ${share.PREMIUM_THRESHOLD} referidos → Premium (${share.PREMIUM_SLOTS} slots vs ${share.DEFAULT_SLOTS})`,
    '',
    `Compartidos: ${stats.count}`,
    `Premium: ${stats.premium ? '✨ SÍ' : 'NO'}${remaining > 0 && !stats.premium ? ` (faltan ${remaining})` : ''}`,
  ];
  return lines.join('\n');
}

function _keyboard(userId) {
  const link = share.buildReferralLink(userId);
  const txt = encodeURIComponent(
    'Te recomiendo este bot de alertas para Pear Protocol — copy-trade en 1 toque:'
  );
  const tgShare = `https://t.me/share/url?url=${encodeURIComponent(link)}&text=${txt}`;
  return {
    inline_keyboard: [
      [{ text: '📤 Compartir en Telegram', url: tgShare }],
    ],
  };
}

function attach(bot) {
  bot.onText(/^\/share(?:@\w+)?$/i, async (msg) => {
    const chatId = msg.chat.id;
    const userId = msg.from && msg.from.id ? msg.from.id : chatId;
    try {
      await bot.sendMessage(chatId, _body(userId), {
        parse_mode: 'Markdown',
        reply_markup: _keyboard(userId),
        disable_web_page_preview: true,
      });
    } catch (e) {
      console.error('[commandsShare] failed:', e && e.message ? e.message : e);
    }
  });
  console.log('[commandsShare] attached: /share');
}

module.exports = { attach, _body, _keyboard };
