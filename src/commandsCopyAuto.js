'use strict';

/**
 * R-AUTOCOPY — /copy_auto command + callback router. (R-EN: English)
 */

const store = require('./copyAutoStore');

function _formatStatus(cfg) {
  const lines = [
    '🤖 *Copy Auto*',
    '',
    `Status: ${cfg.enabled ? '🟢 ENABLED' : '🔴 DISABLED'}`,
    `Capital per signal: $${Math.round(cfg.capital_usdc).toLocaleString()} USDC`,
    `Mode: ${cfg.mode}`,
    `Risk preset: SL ${cfg.sl_pct}% / Trailing ${cfg.trailing_pct}% activation ${cfg.trailing_activation_pct}%`,
    '',
    '_When a signal hits @BlackCatDeFiSignals, the pre-armed alert reaches you._',
  ];
  return lines.join('\n');
}

function _buildKeyboard(cfg) {
  return {
    inline_keyboard: [
      [
        { text: '💰 Set capital', callback_data: 'copyauto:capital_help' },
      ],
      [
        {
          text: cfg.mode === 'AUTO' ? '🔄 Switch to MANUAL' : '🔄 Switch to AUTO',
          callback_data: 'copyauto:toggle_mode',
        },
      ],
      [
        {
          text: cfg.enabled ? '🚦 Disable' : '🚦 Enable',
          callback_data: 'copyauto:toggle_enabled',
        },
      ],
      [
        { text: 'ℹ️ How it works', callback_data: 'copyauto:howto' },
      ],
    ],
  };
}

async function showMenu(bot, chatId, userId) {
  const cfg = store.getConfig(userId || chatId);
  await bot.sendMessage(chatId, _formatStatus(cfg), {
    parse_mode: 'Markdown',
    reply_markup: _buildKeyboard(cfg),
  });
}

async function _handleCallback(bot, cb) {
  const chatId = cb.message && cb.message.chat ? cb.message.chat.id : null;
  const userId = cb.from && cb.from.id ? cb.from.id : chatId;
  if (!chatId) return;
  try { await bot.answerCallbackQuery(cb.id); } catch (_) {}

  const action = cb.data.split(':')[1];

  if (action === 'capital_help') {
    await bot.sendMessage(
      chatId,
      `💰 *Set capital*\n\nUsage:\n  \`/capital 500\` — to set $500 USDC\n\nMin: $${store.MIN_CAPITAL} · Max: $${store.MAX_CAPITAL.toLocaleString()}`,
      { parse_mode: 'Markdown' }
    );
    return;
  }
  if (action === 'toggle_mode') {
    const cur = store.getConfig(userId);
    const next = cur.mode === 'AUTO' ? 'MANUAL' : 'AUTO';
    store.setMode(userId, next);
    await bot.sendMessage(chatId, `✅ Mode switched to *${next}*.`, { parse_mode: 'Markdown' });
    await showMenu(bot, chatId, userId);
    return;
  }
  if (action === 'toggle_enabled') {
    const cur = store.getConfig(userId);
    store.setEnabled(userId, !cur.enabled);
    const ny = cur.enabled ? 'disabled' : 'enabled';
    await bot.sendMessage(chatId, `✅ Copy auto ${ny}.`, { parse_mode: 'Markdown' });
    await showMenu(bot, chatId, userId);
    return;
  }
  if (action === 'howto') {
    const lines = [
      'ℹ️ *How it works*',
      '',
      '1️⃣ Toggle copy auto on.',
      '2️⃣ Set your capital with `/capital <amount>`.',
      '3️⃣ When a signal hits @BlackCatDeFiSignals, you get an alert with a one-tap Pear button and your capital pre-loaded.',
      '4️⃣ Click + sign in your wallet → executed.',
      '',
      '*Modes:*',
      '  • *MANUAL* — alert with "Copy on Pear" button',
      '  • *AUTO* — pre-armed alert, wording: "everything\'s ready, you sign"',
      '',
      '⚠️ Pear has no public execution API → you always sign from your wallet (only legit way).',
    ];
    await bot.sendMessage(chatId, lines.join('\n'), { parse_mode: 'Markdown' });
    return;
  }
  if (action === 'menu') {
    await showMenu(bot, chatId, userId);
    return;
  }
  if (action === 'skip') {
    // user pressed "Skip" on a signal alert — nothing to record beyond the
    // callback ack; future stats hook can count skips.
    return;
  }
}

function attach(bot) {
  bot.onText(/^\/copy_?auto(?:@\w+)?$/i, async (msg) => {
    try { await showMenu(bot, msg.chat.id, msg.from && msg.from.id); }
    catch (e) {
      console.error('[commandsCopyAuto] /copy_auto failed:', e && e.message ? e.message : e);
    }
  });

  bot.on('callback_query', async (cb) => {
    if (!cb.data || !cb.data.startsWith('copyauto:')) return;
    try { await _handleCallback(bot, cb); }
    catch (e) {
      console.error('[commandsCopyAuto] callback failed:', e && e.message ? e.message : e);
    }
  });

  console.log('[commandsCopyAuto] attached: /copy_auto');
}

module.exports = {
  attach,
  showMenu,
  _formatStatus,
  _buildKeyboard,
  _handleCallback,
};
