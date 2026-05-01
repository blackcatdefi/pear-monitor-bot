'use strict';

/**
 * R-START — /start handler.
 *
 * Detects first-time vs recurring users (via onboarding.js JSON store):
 *   • First-time → full Spanish onboarding tutorial + 3-row inline keyboard
 *                  + auto-detect TZ from Telegram's language_code (silent;
 *                  user can override later with /timezone).
 *   • Recurring  → compact dashboard ("Bienvenido de vuelta") with the same
 *                  3-row keyboard so the UX is consistent.
 *
 * Hero button label: "🍐 Abrir Pear Protocol".
 * Hero URL: PEAR_HERO_URL env var (default https://app.pear.garden/?referral=BlackCatDeFi).
 * The label NEVER mentions "referral" or "BlackCat" — see alertButtons.js
 * for the same pattern in basket alerts.
 *
 * Inline-keyboard callbacks routed here:
 *   start:track_add    → opens the /track add-wallet flow
 *   start:track_list   → renders the user's tracked-wallet list
 *   start:tz_menu      → opens the /timezone help message
 *   start:status_view  → shows tracked-wallets count + bot status quick view
 *   mute:<addr>        → silences a tracked wallet (removes from /track list)
 *
 * IMPORTANT: this handler REPLACES the legacy /start in bot.js. bot.js still
 * owns /menu (the inline-keyboard for personal wallet management on the
 * bot operator's own chat), but plain /start is exclusively R-START.
 */

const onboarding = require('./onboarding');
const tzMgr = require('./timezoneManager');
const wt = require('./walletTracker');
const sm = require('./userStateMachine');
// R-AUTOCOPY — referral capture + stats touch.
const share = require('./share');
const stats = require('./stats');

const DEFAULT_HERO_URL =
  'https://app.pear.garden/?referral=BlackCatDeFi';

function _heroUrl() {
  const fromEnv = process.env.PEAR_HERO_URL;
  if (fromEnv && String(fromEnv).trim()) return String(fromEnv);
  return DEFAULT_HERO_URL;
}

/**
 * Build the inline keyboard for /start. Same 3-row layout for first-time
 * and recurring users — only the message text differs.
 *
 * Returns a Telegram inline_keyboard object suitable for reply_markup.
 */
function buildStartKeyboard(/* isReturning unused — same layout */) {
  return {
    inline_keyboard: [
      [
        { text: '🎯 Trackear wallet', callback_data: 'start:track_add' },
        { text: '📋 Mis wallets', callback_data: 'start:track_list' },
      ],
      [
        // R-AUTOCOPY — signals + copy auto in row 2.
        { text: '📡 Signals oficiales', callback_data: 'start:signals_menu' },
        { text: '🤖 Copy auto', callback_data: 'start:copyauto_menu' },
      ],
      [
        { text: '🌎 Mi TZ', callback_data: 'start:tz_menu' },
        { text: '📊 Status', callback_data: 'start:status_view' },
      ],
      [
        { text: '🍐 Abrir Pear Protocol', url: _heroUrl() },
      ],
    ],
  };
}

function _formatFirstTimeText(detectedTz) {
  const lines = [
    '🍐 *Pear Protocol Alerts*',
    '',
    'Tu copiloto de trading on-chain. Te aviso cuando pasa algo importante en wallets que seguís — propias o de otros traders.',
    '',
    '*⚡ Qué podés hacer:*',
    '',
    '🎯 Trackear wallets de top traders en HyperLiquid',
    '📋 Recibir alertas en tiempo real cuando abren/cierran baskets',
    '🔗 Copiar sus trades en 1 toque (con sus pares exactos en Pear)',
    '🎯 Monitorear TP/SL y fondos disponibles',
    '🏦 Alertas de borrow en HyperLend',
    '',
    '🌎 Configurá tu zona horaria con /timezone',
    '📡 Empezá a trackear con /track',
  ];
  if (detectedTz && detectedTz !== tzMgr.DEFAULT_TZ) {
    lines.push(
      '',
      `🌎 Detecté tu zona horaria como \`${detectedTz}\`. Cambiala con /timezone si no es correcta.`
    );
  }
  return lines.join('\n');
}

