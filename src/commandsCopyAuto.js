'use strict';

/**
 * R-AUTOCOPY — /copy_auto command + callback router.
 */

const store = require('./copyAutoStore');

function _formatStatus(cfg) {
  const lines = [
    '🤖 *Copy Auto*',
    '',
    `Estado: ${cfg.enabled ? '🟢 ACTIVADO' : '🔴 DESACTIVADO'}`,
    `Capital por signal: $${Math.round(cfg.capital_usdc).toLocaleString()} USDC`,
    `Modo: ${cfg.mode}`,
    `Risk preset: SL ${cfg.sl_pct}% / Trailing ${cfg.trailing_pct}% activación ${cfg.trailing_activation_pct}%`,
    '',
    '_Cuando hay signal en @BlackCatDeFiSignals, te llega el alert pre-armado._',
  ];
  return lines.join('\n');
}

function _buildKeyboard(cfg) {
  return {
    inline_keyboard: [
      [
        { text: '💰 Setear capital', callback_data: 'copyauto:capital_help' },
      ],
      [
        {
          text: cfg.mode === 'AUTO' ? '🔄 Cambiar a MANUAL' : '🔄 Cambiar a AUTO',
          callback_data: 'copyauto:toggle_mode',
        },
      ],
      [
        {
          text: cfg.enabled ? '🚦 Desactivar' : '🚦 Activar',
          callback_data: 'copyauto:toggle_enabled',
        },
      ],
      [
        { text: 'ℹ️ Cómo funciona', callback_data: 'copyauto:howto' },
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
      `💰 *Setear capital*\n\nUsá:\n  \`/capital 500\` — para fijar $500 USDC\n\nMin: $${store.MIN_CAPITAL} · Max: $${store.MAX_CAPITAL.toLocaleString()}`,
      { parse_mode: 'Markdown' }
    );
    return;
  }
  if (action === 'toggle_mode') {
    const cur = store.getConfig(userId);
    const next = cur.mode === 'AUTO' ? 'MANUAL' : 'AUTO';
    store.setMode(userId, next);
    await bot.sendMessage(chatId, `✅ Modo cambiado a *${next}*.`, { parse_mode: 'Markdown' });
    await showMenu(bot, chatId, userId);
    return;
  }
  if (action === 'toggle_enabled') {
    const cur = store.getConfig(userId);
    store.setEnabled(userId, !cur.enabled);
    const ny = cur.enabled ? 'desactivado' : 'activado';
    await bot.sendMessage(chatId, `✅ Copy auto ${ny}.`, { parse_mode: 'Markdown' });
    await showMenu(bot, chatId, userId);
    return;
  }
  if (action === 'howto') {
    const lines = [
      'ℹ️ *Cómo funciona*',
      '',
      '1️⃣ Activás copy auto (toggle).',
      '2️⃣ Setea tu capital con `/capital <amount>`.',
      '3️⃣ Cuando hay signal en @BlackCatDeFiSignals, recibís un alert con un botón directo a Pear con tu capital pre-cargado.',
      '4️⃣ Click + firmás en tu wallet → ejecutado.',
      '',
      '*Modos:*',
      '  • *MANUAL* — alert con botón "Copiar en Pear"',
      '  • *AUTO* — alert pre-armado, wording de "lo tengo todo listo, vos firmá"',
      '',
      '⚠️ Pear no expone API pública de execution → vos firmás siempre desde tu wallet (única forma legítima).',
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
