'use strict';

/**
 * R-PUBLIC-V4-COPYMENU — /copy_trading + Copy Trading submenu.
 *
 *   /copy_trading                  → top-level menu (Black Cat / Custom / Settings)
 *   callback copytrade:bcd         → BCD wallet sub-menu
 *   callback copytrade:custom      → custom wallets sub-menu
 *   callback copytrade:settings    → user settings sub-menu
 *   callback copytrade:menu        → back to top
 *   callback copytrade:back_start  → back to /start (deletes current msg)
 *
 * V4 changes vs V3 (R-AUTOCOPY-MENU):
 *   • BCD_SIGNALS source REMOVED — only on-chain wallet polling.
 *   • Custom wallets cap reduced 10 → 3.
 *   • New ⚙️ Settings sub-menu (basket-level only / pause all / wallet list).
 *   • Single back button always returns to top-level menu.
 *   • Top-level menu is the ONLY entry point invoked from /start.
 *
 * State for the "add custom wallet" three-step flow lives in
 * userStateMachine (already used by R-PUBLIC's /track flow). We monkey-patch
 * 3 V4 states into the state machine's allow-list because userStateMachine
 * validates set-states against `Object.values(STATES).includes`.
 */

const store = require('./copyTradingStore');
const sm = require('./userStateMachine');

sm.STATES.COPY_TRADE_AWAIT_ADDRESS =
  sm.STATES.COPY_TRADE_AWAIT_ADDRESS || 'COPY_TRADE_AWAIT_ADDRESS';
sm.STATES.COPY_TRADE_AWAIT_LABEL =
  sm.STATES.COPY_TRADE_AWAIT_LABEL || 'COPY_TRADE_AWAIT_LABEL';
sm.STATES.COPY_TRADE_AWAIT_CAPITAL =
  sm.STATES.COPY_TRADE_AWAIT_CAPITAL || 'COPY_TRADE_AWAIT_CAPITAL';

const HF_HEADER_OK = '🟢 ON';
const HF_HEADER_OFF = '⚪ OFF';

function _fmtMoney(n) {
  if (!Number.isFinite(n)) return '$0';
  return `$${Math.round(n).toLocaleString()}`;
}

function _shortAddr(a) {
  const s = String(a || '');
  if (s.length < 12) return s;
  return `${s.slice(0, 6)}...${s.slice(-4)}`;
}

// --- Top-level menu ------------------------------------------------------

function _renderTopMenu(userId) {
  const t = store.getTargets(userId);
  const bcd = t[store.TYPE_BCD_WALLET];
  const customs = t[store.TYPE_CUSTOM_WALLET] || [];
  const customsActive = customs.filter((x) => x.enabled).length;
  const settings = t.settings || {};
  const paused = !!settings.paused;

  const lines = [
    '🤖 *Copy Trading Menu*',
    '',
    'Choose what to copy automatically:',
    '',
    `🐈‍⬛ *Black Cat Wallet*  ${bcd && bcd.enabled ? HF_HEADER_OK : HF_HEADER_OFF}`,
    `   Capital: ${_fmtMoney(bcd ? bcd.capital_usdc : 0)} · Wallet: \`${_shortAddr(store.BCD_WALLET)}\``,
    '',
    `👁 *Custom Wallets*  ${customsActive > 0 ? HF_HEADER_OK : HF_HEADER_OFF}`,
    `   ${customsActive}/${customs.length} active · Cap: ${customs.length}/${store.MAX_CUSTOM_PER_USER}`,
    '',
  ];
  if (paused) {
    lines.push('⏸️ _All copy notifications paused (toggle in Settings)._');
    lines.push('');
  }
  lines.push('─────────────');
  lines.push('Source: on-chain wallet polling only.');
  lines.push('_Per-leg alerts disabled by default — basket-level events only._');

  const keyboard = {
    inline_keyboard: [
      [{ text: '🐈‍⬛ COPY BLACK CAT WALLET — auto-mirror', callback_data: 'copytrade:bcd' }],
      [{ text: '👁 COPY CUSTOM WALLET — paste address', callback_data: 'copytrade:custom' }],
      [{ text: '⚙️ MY COPY SETTINGS', callback_data: 'copytrade:settings' }],
      [{ text: '← BACK', callback_data: 'copytrade:back_start' }],
    ],
  };
  return { text: lines.join('\n'), keyboard };
}

