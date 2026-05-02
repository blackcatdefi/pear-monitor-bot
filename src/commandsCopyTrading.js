'use strict';

/**
 * R-AUTOCOPY-MENU — /copy_trading unified menu.
 *
 *   /copy_trading             → top-level menu (3 modes)
 *   callback copytrade:bcd    → BCD wallet sub-menu
 *   callback copytrade:sig    → BCD signals sub-menu
 *   callback copytrade:custom → custom wallets sub-menu
 *
 * Each sub-menu lets the user toggle ON/OFF, change capital, change mode
 * (MANUAL/AUTO), and (for custom) add/remove addresses. Capital changes
 * happen via /capital <amount> (already wired) — the sub-menu shows a
 * quick-help message linking to /capital.
 *
 * State for the "add custom wallet" two-step flow lives in userStateMachine
 * (already used by R-PUBLIC's /track flow).
 */

const store = require('./copyTradingStore');
const sm = require('./userStateMachine');

// R-AUTOCOPY-MENU — monkey-patch our 3-step add-flow states into the state
// machine's allowed-list (it validates with `Object.values(STATES).includes`).
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
  const sig = t[store.TYPE_BCD_SIGNALS];
  const customs = t[store.TYPE_CUSTOM_WALLET] || [];
  const customsActive = customs.filter((x) => x.enabled).length;
  const customsTotalCap = customs
    .filter((x) => x.enabled)
    .reduce((s, x) => s + (Number(x.capital_usdc) || 0), 0);

  const lines = [
    '🤖 *Copy Trading*',
    '',
    'Pick what to copy:',
    '',
    `🐈‍⬛ *BCD Wallet*  ${bcd && bcd.enabled ? HF_HEADER_OK : HF_HEADER_OFF}`,
    `   Capital: ${_fmtMoney(bcd ? bcd.capital_usdc : 0)} · Wallet: \`${_shortAddr(store.BCD_WALLET)}\``,
    '',
    `📡 *BCD Signals*  ${sig && sig.enabled ? HF_HEADER_OK : HF_HEADER_OFF}`,
    `   Capital: ${_fmtMoney(sig ? sig.capital_usdc : 0)} · @${store.BCD_SIGNALS_CHANNEL}`,
    '',
    `👥 *Custom Wallets*  ${customsActive > 0 ? HF_HEADER_OK : HF_HEADER_OFF}`,
    `   ${customsActive}/${customs.length} active · Total: ${_fmtMoney(customsTotalCap)}`,
    '',
    '─────────────',
    '*Global risk preset:* SL 50% / Trailing 10% activation 30%',
  ];
  const keyboard = {
    inline_keyboard: [
      [{ text: '🐈‍⬛ BCD Wallet', callback_data: 'copytrade:bcd' }],
      [{ text: '📡 BCD Signals Channel', callback_data: 'copytrade:sig' }],
      [{ text: '👥 Custom Wallets', callback_data: 'copytrade:custom' }],
      [{ text: 'ℹ️ How it works', callback_data: 'copytrade:howto' }],
    ],
  };
  return { text: lines.join('\n'), keyboard };
}

// --- Sub-menus -----------------------------------------------------------