function _formatRecurringText(userId) {
  const tz = tzMgr.getUserTz(userId);
  const wallets = wt.getUserWallets(userId);
  const lines = [
    '🍐 *Pear Protocol Alerts*',
    '',
    'Bienvenido de vuelta 👋',
    '',
    '📊 *Tu setup actual:*',
    `  🌎 TZ: \`${tz}\``,
    `  📡 Wallets trackeadas: ${wallets.length}/${wt.MAX_WALLETS_PER_USER}`,
    `  🟢 Bot status: activo`,
  ];
  return lines.join('\n');
}

async function handleStart(bot, msg) {
  const chatId = msg.chat.id;
  const userId = msg.from && msg.from.id ? msg.from.id : chatId;
  const langCode = msg.from && msg.from.language_code;

  const wasFirstTime = onboarding.isFirstTime(userId);
  let detectedTz = null;
  if (wasFirstTime) {
    try {
      detectedTz = onboarding.autoDetectTzIfFirstTime(userId, langCode);
    } catch (_) {
      detectedTz = null;
    }
  }
  onboarding.markSeen(userId);

  // R-AUTOCOPY — capture /start ref_<userId> deep-link payload (only on
  // first sighting, to keep the flow idempotent).
  if (wasFirstTime) {
    const m = (msg.text || '').match(/^\/start(?:@\w+)?\s+(\S+)/);
    if (m && m[1]) {
      const refUid = share.parseStartPayload(m[1]);
      if (refUid) {
        try { share.recordReferral(refUid, userId); } catch (_) {}
      }
    }
  }
  try { stats.touch(userId); } catch (_) {}

  const text = wasFirstTime
    ? _formatFirstTimeText(detectedTz)
    : _formatRecurringText(userId);

  const keyboard = buildStartKeyboard(!wasFirstTime);
  await bot.sendMessage(chatId, text, {
    parse_mode: 'Markdown',
    reply_markup: keyboard,
    disable_web_page_preview: true,
  });
}

/**
 * Routes start:* and mute:* callbacks. Returns true if handled, false if
 * the callback isn't ours (so other modules' handlers can take over).
 */
async function _handleCallback(bot, cb) {
  if (!cb.data) return false;
  if (cb.data.startsWith('mute:')) return _handleMuteCallback(bot, cb);
  if (!cb.data.startsWith('start:')) return false;

  const chatId = cb.message && cb.message.chat ? cb.message.chat.id : null;
  const userId = cb.from && cb.from.id ? cb.from.id : chatId;
  if (!chatId) return false;

  try {
    await bot.answerCallbackQuery(cb.id);
  } catch (_) {}

  const action = cb.data.split(':')[1];

  if (action === 'track_add') {
    const wallets = wt.getUserWallets(userId);
    if (wallets.length >= wt.MAX_WALLETS_PER_USER) {
      await bot.sendMessage(
        chatId,
        `🚫 Llegaste al máximo de ${wt.MAX_WALLETS_PER_USER} wallets.\n\n` +
          'Remové alguna primero con /track → 📋 Mis wallets.',
        { parse_mode: 'Markdown' }
      );
      return true;
    }
    sm.setState(chatId, sm.STATES.AWAITING_WALLET_ADDRESS, { userId });
    await bot.sendMessage(
      chatId,
      '📡 *Trackear nueva wallet*\n\n' +
        'Pegame la dirección (`0x...`) que querés seguir.\n\n' +
        `💡 _Tip: podés trackear hasta ${wt.MAX_WALLETS_PER_USER} wallets de traders top._\n` +
        '_Cuando abran una basket, te aviso al instante con\n' +
        'un botón para copiar su trade._\n\n' +
        '(escribí /cancel para volver)',
      { parse_mode: 'Markdown' }
    );
    return true;
  }

  if (action === 'track_list') {
    const wallets = wt.getUserWallets(userId);
    if (wallets.length === 0) {
      await bot.sendMessage(
        chatId,
        '📋 No tenés wallets trackeadas todavía.\n\nUsá /track y tocá *🎯 Trackear wallet*.',
        { parse_mode: 'Markdown' }
      );
      return true;
    }
    const lines = ['📋 *TUS WALLETS TRACKEADAS*', ''];
    for (const w of wallets) {
      const label = w.label ? ` — ${w.label}` : '';
      lines.push(`  • \`${w.address}\`${label}`);
    }
    lines.push('', `Total: ${wallets.length}/${wt.MAX_WALLETS_PER_USER}`);
    await bot.sendMessage(chatId, lines.join('\n'), {
      parse_mode: 'Markdown',
    });
    return true;
  }

  if (action === 'tz_menu') {
    const current = tzMgr.getUserTz(userId);
    await bot.sendMessage(
      chatId,
      '🌎 *Tu zona horaria*\n\n' +
        `Actual: \`${current}\`\n\n` +
        'Para cambiarla:\n' +
        '  • `/timezone <IANA>` (ej. `/timezone America/Argentina/Buenos_Aires`)\n' +
        '  • `/timezone auto` para detectarla',
      { parse_mode: 'Markdown' }
    );
    return true;
  }

  if (action === 'signals_menu') {
    try {
      const cmdSignals = require('./commandsSignals');
      await bot.sendMessage(chatId, cmdSignals._bodyText(), {
        parse_mode: 'Markdown',
        reply_markup: cmdSignals._menuKeyboard(),
        disable_web_page_preview: true,
      });
    } catch (_) {
      await bot.sendMessage(chatId, 'Tocá /signals.', { parse_mode: 'Markdown' });
    }
    return true;
  }

  if (action === 'copyauto_menu') {
    try {
      const cmdCA = require('./commandsCopyAuto');
      await cmdCA.showMenu(bot, chatId, userId);
    } catch (_) {
      await bot.sendMessage(chatId, 'Tocá /copy_auto.', { parse_mode: 'Markdown' });
    }
    return true;
  }

  if (action === 'status_view') {
    const wallets = wt.getUserWallets(userId);
    const tz = tzMgr.getUserTz(userId);
    const lines = [
      '📊 *Alertas activas*',
      '',
      `🟢 Bot: activo`,
      `🌎 TZ: \`${tz}\``,
      `📡 Wallets trackeadas: ${wallets.length}/${wt.MAX_WALLETS_PER_USER}`,
    ];
    if (wallets.length > 0) {
      lines.push('', '_Recibís alerta cuando estas wallets abren o cierran baskets._');
    } else {
      lines.push('', 'Tocá *🎯 Trackear wallet* para empezar.');
    }
    await bot.sendMessage(chatId, lines.join('\n'), {
      parse_mode: 'Markdown',
    });
    return true;
  }

  return false;
}