// --- Sub-menu: Black Cat wallet ------------------------------------------

function _renderBcdMenu(userId) {
  const cfg = store.getTarget(userId, store.TYPE_BCD_WALLET);
  const enabled = cfg && cfg.enabled;
  const cap = cfg ? cfg.capital_usdc : store.DEFAULT_CAPITAL;
  const mode = cfg ? cfg.mode : 'MANUAL';
  const lines = [
    '🐈‍⬛ *Copy Black Cat Wallet*',
    '',
    `Auto-mirror of wallet \`${_shortAddr(store.BCD_WALLET)}\`.`,
    'Polled every 60s. When this wallet opens or closes a basket, you',
    'receive a DM with a 1-tap copy URL pre-filled with your size.',
    '',
    `Your size multiplier: ${(cap / store.DEFAULT_CAPITAL).toFixed(2)}x ` +
      `(≈ ${_fmtMoney(cap)} per basket of ${_fmtMoney(store.DEFAULT_CAPITAL)})`,
    `Status: ${enabled ? '🟢 ENABLED' : '⚪ DISABLED'}`,
    `Mode: ${mode}`,
  ];
  const keyboard = {
    inline_keyboard: [
      [
        { text: '💰 0.5x', callback_data: 'copytrade:bcd:size:0.5' },
        { text: '💰 1x',   callback_data: 'copytrade:bcd:size:1'   },
        { text: '💰 2x',   callback_data: 'copytrade:bcd:size:2'   },
      ],
      [{ text: '💵 Custom capital ($)', callback_data: 'copytrade:bcd:cap_help' }],
      [
        {
          text: mode === 'AUTO'
            ? '🔄 Mode: AUTO (switch to MANUAL)'
            : '🔄 Mode: MANUAL (switch to AUTO)',
          callback_data: 'copytrade:bcd:toggle_mode',
        },
      ],
      [
        {
          text: enabled ? '🚦 Disable' : '🚦 Enable',
          callback_data: 'copytrade:bcd:toggle_enabled',
        },
      ],
      [{ text: '← Back', callback_data: 'copytrade:menu' }],
    ],
  };
  return { text: lines.join('\n'), keyboard };
}

// --- Sub-menu: Custom wallets --------------------------------------------

function _renderCustomMenu(userId) {
  const slot = store.getTargets(userId);
  const customs = slot[store.TYPE_CUSTOM_WALLET] || [];
  const lines = [
    '👁 *Copy Custom Wallets*',
    '',
    `Wallets you're copying: ${customs.length}/${store.MAX_CUSTOM_PER_USER}`,
    '',
  ];
  if (customs.length === 0) {
    lines.push('_None yet. Tap *➕ Add wallet* and paste a Hyperliquid address._');
  } else {
    customs.forEach((entry, idx) => {
      const tag = entry.enabled ? '🟢 ON' : '⚪ OFF';
      lines.push(
        `${idx + 1}. \`${_shortAddr(entry.ref)}\` (${entry.label || 'no label'}) · ${_fmtMoney(entry.capital_usdc)} · ${tag}`
      );
    });
  }
  const rows = [];
  if (customs.length < store.MAX_CUSTOM_PER_USER) {
    rows.push([{ text: '➕ Add wallet', callback_data: 'copytrade:custom:add' }]);
  } else {
    rows.push([{ text: `⚠️ Limit ${store.MAX_CUSTOM_PER_USER}/${store.MAX_CUSTOM_PER_USER} — remove one to add another`, callback_data: 'copytrade:custom' }]);
  }
  for (const entry of customs) {
    const short = _shortAddr(entry.ref);
    rows.push([
      {
        text: `${entry.enabled ? '🚦 OFF' : '🚦 ON'} ${short}`,
        callback_data: `copytrade:custom:toggle:${entry.ref}`,
      },
      {
        text: `🗑️ ${short}`,
        callback_data: `copytrade:custom:rm:${entry.ref}`,
      },
    ]);
  }
  rows.push([{ text: '← Back', callback_data: 'copytrade:menu' }]);
  return { text: lines.join('\n'), keyboard: { inline_keyboard: rows } };
}

