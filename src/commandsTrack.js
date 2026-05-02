'use strict';

/**
 * R-PUBLIC — /track command + inline keyboard handlers.
 *
 * UI flow:
 *   /track                                  → menu [Add] [My wallets] [Remove]
 *   tap "➕ Add wallet"                     → set state AWAITING_WALLET_ADDRESS
 *     next text msg = address               → validate → set state AWAITING_WALLET_LABEL
 *   next text msg = label or /skip          → persist, reset state, confirm
 *   tap "📋 My wallets"                     → list of subscriptions
 *   tap "🔕 Stop tracking"                 → set state AWAITING_REMOVE_ADDRESS
 *
 * The state machine is keyed on chatId (private chats use chatId === userId).
 * R-EN — All user-facing strings now go through `t()` from `./i18n`.
 */

const wt = require('./walletTracker');
const sm = require('./userStateMachine');
const { t } = require('./i18n/index');

function _menuKeyboard() {
  return {
    inline_keyboard: [
      [{ text: t('track.menu_kb_add'), callback_data: 'track:add' }],
      [{ text: t('track.menu_kb_list'), callback_data: 'track:list' }],
      [{ text: t('track.menu_kb_remove'), callback_data: 'track:remove' }],
    ],
  };
}

// Kept for backward-compat with tests that import MENU_KEYBOARD.
const MENU_KEYBOARD = _menuKeyboard();

async function _showMenu(bot, chatId) {
  await bot.sendMessage(
    chatId,
    `${t('track.menu_title')}\n\n${t('track.menu_body')}`,
    { parse_mode: 'Markdown', reply_markup: _menuKeyboard() }
  );
}

async function _showList(bot, chatId, userId) {
  const wallets = wt.getUserWallets(userId);
  if (wallets.length === 0) {
    await bot.sendMessage(chatId, t('track.list_empty'), {
      parse_mode: 'Markdown',
    });
    return;
  }
  const lines = [t('track.list_header'), ''];
  for (const w of wallets) {
    const label = w.label ? ` — ${w.label}` : '';
    lines.push(`  • \`${w.address}\`${label}`);
  }
  lines.push('');
  lines.push(
    t('track.list_total', {
      count: wallets.length,
      max: wt.MAX_WALLETS_PER_USER,
    })
  );
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
      await bot.sendMessage(chatId, t('track.add_prompt'), {
        parse_mode: 'Markdown',
      });
    } else if (action === 'list') {
      await _showList(bot, chatId, userId);
    } else if (action === 'remove') {
      const wallets = wt.getUserWallets(userId);
      if (wallets.length === 0) {
        await bot.sendMessage(chatId, t('track.no_tracked'));
        return;
      }
      sm.setState(chatId, sm.STATES.AWAITING_REMOVE_ADDRESS, { userId });
      const lines = [
        t('track.remove_title'),
        '',
        t('track.remove_prompt'),
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
        await bot.sendMessage(chatId, t('track.invalid_addr'), {
          parse_mode: 'Markdown',
        });
        return;
      }
      sm.setState(chatId, sm.STATES.AWAITING_WALLET_LABEL, {
        userId,
        address: text,
      });
      await bot.sendMessage(
        chatId,
        t('track.addr_validated', { addr: text }),
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
          t('track.save_failed', { error: e.message || t('track.error_unknown') })
        );
        return;
      }
      sm.reset(chatId);
      await bot.sendMessage(
        chatId,
        t('track.saved_with_label', { addr: address, label }),
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
        await bot.sendMessage(chatId, t('track.not_found', { q: text }), {
          parse_mode: 'Markdown',
        });
        return;
      }
      const removed = wt.removeWallet(userId, match.address);
      sm.reset(chatId);
      await bot.sendMessage(
        chatId,
        removed > 0
          ? t('track.removed_ok', { addr: match.address, n: removed })
          : t('track.remove_failed'),
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
        t('track.save_failed', { error: e.message || t('track.error_unknown') })
      );
      return;
    }
    sm.reset(chatId);
    await bot.sendMessage(
      chatId,
      t('track.saved_no_label', { addr: address }),
      { parse_mode: 'Markdown' }
    );
  });

  // /cancel — abort any in-flight conversation
  bot.onText(/^\/cancel$/i, async (msg) => {
    const chatId = msg.chat.id;
    sm.reset(chatId);
    await bot.sendMessage(chatId, t('track.cancelled'));
  });

  console.log('[commandsTrack] attached: /track /skip /cancel + callbacks');
}

module.exports = { attach, MENU_KEYBOARD };
