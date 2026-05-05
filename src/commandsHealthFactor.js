'use strict';

/**
 * R-PUBLIC-V3-TRACKING — /hf address-input handler.
 *
 * The "🛡 MY HEALTH FACTOR" button on the simplified /start enters the
 * AWAITING_HF_ADDRESS state. This module registers a parallel
 * `bot.on('message')` listener that picks up the next plain-text reply
 * from a user in that state, validates the address, calls the
 * healthFactor reader, and sends back a Markdown-formatted HF card.
 *
 * Side-channel: also listens to `/hf <address>` for power-users who want
 * to skip the menu. Same code path, no state required.
 *
 * The handler is intentionally narrow: it only fires when the chat is
 * actually in the HF state — non-HF chats fall through cleanly so the
 * /track + portfolio handlers keep owning their own states.
 */

const sm = require('./userStateMachine');
const hf = require('./healthFactor');

const HF_CMD_REGEX = /^\/hf(?:@\w+)?(?:\s+(\S+))?\s*$/i;

function _toUserId(msgOrCb) {
  return (msgOrCb && msgOrCb.from && msgOrCb.from.id) ||
    (msgOrCb && msgOrCb.chat && msgOrCb.chat.id) ||
    null;
}

async function _runHfRead(bot, chatId, address) {
  let read;
  try {
    read = await hf.readWithCache(address);
  } catch (e) {
    read = {
      status: 'ERROR',
      error: (e && e.message) ? e.message : 'Unknown error',
    };
  }
  const txt = hf.formatHfMessage(address, read);
  await bot.sendMessage(chatId, txt, { parse_mode: 'Markdown' });
}

function attach(bot) {
  // /hf <address> — direct command path (bypasses state machine).
  bot.onText(HF_CMD_REGEX, async (msg, match) => {
    const chatId = msg.chat.id;
    const arg = (match && match[1]) ? match[1].trim() : '';
    if (!arg) {
      const userId = _toUserId(msg);
      sm.setState(chatId, sm.STATES.AWAITING_HF_ADDRESS, { userId });
      await bot.sendMessage(
        chatId,
        [
          '🛡 *Health Factor reader*',
          '',
          'Send the wallet address (`0x...`, 40 hex chars). /cancel to abort.',
        ].join('\n'),
        { parse_mode: 'Markdown' }
      );
      return;
    }
    if (!hf.isValidAddress(arg)) {
      await bot.sendMessage(
        chatId,
        'Invalid address — must be `0x` followed by 40 hex chars.',
        { parse_mode: 'Markdown' }
      );
      return;
    }
    await _runHfRead(bot, chatId, arg);
  });

  // Plain-text handler: fires only while the chat is in AWAITING_HF_ADDRESS.
  bot.on('message', async (msg) => {
    if (!msg || !msg.text) return;
    if (msg.text.startsWith('/')) return; // /cancel handled by commandsTrack
    const chatId = msg.chat.id;
    const rec = sm.getState(chatId);
    if (rec.state !== sm.STATES.AWAITING_HF_ADDRESS) return;

    const text = msg.text.trim();
    if (!hf.isValidAddress(text)) {
      await bot.sendMessage(
        chatId,
        'Invalid address — must be `0x` followed by 40 hex chars. Send /cancel to abort.',
        { parse_mode: 'Markdown' }
      );
      return;
    }
    sm.reset(chatId);
    await _runHfRead(bot, chatId, text);
  });

  console.log('[commandsHealthFactor] attached: /hf + AWAITING_HF_ADDRESS handler');
}

module.exports = { attach, _runHfRead };