// --- Sub-menu: Settings --------------------------------------------------

function _renderSettingsMenu(userId) {
  const settings = store.getSettings(userId);
  const t = store.getTargets(userId);
  const bcd = t[store.TYPE_BCD_WALLET];
  const customs = t[store.TYPE_CUSTOM_WALLET] || [];

  const lines = [
    '⚙️ *My Copy Settings*',
    '',
    '*Active subscriptions*',
    `  🐈‍⬛ Black Cat: ${bcd && bcd.enabled ? '🟢 ON' : '⚪ OFF'}`,
    `  👁 Custom wallets: ${customs.filter((x) => x.enabled).length}/${customs.length}`,
    '',
    `Default capital: ${_fmtMoney(store.DEFAULT_CAPITAL)} (env COPY_AUTO_DEFAULT_CAPITAL)`,
    '',
    `*Basket-level only*: ${settings.basket_level_only ? '🟢 ON' : '⚪ OFF'}`,
    '_(when ON, you only get one alert per basket OPEN/CLOSE — never per leg)_',
    '',
    `*Paused*: ${settings.paused ? '⏸️ YES' : '▶️ NO'}`,
    '_(silences all copy DMs without losing your config)_',
  ];
  const keyboard = {
    inline_keyboard: [
      [
        {
          text: settings.basket_level_only
            ? '🚦 Per-leg alerts (currently OFF — turn ON)'
            : '🚦 Basket-level only (currently OFF — turn ON)',
          callback_data: 'copytrade:settings:toggle_basket_level',
        },
      ],
      [
        {
          text: settings.paused ? '▶️ Resume copy DMs' : '⏸️ Pause copy DMs',
          callback_data: 'copytrade:settings:toggle_paused',
        },
      ],
      [{ text: '← Back', callback_data: 'copytrade:menu' }],
    ],
  };
  return { text: lines.join('\n'), keyboard };
}

// --- Callback dispatch ---------------------------------------------------

async function _editOrSend(bot, chatId, userId, msgId, payload) {
  const opts = {
    parse_mode: 'Markdown',
    reply_markup: payload.keyboard,
    disable_web_page_preview: true,
  };
  if (msgId) {
    try {
      await bot.editMessageText(payload.text, {
        chat_id: chatId,
        message_id: msgId,
        ...opts,
      });
      return;
    } catch (_) {
      // Fall through to send-new (Telegram quirks like "message not modified").
    }
  }
  await bot.sendMessage(chatId, payload.text, opts);
}

async function showTopMenu(bot, chatId, userId) {
  const payload = _renderTopMenu(userId);
  await bot.sendMessage(chatId, payload.text, {
    parse_mode: 'Markdown',
    reply_markup: payload.keyboard,
  });
}