function _renderBcdMenu(userId) {
  const cfg = store.getTarget(userId, store.TYPE_BCD_WALLET);
  const enabled = cfg && cfg.enabled;
  const cap = cfg ? cfg.capital_usdc : store.DEFAULT_CAPITAL;
  const mode = cfg ? cfg.mode : 'MANUAL';
  const lines = [
    '🐈‍⬛ *Copy BCD Wallet*',
    '',
    `Auto-tracking wallet \`${_shortAddr(store.BCD_WALLET)}\`.`,
    'When it opens/closes a basket, you get an alert with a pre-configured Pear link.',
    '',
    `Your capital: ${_fmtMoney(cap)}`,
    `Status: ${enabled ? '🟢 ENABLED' : '⚪ DISABLED'}`,
    `Mode: ${mode}`,
  ];
  const keyboard = {
    inline_keyboard: [
      [{ text: '💰 Change capital', callback_data: 'copytrade:bcd:cap_help' }],
      [
        {
          text: mode === 'AUTO' ? '🔄 Mode: AUTO (switch to MANUAL)' : '🔄 Mode: MANUAL (switch to AUTO)',
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

function _renderSigMenu(userId) {
  const cfg = store.getTarget(userId, store.TYPE_BCD_SIGNALS);
  const enabled = cfg && cfg.enabled;
  const cap = cfg ? cfg.capital_usdc : store.DEFAULT_CAPITAL;
  const mode = cfg ? cfg.mode : 'MANUAL';
  const lines = [
    '📡 *Copy BCD Signals Channel*',
    '',
    `Auto-reading @${store.BCD_SIGNALS_CHANNEL}.`,
    'When a signal with a Pear link is posted,',
    'you get an instant alert with the copied link and your capital.',
    '',
    `Your capital: ${_fmtMoney(cap)}`,
    `Status: ${enabled ? '🟢 ENABLED' : '⚪ DISABLED'}`,
    `Mode: ${mode}`,
  ];
  const keyboard = {
    inline_keyboard: [
      [
        {
          text: '📲 Open channel',
          url: `https://t.me/${store.BCD_SIGNALS_CHANNEL}`,
        },
      ],
      [{ text: '💰 Change capital', callback_data: 'copytrade:sig:cap_help' }],
      [
        {
          text: mode === 'AUTO' ? '🔄 Mode: AUTO (switch to MANUAL)' : '🔄 Mode: MANUAL (switch to AUTO)',
          callback_data: 'copytrade:sig:toggle_mode',
        },
      ],
      [
        {
          text: enabled ? '🚦 Disable' : '🚦 Enable',
          callback_data: 'copytrade:sig:toggle_enabled',
        },
      ],
      [{ text: '← Back', callback_data: 'copytrade:menu' }],
    ],
  };
  return { text: lines.join('\n'), keyboard };
}

function _renderCustomMenu(userId) {
  const slot = store.getTargets(userId);
  const customs = slot[store.TYPE_CUSTOM_WALLET] || [];
  const lines = [
    '👥 *Copy Custom Wallets*',
    '',
    `Wallets you're copying: ${customs.length}/${store.MAX_CUSTOM_PER_USER}`,
    '',
  ];
  if (customs.length === 0) {
    lines.push('_You haven\'t added any yet._');
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

function _renderHowto() {
  const lines = [
    'ℹ️ *How Copy Trading works*',
    '',
    '*3 modes:*',
    '',
    '🐈‍⬛ *BCD Wallet* — the bot watches BCD\'s on-chain wallet (HyperLiquid) every 60s. When it opens/closes a basket, you get an alert.',
    '',
    '📡 *BCD Signals* — the bot reads the public channel @BlackCatDeFiSignals every 30s. When there\'s a signal with a Pear link, you get it.',
    '',
    '👥 *Custom Wallets* — add any 0x... wallet and the bot tracks it every 60s with your configured capital.',
    '',
    '*Modes (MANUAL vs AUTO):*',
    '  • MANUAL — standard "Copy on Pear" button.',
    '  • AUTO — pre-armed alert with "everything\'s ready, you sign" wording.',
    '',
    '*Global risk preset:* SL 50% basket / Trailing 10% activation 30%.',
    '',
    '⚠️ Pear has no public execution API → you always sign from your wallet.',
  ];
  return {
    text: lines.join('\n'),
    keyboard: {
      inline_keyboard: [[{ text: '← Back', callback_data: 'copytrade:menu' }]],
    },
  };
}

// --- Callbacks -----------------------------------------------------------

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
      // Fall through to send-new (some Telegram quirks like "message not modified").
    }
  }
  await bot.sendMessage(chatId, payload.text, opts);
}

async function showTopMenu(bot, chatId, userId) {
  await bot.sendMessage(chatId, _renderTopMenu(userId).text, {
    parse_mode: 'Markdown',
    reply_markup: _renderTopMenu(userId).keyboard,
  });
}

async function _handleCallback(bot, cb) {
  const chatId = cb.message && cb.message.chat ? cb.message.chat.id : null;
  const userId = cb.from && cb.from.id ? cb.from.id : chatId;
  const msgId = cb.message && cb.message.message_id;
  if (!chatId) return;
  try { await bot.answerCallbackQuery(cb.id); } catch (_) {}

  const parts = cb.data.split(':');
  // copytrade:menu | copytrade:bcd | copytrade:sig | copytrade:custom | copytrade:skip | copytrade:howto
  const root = parts[1] || '';
  const sub = parts[2] || '';
  const arg = parts.slice(3).join(':') || null;

  if (root === '' || root === 'menu') {
    return _editOrSend(bot, chatId, userId, msgId, _renderTopMenu(userId));
  }
  if (root === 'howto') {
    return _editOrSend(bot, chatId, userId, msgId, _renderHowto());
  }
  if (root === 'skip') {
    return; // ack only
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

  if (root === 'sig') {
    if (!sub) {
      return _editOrSend(bot, chatId, userId, msgId, _renderSigMenu(userId));
    }
    if (sub === 'cap_help') {
      await bot.sendMessage(
        chatId,
        `💰 Set capital:\n\nUsage: \`/capital_signals <amount>\`  (e.g. \`/capital_signals 250\`)\n\nMin: $${store.MIN_CAPITAL} · Max: $${store.MAX_CAPITAL.toLocaleString()}`,
        { parse_mode: 'Markdown' }
      );
      return;
    }
    if (sub === 'toggle_mode') {
      const cur = store.getTarget(userId, store.TYPE_BCD_SIGNALS) || {};
      const next = (cur.mode === 'AUTO') ? 'MANUAL' : 'AUTO';
      store.setTarget(userId, store.TYPE_BCD_SIGNALS, null, { mode: next });
      return _editOrSend(bot, chatId, userId, msgId, _renderSigMenu(userId));
    }
    if (sub === 'toggle_enabled') {
      const cur = store.getTarget(userId, store.TYPE_BCD_SIGNALS) || {};
      store.setTarget(userId, store.TYPE_BCD_SIGNALS, null, { enabled: !cur.enabled });
      return _editOrSend(bot, chatId, userId, msgId, _renderSigMenu(userId));
    }
  }

  if (root === 'custom') {
    if (!sub) {
      return _editOrSend(bot, chatId, userId, msgId, _renderCustomMenu(userId));
    }
    if (sub === 'add') {
      sm.setState(userId, sm.STATES.COPY_TRADE_AWAIT_ADDRESS, { msgId });
      await bot.sendMessage(
        chatId,
        '👥 Send the wallet address to copy (must start with `0x` and be 40 hex chars):',
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
}

// --- Address-input two-step state machine -------------------------------

async function _handleTextInput(bot, msg) {
  const userId = msg.from && msg.from.id ? msg.from.id : msg.chat.id;
  const chatId = msg.chat.id;
  const text = (msg.text || '').trim();
  if (!text || text.startsWith('/')) return false; // commands handled elsewhere
  const state = sm.getState(userId);
  if (!state || !state.state || state.state === sm.STATES.IDLE) return false;
  // Only intercept our copy-trade conversational states; other modules own
  // the rest (e.g. AWAITING_WALLET_ADDRESS / AWAITING_PORTFOLIO_ADDRESS).
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

// --- /capital_bcd /capital_signals (alias) -------------------------------

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
  bot.onText(/^\/capital_signals(?:@\w+)?(?:\s|$)/i, (msg) => {
    try { _handleCapitalCmd(store.TYPE_BCD_SIGNALS, bot, msg); }
    catch (e) { console.error('[commandsCopyTrading] /capital_signals failed:', e && e.message ? e.message : e); }
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

  console.log('[commandsCopyTrading] attached: /copy_trading + sub-menus');
}

module.exports = {
  attach,
  showTopMenu,
  _renderTopMenu,
  _renderBcdMenu,
  _renderSigMenu,
  _renderCustomMenu,
  _renderHowto,
  _handleCallback,
  _handleTextInput,
  _handleCapitalCmd,
};
