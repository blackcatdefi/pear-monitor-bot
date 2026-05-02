'use strict';

/**
 * R-START — /start handler.
 *
 * Detects first-time vs recurring users (via onboarding.js JSON store):
 *   • First-time → full English onboarding tutorial + 3-row inline keyboard
 *                  + auto-detect TZ from Telegram's language_code (silent;
 *                  user can override later with /timezone).
 *   • Recurring  → compact dashboard ("Welcome back") with the same 3-row
 *                  keyboard so the UX is consistent.
 *
 * Hero button label: "🍐 Open Pear Protocol".
 * Hero URL: PEAR_HERO_URL env var (default https://app.pear.garden/?referral=BlackCatDeFi).
 * The label NEVER mentions "referral" or "BlackCat" — see alertButtons.js
 * for the same pattern in basket alerts.
 *
 * Inline-keyboard callbacks routed here:
 *   start:track_add        → opens the /track add-wallet flow
 *   start:track_list       → renders the user's tracked-wallet list
 *   start:tz_menu          → opens the /timezone help message
 *   start:status_view      → shows tracked-wallets count + bot status
 *   start:copytrading_menu → opens the /copy_trading top menu
 *   start:learn_menu       → opens /learn
 *   mute:<addr>            → silences a tracked wallet
 *
 * R-EN — All user-facing strings now go through `t()` from `./i18n`.
 */

const onboarding = require('./onboarding');
const tzMgr = require('./timezoneManager');
const wt = require('./walletTracker');
const sm = require('./userStateMachine');
const { t } = require('./i18n/index');
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
 */
function buildStartKeyboard(/* isReturning unused — same layout */) {
  return {
    inline_keyboard: [
      [
        { text: t('start.kb_track_add'), callback_data: 'start:track_add' },
        { text: t('start.kb_track_list'), callback_data: 'start:track_list' },
      ],
      [
        // R-AUTOCOPY-MENU — unified Copy Trading entry replaces the separate
        // signals + copy_auto buttons. Inside the menu the user picks one of
        // 3 modes (BCD wallet / BCD Signals / custom wallets).
        { text: t('start.kb_copy_trading'), callback_data: 'start:copytrading_menu' },
        { text: t('start.kb_status'), callback_data: 'start:status_view' },
      ],
      [
        { text: t('start.kb_tz'), callback_data: 'start:tz_menu' },
        { text: t('start.kb_learn'), callback_data: 'start:learn_menu' },
      ],
      [
        { text: t('start.kb_pear'), url: _heroUrl() },
      ],
    ],
  };
}

function _formatFirstTimeText(detectedTz) {
  const lines = [
    t('start.title'),
    '',
    t('start.first_time_intro'),
    '',
    t('start.first_time_what'),
    '',
    t('start.first_time_b1'),
    t('start.first_time_b2'),
    t('start.first_time_b3'),
    t('start.first_time_b4'),
    t('start.first_time_b5'),
    '',
    t('start.first_time_tz_hint'),
    t('start.first_time_track_hint'),
  ];
  if (detectedTz && detectedTz !== tzMgr.DEFAULT_TZ) {
    lines.push('', t('start.tz_detected', { tz: detectedTz }));
  }
  return lines.join('\n');
}