async function _handleCallback(bot, cb) {
  const chatId = cb.message && cb.message.chat ? cb.message.chat.id : null;
  const userId = cb.from && cb.from.id ? cb.from.id : chatId;
  const msgId = cb.message && cb.message.message_id;
  if (!chatId) return;
  try { await bot.answerCallbackQuery(cb.id); } catch (_) {}

  const parts = cb.data.split(':');
  // copytrade:menu | copytrade:bcd | copytrade:custom | copytrade:settings | copytrade:skip | copytrade:back_start
  const root = parts[1] || '';
  const sub = parts[2] || '';
  const arg = parts.slice(3).join(':') || null;

  if (root === '' || root === 'menu') {
    return _editOrSend(bot, chatId, userId, msgId, _renderTopMenu(userId));
  }
  if (root === 'skip') {
    return; // ack only — used by alert keyboards
  }
  if (root === 'back_start') {
    // Try to delete current menu, then return user to /start hero.
    if (msgId) {
      try { await bot.deleteMessage(chatId, msgId); } catch (_) {}
    }
    let simplified = null;
    try { simplified = require('./simplifiedStart'); } catch (_) {}
    if (simplified && simplified.isEnabled() && simplified.handleStartSimple) {
      try {
        await simplified.handleStartSimple(bot, {
          chat: { id: chatId },
          from: { id: userId },
          text: '/start',
        });
        return;
      } catch (_) {}
    }
    return;
  }

  if (root === 'bcd') {
    if (!sub) {
      return _editOrSend(bot, chatId, userId, msgId, _renderBcdMenu(userId));
    }
    if (sub === 'cap_help') {
      await bot.sendMessage(
        chatId,
        `💰 Set capital:\n\nUsage: \`/capital_bcd <amount>\`  (e.g. \`/capital_bcd 250\`)\n\nMin: $${store.MIN_CAPITAL} · Max: $${store.MAX_CAPITAL.toLocaleString()}`,
        { parse_mode: 'Markdown' }
      );
      return;
    }
    if (sub === 'size') {
      const mult = parseFloat(arg);
      if (Number.isFinite(mult) && mult > 0) {
        const next = store.DEFAULT_CAPITAL * mult;
        try {
          store.setTarget(userId, store.TYPE_BCD_WALLET, null, {
            capital_usdc: next,
          });
        } catch (e) {
          await bot.sendMessage(chatId, `⚠️ ${e.message || 'Invalid size'}`);
          return;
        }
      }
      return _editOrSend(bot, chatId, userId, msgId, _renderBcdMenu(userId));
    }
    if (sub === 'toggle_mode') {
      const cur = store.getTarget(userId, store.TYPE_BCD_WALLET) || {};
      const next = (cur.mode === 'AUTO') ? 'MANUAL' : 'AUTO';
      store.setTarget(userId, store.TYPE_BCD_WALLET, null, { mode: next });
      return _editOrSend(bot, chatId, userId, msgId, _renderBcdMenu(userId));
    }
    if (sub === 'toggle_enabled') {
      const cur = store.getTarget(userId, store.TYPE_BCD_WALLET) || {};
      store.setTarget(userId, store.TYPE_BCD_WALLET, null, { enabled: !cur.enabled });
      return _editOrSend(bot, chatId, userId, msgId, _renderBcdMenu(userId));
    }
  }

  if (root === 'custom') {
    if (!sub) {
      return _editOrSend(bot, chatId, userId, msgId, _renderCustomMenu(userId));
    }
    if (sub === 'add') {
      const slot = store.getTargets(userId);
      const customs = slot[store.TYPE_CUSTOM_WALLET] || [];
      if (customs.length >= store.MAX_CUSTOM_PER_USER) {
        await bot.sendMessage(
          chatId,
          `⚠️ You hit the ${store.MAX_CUSTOM_PER_USER}-wallet limit. Remove one before adding another.`,
          { parse_mode: 'Markdown' }
        );
        return;
      }
      sm.setState(userId, sm.STATES.COPY_TRADE_AWAIT_ADDRESS, { msgId });
      await bot.sendMessage(
        chatId,
        '👁 Send the wallet address to copy (must start with `0x` and be 40 hex chars):',
        { parse_mode: 'Markdown' }
      );
      return;
    }
    if (sub === 'toggle') {
      const cur = store.getTarget(userId, store.TYPE_CUSTOM_WALLET, arg) || {};
      store.setTarget(userId, store.TYPE_CUSTOM_WALLET, arg, { enabled: !cur.enabled });
      return _editOrSend(bot, chatId, userId, msgId, _renderCustomMenu(userId));
    }
    if (sub === 'rm') {
      store.removeTarget(userId, store.TYPE_CUSTOM_WALLET, arg);
      return _editOrSend(bot, chatId, userId, msgId, _renderCustomMenu(userId));
    }
  }

  if (root === 'settings') {
    if (!sub) {
      return _editOrSend(bot, chatId, userId, msgId, _renderSettingsMenu(userId));
    }
    if (sub === 'toggle_basket_level') {
      const cur = store.getSettings(userId);
      store.setSetting(userId, 'basket_level_only', !cur.basket_level_only);
      return _editOrSend(bot, chatId, userId, msgId, _renderSettingsMenu(userId));
    }
    if (sub === 'toggle_paused') {
      const cur = store.getSettings(userId);
      store.setSetting(userId, 'paused', !cur.paused);
      return _editOrSend(bot, chatId, userId, msgId, _renderSettingsMenu(userId));
    }
  }
}

