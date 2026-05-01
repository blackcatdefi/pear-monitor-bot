'use strict';

/**
 * R-AUTOCOPY — @BlackCatDeFiSignals channel listener.
 *
 * The bot must be added as admin (or at minimum a member) of the public
 * channel @BlackCatDeFiSignals so the Bot API delivers `channel_post`
 * updates. We accept either:
 *
 *   - Numeric SIGNALS_CHANNEL_ID env var (preferred — fixed reference)
 *   - SIGNALS_CHANNEL env var as @username fallback
 *
 * Whichever first message arrives that matches one of these references,
 * we capture the channel_id into a runtime variable so subsequent posts
 * are matched against the resolved id (cheaper than a string compare).
 *
 * The actual signal parsing lives in signalsParser.js — this module is
 * just the wiring.
 */

const parser = require('./signalsParser');
const copyAuto = require('./copyAuto');

const CHANNEL_USERNAME = (process.env.SIGNALS_CHANNEL || '@BlackCatDeFiSignals')
  .replace(/^@/, '')
  .toLowerCase();
let _resolvedChannelId =
  process.env.SIGNALS_CHANNEL_ID
    ? parseInt(process.env.SIGNALS_CHANNEL_ID, 10)
    : null;

function isEnabled() {
  return (process.env.SIGNALS_ENABLED || 'true').toLowerCase() !== 'false';
}

function getResolvedChannelId() {
  return _resolvedChannelId;
}

function _matchesChannel(chat) {
  if (!chat) return false;
  if (_resolvedChannelId && chat.id === _resolvedChannelId) return true;
  if (chat.username && chat.username.toLowerCase() === CHANNEL_USERNAME) {
    // Capture the id for future cycles
    _resolvedChannelId = chat.id;
    return true;
  }
  return false;
}

async function _onChannelPost(post) {
  try {
    if (!post || !post.chat) return;
    if (!_matchesChannel(post.chat)) return;

    const text = post.text || post.caption || '';
    if (!parser.looksLikeSignal(text)) {
      console.log(
        `[signalsChannel] post ${post.message_id} — not a signal, skipping`
      );
      return;
    }
    const sig = parser.parseSignal(text);
    if (!sig) {
      console.log(
        `[signalsChannel] post ${post.message_id} — parser returned null`
      );
      return;
    }
    console.log(
      `[signalsChannel] dispatching signal ${sig.signal_id || '?'} (${sig.positions.length} tokens)`
    );
    const count = await copyAuto.dispatchSignal(sig);
    console.log(`[signalsChannel] dispatched to ${count} user(s)`);
  } catch (e) {
    console.error('[signalsChannel] handler failed:', e && e.message ? e.message : e);
  }
}

function attach(bot) {
  if (!isEnabled()) {
    console.log('[signalsChannel] disabled via SIGNALS_ENABLED=false');
    return;
  }
  bot.on('channel_post', _onChannelPost);
  console.log(
    `[signalsChannel] attached: listening on @${CHANNEL_USERNAME}` +
      (_resolvedChannelId ? ` (id=${_resolvedChannelId})` : ' (id will resolve on first post)')
  );
}

module.exports = {
  attach,
  isEnabled,
  getResolvedChannelId,
  _onChannelPost,
  _matchesChannel,
  CHANNEL_USERNAME,
};