function _formatRecurringText(userId) {
  const tz = tzMgr.getUserTz(userId);
  const wallets = wt.getUserWallets(userId);
  const lines = [
    t('start.title'),
    '',
    t('start.recurring_welcome'),
    '',
    t('start.recurring_setup'),
    t('start.recurring_tz', { tz }),
    t('start.recurring_wallets', {
      count: wallets.length,
      max: wt.MAX_WALLETS_PER_USER,
    }),
    t('start.recurring_status'),
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
        t('start.track_max_reached', { max: wt.MAX_WALLETS_PER_USER }),
        { parse_mode: 'Markdown' }
      );
      return true;
    }
    sm.setState(chatId, sm.STATES.AWAITING_WALLET_ADDRESS, { userId });
    await bot.sendMessage(
      chatId,
      t('start.track_add_prompt', { max: wt.MAX_WALLETS_PER_USER }),
      { parse_mode: 'Markdown' }
    );
    return true;
  }

  if (action === 'track_list') {
    const wallets = wt.getUserWallets(userId);
    if (wallets.length === 0) {
      await bot.sendMessage(chatId, t('start.list_empty'), {
        parse_mode: 'Markdown',
      });
      return true;
    }
    const lines = [t('start.list_header'), ''];
    for (const w of wallets) {
      const label = w.label ? ` — ${w.label}` : '';
      lines.push(`  • \`${w.address}\`${label}`);
    }
    lines.push(
      '',
      t('start.list_total', {
        count: wallets.length,
        max: wt.MAX_WALLETS_PER_USER,
      })
    );
    await bot.sendMessage(chatId, lines.join('\n'), {
      parse_mode: 'Markdown',
    });
    return true;
  }

  if (action === 'tz_menu') {
    const current = tzMgr.getUserTz(userId);
    await bot.sendMessage(
      chatId,
      [
        t('start.tz_menu_title'),
        '',
        t('start.tz_menu_current', { tz: current }),
        '',
        t('start.tz_menu_howto'),
      ].join('\n'),
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
      await bot.sendMessage(chatId, t('start.tap_signals'), { parse_mode: 'Markdown' });
    }
    return true;
  }

  if (action === 'copyauto_menu') {
    try {
      const cmdCA = require('./commandsCopyAuto');
      await cmdCA.showMenu(bot, chatId, userId);
    } catch (_) {
      await bot.sendMessage(chatId, t('start.tap_copyauto'), { parse_mode: 'Markdown' });
    }
    return true;
  }

  if (action === 'copytrading_menu') {
    try {
      const cmdCT = require('./commandsCopyTrading');
      await cmdCT.showTopMenu(bot, chatId, userId);
    } catch (_) {
      await bot.sendMessage(chatId, t('start.tap_copytrading'), { parse_mode: 'Markdown' });
    }
    return true;
  }

  if (action === 'learn_menu') {
    try {
      const cmdLearn = require('./commandsLearn');
      // commandsLearn exposes `attach(bot)` which wires `/learn` — for the
      // inline-keyboard entry we re-use the same body builder if present,
      // else fall back to a hint message.
      if (typeof cmdLearn.showMenu === 'function') {
        await cmdLearn.showMenu(bot, chatId, userId);
      } else {
        await bot.sendMessage(chatId, t('start.tap_learn_full'), { parse_mode: 'Markdown' });
      }
    } catch (_) {
      await bot.sendMessage(chatId, t('start.tap_learn'), { parse_mode: 'Markdown' });
    }
    return true;
  }

  if (action === 'status_view') {
    const wallets = wt.getUserWallets(userId);
    const tz = tzMgr.getUserTz(userId);
    const lines = [
      t('start.status_title'),
      '',
      t('start.status_bot'),
      t('start.status_tz', { tz }),
      t('start.status_wallets', {
        count: wallets.length,
        max: wt.MAX_WALLETS_PER_USER,
      }),
    ];
    if (wallets.length > 0) {
      lines.push('', t('start.status_recv'));
    } else {
      lines.push('', t('start.status_empty_cta'));
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
    await bot.answerCallbackQuery(cb.id, { text: t('start.muted_callback') });
  } catch (_) {}
  const addr = cb.data.slice(5).toLowerCase();
  let removed = 0;
  try {
    removed = wt.removeWallet(userId, addr);
  } catch (_) {}
  if (removed > 0) {
    await bot.sendMessage(
      chatId,
      t('start.muted_wallet', { addr }),
      { parse_mode: 'Markdown' }
    );
  } else {
    await bot.sendMessage(
      chatId,
      t('start.not_in_list'),
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
