'use strict';

/**
 * R-AUTOCOPY — /feedback command.
 *
 * Captures the next plain-text message after /feedback and forwards it to
 * OWNER_USER_ID via the bot's wrapped notifier. /cancel exits.
 */

const feedback = require('./feedback');
const stats = require('./stats');
const sm = require('./userStateMachine');

const STATE_AWAITING_FEEDBACK = 'AWAITING_FEEDBACK';
if (!sm.STATES[STATE_AWAITING_FEEDBACK]) {
  sm.STATES[STATE_AWAITING_FEEDBACK] = STATE_AWAITING_FEEDBACK;
}

function _isAwaiting(chatId) {
  const st = sm.getState(chatId);
  return st && st.state === STATE_AWAITING_FEEDBACK;
}

function attach(bot, getNotify) {
  bot.onText(/^\/feedback(?:@\w+)?$/i, async (msg) => {
    const chatId = msg.chat.id;
    if (!feedback.ownerConfigured()) {
      await bot.sendMessage(
        chatId,
        '⚠️ Feedback temporarily unavailable (owner not configured). Try again later.'
      );
      return;
    }
    sm.setState(chatId, STATE_AWAITING_FEEDBACK, {});
    await bot.sendMessage(
      chatId,
      '💬 *Send feedback*\n\nWrite your message. It goes straight to me.\n\n_(type /cancel to go back)_',
      { parse_mode: 'Markdown' }
    );
  });

  bot.on('message', async (msg) => {
    if (!msg.text) return;
    if (!_isAwaiting(msg.chat.id)) return;
    if (msg.text.startsWith('/')) {
      // /cancel handled below; any other slash-cmd also exits state
      sm.reset(msg.chat.id);
      if (/^\/cancel/i.test(msg.text)) {
        await bot.sendMessage(msg.chat.id, '✖️ Cancelled.');
      }
      return;
    }
    sm.reset(msg.chat.id);
    const notify = typeof getNotify === 'function' ? getNotify() : null;
    if (typeof notify !== 'function') {
      await bot.sendMessage(
        msg.chat.id,
        '⚠️ Could not forward your feedback right now (notifier not ready). Try again later.'
      );
      return;
    }
    const r = await feedback.forwardFeedback({
      notify,
      fromUserId: msg.from && msg.from.id,
      fromUsername: msg.from && msg.from.username,
      text: msg.text,
    });
    if (r && r.ok) {
      stats.incrementFeedback(msg.from && msg.from.id);
      await bot.sendMessage(msg.chat.id, '✅ Feedback received. Thanks.');
    } else {
      await bot.sendMessage(
        msg.chat.id,
        `⚠️ Could not forward your feedback (${r && r.error ? r.error : 'error'}).`
      );
    }
  });

  console.log('[commandsFeedback] attached: /feedback');
}

module.exports = { attach, STATE_AWAITING_FEEDBACK, _isAwaiting };
