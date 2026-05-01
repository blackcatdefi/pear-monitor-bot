'use strict';

const stats = require('./stats');

function attach(bot) {
  bot.onText(/^\/stats(?:@\w+)?$/i, async (msg) => {
    const chatId = msg.chat.id;
    const userId = msg.from && msg.from.id ? msg.from.id : chatId;
    try {
      stats.touch(userId);
      await bot.sendMessage(chatId, stats.formatStats(userId), { parse_mode: 'Markdown' });
    } catch (e) {
      console.error('[commandsStats] failed:', e && e.message ? e.message : e);
    }
  });
  console.log('[commandsStats] attached: /stats');
}

module.exports = { attach };
