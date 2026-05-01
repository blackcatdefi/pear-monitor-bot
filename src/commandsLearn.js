'use strict';

const learn = require('./learn');

async function _showIndex(bot, chatId) {
  await bot.sendMessage(chatId, learn.formatIndex(), {
    parse_mode: 'Markdown',
    reply_markup: learn.buildIndexKeyboard(),
  });
}

async function _showLesson(bot, chatId, idx, messageIdToEdit) {
  const text = learn.formatLesson(idx);
  const kb = learn.buildKeyboard(idx);
  if (messageIdToEdit) {
    try {
      await bot.editMessageText(text, {
        chat_id: chatId,
        message_id: messageIdToEdit,
        parse_mode: 'Markdown',
        reply_markup: kb,
      });
      return;
    } catch (_) {
      // Fall through to send-new on edit failure (e.g. message too old)
    }
  }
  await bot.sendMessage(chatId, text, {
    parse_mode: 'Markdown',
    reply_markup: kb,
  });
}

async function _handleCallback(bot, cb) {
  if (!cb.data || !cb.data.startsWith('learn:')) return;
  const chatId = cb.message && cb.message.chat ? cb.message.chat.id : null;
  if (!chatId) return;
  try { await bot.answerCallbackQuery(cb.id); } catch (_) {}
  const parts = cb.data.split(':');
  if (parts[1] === 'nav' && parts[2]) {
    const idx = parseInt(parts[2], 10);
    if (Number.isFinite(idx)) {
      await _showLesson(bot, chatId, idx, cb.message.message_id);
    }
  } else if (parts[1] === 'exit') {
    try { await bot.deleteMessage(chatId, cb.message.message_id); } catch (_) {}
  }
}

function attach(bot) {
  bot.onText(/^\/learn(?:@\w+)?$/i, async (msg) => {
    try { await _showIndex(bot, msg.chat.id); }
    catch (e) {
      console.error('[commandsLearn] failed:', e && e.message ? e.message : e);
    }
  });

  bot.on('callback_query', async (cb) => {
    if (!cb.data || !cb.data.startsWith('learn:')) return;
    try { await _handleCallback(bot, cb); }
    catch (e) {
      console.error('[commandsLearn] cb failed:', e && e.message ? e.message : e);
    }
  });

  console.log('[commandsLearn] attached: /learn');
}

module.exports = { attach, _showIndex, _showLesson, _handleCallback };
