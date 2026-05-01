'use strict';

/**
 * R-PUBLIC — /track command + inline keyboard handlers.
 *
 * UI flow:
 *   /track                                  → menú [Agregar] [Mis wallets] [Eliminar]
 *   tap "➕ Agregar wallet"                 → set state AWAITING_WALLET_ADDRESS
 *     next text msg = address               → validate → set state AWAITING_WALLET_LABEL
 *   next text msg = label or /skip          → persist, reset state, confirm
 *   tap "📋 Mis wallets"                    → list of subscriptions
 *   tap "🔕 Dejar de trackear"             → set state AWAITING_REMOVE_ADDRESS
 *
 * The state machine is keyed on chatId (private chats use chatId === userId).
 */

const wt = require('./walletTracker');
const sm = require('./userStateMachine');

const MENU_KEYBOARD = {
  inline_keyboard: [
    [{ text: '➕ Agregar wallet', callback_data: 'track:add' }],
    [{ text: '📋 Mis wallets trackeadas', callback_data: 'track:list' }],
    [{ text: '🔕 Dejar de trackear', callback_data: 'track:remove' }],
  ],
};

function _shortAddr(a) {
  if (!a) return '?';
  const s = String(a);
  if (s.length < 12) return s;
  return `${s.slice(0, 6)}...${s.slice(-4)}`;
}

async function _showMenu(bot, chatId) {
  await bot.sendMessage(
    chatId,
    '🎯 *TRACK — Wallets externas*\n\n' +
      'Trackeá cualquier wallet de Hyperliquid y recibí alertas cuando abre o cierra baskets, con botón para copiar el trade en Pear.',
    { parse_mode: 'Markdown', reply_markup: MENU_KEYBOARD }
  );
}

async function _showList(bot, chatId, userId) {
  const wallets = wt.getUserWallets(userId);
  if (wallets.length === 0) {
    await bot.sendMessage(
      chatId,
      '📋 No tenés wallets trackeadas todavía.\n\nUsá `/track` y tocá *Agregar wallet*.',
      { parse_mode: 'Markdown' }
    );
    return;
  }
  const lines = ['📋 *TUS WALLETS TRACKEADAS*', ''];
  for (const w of wallets) {
    const label = w.label ? ` — ${w.label}` : '';
    lines.push(`  • \`${w.address}\`${label}`);
  }
  lines.push('');
  lines.push(`Total: ${wallets.length}/${wt.MAX_WALLETS_PER_USER}`);
  await bot.sendMessage(chatId, lines.join('\n'), { parse_mode: 'Markdown' });
}

