'use strict';

/**
 * R-AUTOCOPY — /portfolio command.
 *
 *   /portfolio                 → menu (Connect/Refresh/Disconnect)
 *   user pastes 0x...           → fetches HL clearinghouse + renders
 */

const fs = require('fs');
const path = require('path');
const portfolio = require('./portfolioFetcher');
const sm = require('./userStateMachine');

const STATE_PORTFOLIO_AWAITING_ADDR = 'AWAITING_PORTFOLIO_ADDRESS';

// Augment the state machine with our state. userStateMachine validates known
// states, so we monkey-patch its STATES dictionary to include ours.
if (!sm.STATES[STATE_PORTFOLIO_AWAITING_ADDR]) {
  sm.STATES[STATE_PORTFOLIO_AWAITING_ADDR] = STATE_PORTFOLIO_AWAITING_ADDR;
}

function _resolveDbPath() {
  return (
    process.env.PORTFOLIO_DB_PATH ||
    path.join(
      process.env.RAILWAY_VOLUME_MOUNT_PATH || '/app/data',
      'portfolio_addrs.json'
    )
  );
}

const DB_PATH = _resolveDbPath();
let _store = null;

function _ensureDir() {
  try { fs.mkdirSync(path.dirname(DB_PATH), { recursive: true }); }
  catch (_) {}
}

function _load() {
  if (_store !== null) return _store;
  try {
    if (fs.existsSync(DB_PATH)) {
      _store = JSON.parse(fs.readFileSync(DB_PATH, 'utf-8')) || {};
    } else {
      _store = {};
    }
  } catch (_) {
    _store = {};
  }
  return _store;
}

function _save() {
  try {
    _ensureDir();
    fs.writeFileSync(DB_PATH, JSON.stringify(_load(), null, 2));
  } catch (_) {}
}

function getUserAddress(userId) {
  const s = _load();
  return s[String(userId)] || null;
}

function setUserAddress(userId, address) {
  const s = _load();
  s[String(userId)] = address;
  _save();
}

function clearUserAddress(userId) {
  const s = _load();
  delete s[String(userId)];
  _save();
}

function _menu(connected) {
  const rows = [];
  if (!connected) {
    rows.push([{ text: '🔗 Connect read-only wallet', callback_data: 'pf:connect' }]);
  } else {
    rows.push([{ text: '🔄 Refresh', callback_data: 'pf:refresh' }]);
    rows.push([{ text: '🔌 Disconnect', callback_data: 'pf:disconnect' }]);
  }
  return { inline_keyboard: rows };
}

async function _showMenu(bot, chatId, userId) {
  const addr = getUserAddress(userId);
  if (!addr) {
    await bot.sendMessage(
      chatId,
      '📊 *Your portfolio*\n\nConnect a wallet (read-only) to see your equity and positions on HyperLiquid.',
      { parse_mode: 'Markdown', reply_markup: _menu(false) }
    );
    return;
  }
  // Fetch + render
  await bot.sendMessage(chatId, '⏳ Querying HyperLiquid...');
  const p = await portfolio.fetchPortfolio(addr);
  await bot.sendMessage(chatId, portfolio.formatPortfolio(p), {
    parse_mode: 'Markdown',
    reply_markup: _menu(true),
  });
}

async function _handleCallback(bot, cb) {
  if (!cb.data || !cb.data.startsWith('pf:')) return;
  const chatId = cb.message && cb.message.chat ? cb.message.chat.id : null;
  const userId = cb.from && cb.from.id ? cb.from.id : chatId;
  if (!chatId) return;
  try { await bot.answerCallbackQuery(cb.id); } catch (_) {}
  const action = cb.data.split(':')[1];
  if (action === 'connect') {
    sm.setState(chatId, STATE_PORTFOLIO_AWAITING_ADDR, { userId });
    await bot.sendMessage(
      chatId,
      '📥 Paste your address (`0x` + 40 hex). Read-only — never sign anything here.\n\n(type /cancel to go back)',
      { parse_mode: 'Markdown' }
    );
    return;
  }
  if (action === 'refresh') {
    await _showMenu(bot, chatId, userId);
    return;
  }
  if (action === 'disconnect') {
    clearUserAddress(userId);
    await bot.sendMessage(chatId, '🔌 Wallet disconnected.', { parse_mode: 'Markdown' });
    await _showMenu(bot, chatId, userId);
    return;
  }
}

function _isAwaitingAddress(chatId) {
  const st = sm.getState(chatId);
  return st && st.state === STATE_PORTFOLIO_AWAITING_ADDR;
}

async function _handleAddressInput(bot, msg) {
  const chatId = msg.chat.id;
  const userId = (msg.from && msg.from.id) || chatId;
  const text = (msg.text || '').trim();
  if (!portfolio.isValidAddress(text)) {
    await bot.sendMessage(
      chatId,
      '⚠️ Invalid address. Must be `0x` + 40 hex chars.',
      { parse_mode: 'Markdown' }
    );
    return;
  }
  setUserAddress(userId, text);
  sm.reset(chatId);
  await bot.sendMessage(chatId, '✅ Wallet connected (read-only).');
  await _showMenu(bot, chatId, userId);
}

function attach(bot) {
  bot.onText(/^\/portfolio(?:@\w+)?$/i, async (msg) => {
    try {
      await _showMenu(bot, msg.chat.id, msg.from && msg.from.id);
    } catch (e) {
      console.error('[commandsPortfolio] /portfolio failed:', e && e.message ? e.message : e);
    }
  });

  bot.on('callback_query', async (cb) => {
    if (!cb.data || !cb.data.startsWith('pf:')) return;
    try { await _handleCallback(bot, cb); }
    catch (e) {
      console.error('[commandsPortfolio] callback failed:', e && e.message ? e.message : e);
    }
  });

  // Hook plain-text messages when user is in the AWAITING_PORTFOLIO_ADDRESS
  // state. We don't claim other states.
  bot.on('message', async (msg) => {
    if (!msg.text || msg.text.startsWith('/')) return;
    if (!_isAwaitingAddress(msg.chat.id)) return;
    try { await _handleAddressInput(bot, msg); }
    catch (e) {
      console.error('[commandsPortfolio] addr input failed:', e && e.message ? e.message : e);
    }
  });

  console.log('[commandsPortfolio] attached: /portfolio');
}

module.exports = {
  attach,
  STATE_PORTFOLIO_AWAITING_ADDR,
  getUserAddress,
  setUserAddress,
  clearUserAddress,
  _showMenu,
  _menu,
  _handleCallback,
  _isAwaitingAddress,
  DB_PATH,
};