// --- Address-input three-step state machine ------------------------------

async function _handleTextInput(bot, msg) {
  const userId = msg.from && msg.from.id ? msg.from.id : msg.chat.id;
  const chatId = msg.chat.id;
  const text = (msg.text || '').trim();
  if (!text || text.startsWith('/')) return false; // commands handled elsewhere
  const state = sm.getState(userId);
  if (!state || !state.state || state.state === sm.STATES.IDLE) return false;
  const ours = new Set([
    sm.STATES.COPY_TRADE_AWAIT_ADDRESS,
    sm.STATES.COPY_TRADE_AWAIT_LABEL,
    sm.STATES.COPY_TRADE_AWAIT_CAPITAL,
  ]);
  if (!ours.has(state.state)) return false;

  if (state.state === sm.STATES.COPY_TRADE_AWAIT_ADDRESS) {
    if (!/^0x[a-fA-F0-9]{40}$/.test(text)) {
      await bot.sendMessage(
        chatId,
        '⚠️ Invalid address. Must be `0x` + 40 hex chars.',
        { parse_mode: 'Markdown' }
      );
      return true;
    }
    sm.setState(userId, sm.STATES.COPY_TRADE_AWAIT_LABEL, { address: text.toLowerCase() });
    await bot.sendMessage(
      chatId,
      'Optional label for this wallet (e.g. "Whale 1"). Or reply `skip`.',
      { parse_mode: 'Markdown' }
    );
    return true;
  }

  if (state.state === sm.STATES.COPY_TRADE_AWAIT_LABEL) {
    const address = state.payload && state.payload.address;
    let label = '';
    if (text.toLowerCase() !== 'skip') {
      label = text.slice(0, 64);
    }
    sm.setState(userId, sm.STATES.COPY_TRADE_AWAIT_CAPITAL, { address, label });
    await bot.sendMessage(
      chatId,
      `Capital to use for this wallet (USDC). Min ${store.MIN_CAPITAL}, Max ${store.MAX_CAPITAL.toLocaleString()}. Default: ${store.DEFAULT_CAPITAL}.\n\nReply with a number or "default".`,
      { parse_mode: 'Markdown' }
    );
    return true;
  }

  if (state.state === sm.STATES.COPY_TRADE_AWAIT_CAPITAL) {
    const data = state.payload || {};
    let capital = store.DEFAULT_CAPITAL;
    if (text.toLowerCase() !== 'default') {
      const cleaned = text.replace(/[\$,\s]/g, '');
      const n = parseFloat(cleaned);
      if (!Number.isFinite(n)) {
        await bot.sendMessage(chatId, '⚠️ Invalid amount. Try again or "default".');
        return true;
      }
      capital = n;
    }
    try {
      store.setTarget(userId, store.TYPE_CUSTOM_WALLET, data.address, {
        enabled: true,
        capital_usdc: capital,
        label: data.label || '',
      });
      sm.reset(userId);
      await bot.sendMessage(
        chatId,
        `✅ Wallet \`${data.address.slice(0, 6)}...${data.address.slice(-4)}\` added with capital $${Math.round(capital).toLocaleString()} and ENABLED.`,
        { parse_mode: 'Markdown' }
      );
      const menu = _renderCustomMenu(userId);
      await bot.sendMessage(chatId, menu.text, {
        parse_mode: 'Markdown',
        reply_markup: menu.keyboard,
      });
    } catch (e) {
      sm.reset(userId);
      await bot.sendMessage(chatId, `⚠️ ${e.message || 'Failed to save wallet.'}`);
    }
    return true;
  }

  return false;
}