function attach(bot) {
  bot.onText(/^\/track$/i, async (msg) => {
    sm.reset(msg.chat.id);
    await _showMenu(bot, msg.chat.id);
  });

  bot.on('callback_query', async (cb) => {
    if (!cb.data || !cb.data.startsWith('track:')) return;
    const chatId = cb.message && cb.message.chat ? cb.message.chat.id : null;
    const userId = cb.from && cb.from.id ? cb.from.id : chatId;
    if (!chatId) return;

    const action = cb.data.split(':')[1];
    try {
      await bot.answerCallbackQuery(cb.id);
    } catch (_) {}

    if (action === 'add') {
      sm.setState(chatId, sm.STATES.AWAITING_WALLET_ADDRESS, { userId });
      await bot.sendMessage(
        chatId,
        '📥 Mandame la dirección de la wallet (formato `0x...` con 40 caracteres hex):',
        { parse_mode: 'Markdown' }
      );
    } else if (action === 'list') {
      await _showList(bot, chatId, userId);
    } else if (action === 'remove') {
      const wallets = wt.getUserWallets(userId);
      if (wallets.length === 0) {
        await bot.sendMessage(chatId, 'No tenés wallets trackeadas.');
        return;
      }
      sm.setState(chatId, sm.STATES.AWAITING_REMOVE_ADDRESS, { userId });
      const lines = [
        '🔕 *Eliminar wallet*',
        '',
        'Mandame la dirección (o el shortcut tipo `0x6abc...`) que querés dejar de trackear:',
        '',
      ];
      for (const w of wallets) {
        const label = w.label ? ` — ${w.label}` : '';
        lines.push(`  • \`${w.address}\`${label}`);
      }
      await bot.sendMessage(chatId, lines.join('\n'), {
        parse_mode: 'Markdown',
      });
    }
  });

  // Plain-text handler for state transitions. Listens to all messages and
  // gates on current state. Commands (/...) bypass this handler.
  bot.on('message', async (msg) => {
    if (!msg.text || msg.text.startsWith('/')) return;
    const chatId = msg.chat.id;
    const rec = sm.getState(chatId);
    if (rec.state === sm.STATES.IDLE) return;

    const userId = (rec.payload && rec.payload.userId) ||
      (msg.from && msg.from.id) || chatId;
    const text = msg.text.trim();

    if (rec.state === sm.STATES.AWAITING_WALLET_ADDRESS) {
      if (!wt.isValidAddress(text)) {
        await bot.sendMessage(
          chatId,
          '⚠️ Esa dirección no parece válida. Tiene que ser `0x` seguido de 40 caracteres hex (ej: `0x1234abcd...0000`).\n\nMandame de nuevo o /cancel.',
          { parse_mode: 'Markdown' }
        );
        return;
      }
      sm.setState(chatId, sm.STATES.AWAITING_WALLET_LABEL, {
        userId,
        address: text,
      });
      await bot.sendMessage(
        chatId,
        `✅ Dirección \`${text}\` validada.\n\n` +
          `¿Querés ponerle un nombre/etiqueta? (ej. *Whale 1*) o mandá \`/skip\` para guardar sin nombre.`,
        { parse_mode: 'Markdown' }
      );
      return;
    }

    if (rec.state === sm.STATES.AWAITING_WALLET_LABEL) {
      const address = rec.payload && rec.payload.address;
      const label = text;
      try {
        wt.addWallet(userId, address, label);
      } catch (e) {
        sm.reset(chatId);
        await bot.sendMessage(
          chatId,
          `⚠️ No pude guardar: ${e.message || 'error desconocido'}`
        );
        return;
      }
      sm.reset(chatId);
      await bot.sendMessage(
        chatId,
        `✅ Wallet \`${address}\` (${label}) trackeada.\n\n` +
          `Te voy a avisar cuando abra o cierre baskets.`,
        { parse_mode: 'Markdown' }
      );
      return;
    }

    if (rec.state === sm.STATES.AWAITING_REMOVE_ADDRESS) {
      const list = wt.getUserWallets(userId);
      const lc = text.toLowerCase();
      const match = list.find(
        (w) =>
          String(w.address).toLowerCase() === lc ||
          String(w.address).toLowerCase().startsWith(lc) ||
          (w.label && w.label.toLowerCase() === lc)
      );
      if (!match) {
        sm.reset(chatId);
        await bot.sendMessage(
          chatId,
          `⚠️ No encontré una wallet matching \`${text}\`. Usá /track para ver tu lista.`,
          { parse_mode: 'Markdown' }
        );
        return;
      }
      const removed = wt.removeWallet(userId, match.address);
      sm.reset(chatId);
      await bot.sendMessage(
        chatId,
        removed > 0
          ? `✅ Wallet \`${match.address}\` eliminada (${removed} record).`
          : `⚠️ No pude eliminar.`,
        { parse_mode: 'Markdown' }
      );
      return;
    }
  });

  // /skip handler for label step
  bot.onText(/^\/skip$/i, async (msg) => {
    const chatId = msg.chat.id;
    const rec = sm.getState(chatId);
    if (rec.state !== sm.STATES.AWAITING_WALLET_LABEL) return;
    const userId = rec.payload.userId;
    const address = rec.payload.address;
    try {
      wt.addWallet(userId, address, null);
    } catch (e) {
      sm.reset(chatId);
      await bot.sendMessage(
        chatId,
        `⚠️ No pude guardar: ${e.message || 'error desconocido'}`
      );
      return;
    }
    sm.reset(chatId);
    await bot.sendMessage(
      chatId,
      `✅ Wallet \`${address}\` trackeada (sin etiqueta).`,
      { parse_mode: 'Markdown' }
    );
  });

  // /cancel — abort any in-flight conversation
  bot.onText(/^\/cancel$/i, async (msg) => {
    const chatId = msg.chat.id;
    sm.reset(chatId);
    await bot.sendMessage(chatId, 'Cancelado. Mandá /track para empezar de nuevo.');
  });

  console.log('[commandsTrack] attached: /track /skip /cancel + callbacks');
}

module.exports = { attach, MENU_KEYBOARD };