/**
 * Handles the `mute:<addr>` callback emitted by alertButtons.buildAlertKeyboard.
 * Removes the wallet from the user's tracked list (idempotent — they can
 * /track it again later).
 */
async function _handleMuteCallback(bot, cb) {
  if (!cb.data || !cb.data.startsWith('mute:')) return false;
  const chatId = cb.message && cb.message.chat ? cb.message.chat.id : null;
  const userId = cb.from && cb.from.id ? cb.from.id : chatId;
  if (!chatId) return false;
  try {
    await bot.answerCallbackQuery(cb.id, { text: 'Wallet silenciada.' });
  } catch (_) {}
  const addr = cb.data.slice(5).toLowerCase();
  let removed = 0;
  try {
    removed = wt.removeWallet(userId, addr);
  } catch (_) {}
  if (removed > 0) {
    await bot.sendMessage(
      chatId,
      `🔕 Wallet \`${addr}\` silenciada. No vas a recibir más alertas de esta wallet.\n\n` +
        '_Podés re-trackearla con /track cuando quieras._',
      { parse_mode: 'Markdown' }
    );
  } else {
    await bot.sendMessage(
      chatId,
      `ℹ️ Esa wallet ya no está en tu lista de trackeo.`,
      { parse_mode: 'Markdown' }
    );
  }
  return true;
}

function attach(bot) {
  // R-AUTOCOPY — accept optional /start payload (deep-link referral) so
  // the regex matches `/start ref_12345` as well as bare `/start`.
  bot.onText(/^\/start(?:@\w+)?(?:\s+\S+)?$/i, async (msg) => {
    try {
      await handleStart(bot, msg);
    } catch (e) {
      console.error(
        '[commandsStart] /start failed:',
        e && e.message ? e.message : e
      );
    }
  });

  bot.on('callback_query', async (cb) => {
    if (
      !cb.data ||
      (!cb.data.startsWith('start:') && !cb.data.startsWith('mute:'))
    )
      return;
    try {
      await _handleCallback(bot, cb);
    } catch (e) {
      console.error(
        '[commandsStart] callback failed:',
        e && e.message ? e.message : e
      );
    }
  });

  console.log(
    '[commandsStart] attached: /start + start:* + mute:* callbacks'
  );
}

module.exports = {
  attach,
  handleStart,
  buildStartKeyboard,
  _formatFirstTimeText,
  _formatRecurringText,
  _handleCallback,
  _handleMuteCallback,
};
