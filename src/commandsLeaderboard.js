'use strict';

/**
 * R-AUTOCOPY — /leaderboard command.
 */

const lb = require('./leaderboard');
const wt = require('./walletTracker');

async function _show(bot, chatId, userId) {
  const list = lb.getLeaderboard();
  const text = lb.formatLeaderboard(list);
  const kb = lb.buildKeyboard(list);
  await bot.sendMessage(chatId, text, {
    parse_mode: 'Markdown',
    reply_markup: kb || undefined,
  });
}

async function _handleCallback(bot, cb) {
  if (!cb.data || !cb.data.startsWith('lb:')) return;
  const chatId = cb.message && cb.message.chat ? cb.message.chat.id : null;
  const userId = cb.from && cb.from.id ? cb.from.id : chatId;
  if (!chatId) return;
  try { await bot.answerCallbackQuery(cb.id); } catch (_) {}
  const parts = cb.data.split(':');
  if (parts[1] === 'track' && parts[2]) {
    const fullAddr = lb.resolveAddressByPrefix(parts[2]);
    if (!fullAddr) {
      await bot.sendMessage(chatId, '⚠️ Could not resolve that wallet from the ranking.');
      return;
    }
    if (wt.hasWallet(userId, fullAddr)) {
      await bot.sendMessage(chatId, 'ℹ️ You already track that wallet.');
      return;
    }
    try {
      wt.addWallet(userId, fullAddr, null);
      await bot.sendMessage(
        chatId,
        `✅ Wallet \`${fullAddr.slice(0, 6)}...${fullAddr.slice(-4)}\` added to your /track.`,
        { parse_mode: 'Markdown' }
      );
    } catch (e) {
      await bot.sendMessage(chatId, `⚠️ ${e.message}`);
    }
  }
}

function attach(bot) {
  bot.onText(/^\/leaderboard(?:@\w+)?$/i, async (msg) => {
    try { await _show(bot, msg.chat.id, msg.from && msg.from.id); }
    catch (e) {
      console.error('[commandsLeaderboard] failed:', e && e.message ? e.message : e);
    }
  });

  bot.on('callback_query', async (cb) => {
    if (!cb.data || !cb.data.startsWith('lb:')) return;
    try { await _handleCallback(bot, cb); }
    catch (e) {
      console.error('[commandsLeaderboard] cb failed:', e && e.message ? e.message : e);
    }
  });

  console.log('[commandsLeaderboard] attached: /leaderboard');
}

module.exports = { attach, _show, _handleCallback };
