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
    'Elegí qué copiar:',
    '',
    `🐈‍⬛ *BCD Wallet*  ${bcd && bcd.enabled ? HF_HEADER_OK : HF_HEADER_OFF}`,
    `   Capital: ${_fmtMoney(bcd ? bcd.capital_usdc : 0)} · Wallet: \`${_shortAddr(store.BCD_WALLET)}\``,
    '',
    `📡 *BCD Signals*  ${sig && sig.enabled ? HF_HEADER_OK : HF_HEADER_OFF}`,
    `   Capital: ${_fmtMoney(sig ? sig.capital_usdc : 0)} · @${store.BCD_SIGNALS_CHANNEL}`,
    '',
    `👥 *Custom Wallets*  ${customsActive > 0 ? HF_HEADER_OK : HF_HEADER_OFF}`,
    `   ${customsActive}/${customs.length} activas · Total: ${_fmtMoney(customsTotalCap)}`,
    '',
    '─────────────',
    '*Risk preset global:* SL 50% / Trailing 10% activación 30%',
  ];
  const keyboard = {
    inline_keyboard: [
      [{ text: '🐈‍⬛ BCD Wallet', callback_data: 'copytrade:bcd' }],
      [{ text: '📡 BCD Signals Channel', callback_data: 'copytrade:sig' }],
      [{ text: '👥 Custom Wallets', callback_data: 'copytrade:custom' }],
      [{ text: 'ℹ️ Cómo funciona', callback_data: 'copytrade:howto' }],
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
    `Tracking automático de la wallet \`${_shortAddr(store.BCD_WALLET)}\`.`,
    'Cuando abre/cierra basket, recibís alert con link Pear preconfigurado.',
    '',
    `Tu capital: ${_fmtMoney(cap)}`,
    `Estado: ${enabled ? '🟢 ENABLED' : '⚪ DISABLED'}`,
    `Mode: ${mode}`,
  ];
  const keyboard = {
    inline_keyboard: [
      [{ text: '💰 Cambiar capital', callback_data: 'copytrade:bcd:cap_help' }],
      [
        {
          text: mode === 'AUTO' ? '🔄 Modo: AUTO (cambiar a MANUAL)' : '🔄 Modo: MANUAL (cambiar a AUTO)',
          callback_data: 'copytrade:bcd:toggle_mode',
        },
      ],
      [
        {
          text: enabled ? '🚦 Desactivar' : '🚦 Activar',
          callback_data: 'copytrade:bcd:toggle_enabled',
        },
      ],
      [{ text: '← Volver', callback_data: 'copytrade:menu' }],
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
    `Lectura automática de @${store.BCD_SIGNALS_CHANNEL}.`,
    'Cuando se publica una signal con link Pear,',
    'te llega alert inmediato con link copiado y tu capital.',
    '',
    `Tu capital: ${_fmtMoney(cap)}`,
    `Estado: ${enabled ? '🟢 ENABLED' : '⚪ DISABLED'}`,
    `Mode: ${mode}`,
  ];
  const keyboard = {
    inline_keyboard: [
      [
        {
          text: '📲 Abrir canal',
          url: `https://t.me/${store.BCD_SIGNALS_CHANNEL}`,
        },
      ],
      [{ text: '💰 Cambiar capital', callback_data: 'copytrade:sig:cap_help' }],
      [
        {
          text: mode === 'AUTO' ? '🔄 Modo: AUTO (cambiar a MANUAL)' : '🔄 Modo: MANUAL (cambiar a AUTO)',
          callback_data: 'copytrade:sig:toggle_mode',
        },
      ],
      [
        {
          text: enabled ? '🚦 Desactivar' : '🚦 Activar',
          callback_data: 'copytrade:sig:toggle_enabled',
        },
      ],
      [{ text: '← Volver', callback_data: 'copytrade:menu' }],
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
    `Wallets que estás copiando: ${customs.length}/${store.MAX_CUSTOM_PER_USER}`,
    '',
  ];
  if (customs.length === 0) {
    lines.push('_Aún no agregaste ninguna._');
  } else {
    customs.forEach((entry, idx) => {
      const tag = entry.enabled ? '🟢 ON' : '⚪ OFF';
      lines.push(
        `${idx + 1}. \`${_shortAddr(entry.ref)}\` (${entry.label || 'sin label'}) · ${_fmtMoney(entry.capital_usdc)} · ${tag}`
      );
    });
  }
  const rows = [];
  if (customs.length < store.MAX_CUSTOM_PER_USER) {
    rows.push([{ text: '➕ Agregar wallet', callback_data: 'copytrade:custom:add' }]);
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
  rows.push([{ text: '← Volver', callback_data: 'copytrade:menu' }]);
  return { text: lines.join('\n'), keyboard: { inline_keyboard: rows } };
}

function _renderHowto() {
  const lines = [
    'ℹ️ *Cómo funciona Copy Trading*',
    '',
    '*3 modes:*',
    '',
    '🐈‍⬛ *BCD Wallet* — el bot mira la wallet de BCD on-chain (HyperLiquid) cada 60s. Cuando abre/cierra basket, te llega alert.',
    '',
    '📡 *BCD Signals* — el bot lee el canal público @BlackCatDeFiSignals cada 30s. Cuando hay signal con link Pear, te llega.',
    '',
    '👥 *Custom Wallets* — agregás cualquier wallet 0x... y el bot la trackea cada 60s con tu capital configurado.',
    '',
    '*Modes (MANUAL vs AUTO):*',
    '  • MANUAL — botón "Copiar en Pear" estándar.',
    '  • AUTO — alert pre-armado con wording de "lo tengo todo listo, vos firmá".',
    '',
    '*Risk preset global:* SL 50% basket / Trailing 10% activación 30%.',
    '',
    '⚠️ Pear no expone API pública de execution → vos firmás siempre desde tu wallet.',
  ];
  return {
    text: lines.join('\n'),
    keyboard: {
      inline_keyboard: [[{ text: '← Volver', callback_data: 'copytrade:menu' }]],
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
        `💰 Setear capital:\n\nUsá: \`/capital_bcd <monto>\`  (ej. \`/capital_bcd 250\`)\n\nMin: $${store.MIN_CAPITAL} · Max: $${store.MAX_CAPITAL.toLocaleString()}`,
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
        `💰 Setear capital:\n\nUsá: \`/capital_signals <monto>\`  (ej. \`/capital_signals 250\`)\n\nMin: $${store.MIN_CAPITAL} · Max: $${store.MAX_CAPITAL.toLocaleString()}`,
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
        '👥 Pegame la address de la wallet a copiar (debe empezar con `0x` y tener 40 hex):',
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
        '⚠️ Address inválida. Debe ser `0x` + 40 hex.',
        { parse_mode: 'Markdown' }
      );
      return true;
    }
    sm.setState(userId, sm.STATES.COPY_TRADE_AWAIT_LABEL, { address: text.toLowerCase() });
    await bot.sendMessage(
      chatId,
      'Label opcional para esa wallet (ej. "Whale 1"). O respondé `skip`.',
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
      `Capital a usar para esta wallet (USDC). Min ${store.MIN_CAPITAL}, Max ${store.MAX_CAPITAL.toLocaleString()}. Default: ${store.DEFAULT_CAPITAL}.\n\nRespondé un número o "default".`,
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
        await bot.sendMessage(chatId, '⚠️ Monto inválido. Probá de nuevo o "default".');
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
        `✅ Wallet \`${data.address.slice(0, 6)}...${data.address.slice(-4)}\` agregada con capital $${Math.round(capital).toLocaleString()} y ENABLED.`,
        { parse_mode: 'Markdown' }
      );
      const menu = _renderCustomMenu(userId);
      await bot.sendMessage(chatId, menu.text, {
        parse_mode: 'Markdown',
        reply_markup: menu.keyboard,
      });
    } catch (e) {
      sm.reset(userId);
      await bot.sendMessage(chatId, `⚠️ ${e.message || 'No pude guardar la wallet.'}`);
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
      `Capital actual: ${_fmtMoney(cur.capital_usdc)}\n\nRango: $${store.MIN_CAPITAL} – $${store.MAX_CAPITAL.toLocaleString()}\n\nUsá: \`<comando> <monto>\``,
      { parse_mode: 'Markdown' }
    );
    return;
  }
  try {
    const cleaned = arg.replace(/[\$,\s]/g, '');
    const next = store.setTarget(userId, type, null, { capital_usdc: cleaned });
    bot.sendMessage(
      chatId,
      `✅ Capital actualizado: ${_fmtMoney(next.capital_usdc)}.`,
      { parse_mode: 'Markdown' }
    );
  } catch (e) {
    bot.sendMessage(chatId, `⚠️ ${e.message || 'Monto inválido.'}`);
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