// --- /capital_bcd command ------------------------------------------------

function _handleCapitalCmd(type, bot, msg) {
  const chatId = msg.chat.id;
  const userId = msg.from && msg.from.id ? msg.from.id : chatId;
  const m = (msg.text || '').match(/^\/\S+\s*(.*)$/);
  const arg = m ? (m[1] || '').trim() : '';
  if (!arg) {
    const cur = store.getTarget(userId, type) || { capital_usdc: store.DEFAULT_CAPITAL };
    bot.sendMessage(
      chatId,
      `Current capital: ${_fmtMoney(cur.capital_usdc)}\n\nRange: $${store.MIN_CAPITAL} – $${store.MAX_CAPITAL.toLocaleString()}\n\nUsage: \`<command> <amount>\``,
      { parse_mode: 'Markdown' }
    );
    return;
  }
  try {
    const cleaned = arg.replace(/[\$,\s]/g, '');
    const next = store.setTarget(userId, type, null, { capital_usdc: cleaned });
    bot.sendMessage(
      chatId,
      `✅ Capital updated: ${_fmtMoney(next.capital_usdc)}.`,
      { parse_mode: 'Markdown' }
    );
  } catch (e) {
    bot.sendMessage(chatId, `⚠️ ${e.message || 'Invalid amount.'}`);
  }
}

function attach(bot) {
  bot.onText(/^\/copy_?trading(?:@\w+)?$/i, async (msg) => {
    try { await showTopMenu(bot, msg.chat.id, msg.from && msg.from.id); }
    catch (e) {
      console.error('[commandsCopyTrading] /copy_trading failed:', e && e.message ? e.message : e);
    }
  });

  bot.onText(/^\/capital_bcd(?:@\w+)?(?:\s|$)/i, (msg) => {
    try { _handleCapitalCmd(store.TYPE_BCD_WALLET, bot, msg); }
    catch (e) { console.error('[commandsCopyTrading] /capital_bcd failed:', e && e.message ? e.message : e); }
  });

  bot.on('callback_query', async (cb) => {
    if (!cb.data || !cb.data.startsWith('copytrade:')) return;
    try { await _handleCallback(bot, cb); }
    catch (e) {
      console.error('[commandsCopyTrading] callback failed:', e && e.message ? e.message : e);
    }
  });

  bot.on('message', async (msg) => {
    if (!msg || !msg.text || msg.text.startsWith('/')) return;
    try { await _handleTextInput(bot, msg); }
    catch (e) {
      console.error('[commandsCopyTrading] text input failed:', e && e.message ? e.message : e);
    }
  });

  console.log('[commandsCopyTrading] V4 attached: /copy_trading + Black Cat / Custom / Settings');
}

module.exports = {
  attach,
  showTopMenu,
  _renderTopMenu,
  _renderBcdMenu,
  _renderCustomMenu,
  _renderSettingsMenu,
  _handleCallback,
  _handleTextInput,
  _handleCapitalCmd,
};
