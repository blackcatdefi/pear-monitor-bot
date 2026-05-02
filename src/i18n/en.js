'use strict';

/**
 * R-EN — Single source of truth for ALL user-facing strings (English).
 *
 * Tone:
 *   • Native English speaker, not translated Spanish.
 *   • Concise + scannable (Telegram users glance, don't read).
 *   • Action-oriented CTAs ("Track wallet", "Copy trade now").
 *   • Trader/DeFi vernacular: wallet, basket, leverage, notional, TWAP, PnL.
 *   • Time format: 24h `HH:mm UTC` or `HH:mm <TZ-abbrev>`.
 *
 * Keys are nested by feature so `t('start.recurring', {...})` works.
 */

module.exports = {
  // ──────────────────────── alerts (i18n.js legacy keys) ────────────────────
  alerts: {
    POSITION_CLOSED: 'Position closed',
    POSITION_OPENED: 'New position opened',
    NEW_BASKET: 'NEW BASKET OPENED',
    BASKET_CLOSED: 'BASKET CLOSED',
    TAKE_PROFIT_HIT: 'TAKE PROFIT hit',
    STOP_LOSS_TRIGGERED: 'STOP LOSS triggered',
    TRAILING_STOP_TRIGGERED: 'TRAILING STOP triggered',
    MANUAL_CLOSE: 'Manual close',
    PARTIAL_CLOSE: 'Partial close',
    COMPOUND_DETECTED: 'COMPOUNDING DETECTED',
    WALLET: 'Wallet',
    ENTRY: 'Entry',
    CLOSE: 'Close',
    PNL: 'PnL',
    LEVERAGE: 'Leverage',
    NOTIONAL: 'Notional',
    POSITIONS: 'Positions',
    POSITIONS_LIST: 'Composition',
    TOTAL_PNL: 'Total PnL',
    DURATION: 'Duration',
    NOTIONAL_BEFORE: 'Notional before',
    NOTIONAL_NOW: 'Notional now',
    GROWTH: 'Growth',
    REFERRAL_CTA: 'Use this code for 10% off Pear Protocol fees',
    AMBASSADOR_TAGLINE: 'Pear Protocol Alerts · Community Bot',
    WEEKLY_SUMMARY_TITLE: 'WEEKLY SUMMARY — Performance',
    WEEKLY_WEEK: 'Week',
    WEEKLY_PNL_NET: 'Net PnL',
    WEEKLY_TRADES: 'Trades',
    WEEKLY_WIN_RATE: 'Win rate',
    WEEKLY_VOLUME: 'Volume',
    WEEKLY_FEES: 'Fees',
    WEEKLY_BEST: 'Best',
    WEEKLY_WORST: 'Worst',
    WEEKLY_FOLLOW_CTA: 'Want to copy this style? Use the code for 10% off on Pear.',
    HEARTBEAT_OK: 'Pear Alerts Bot online',
    UPTIME: 'Uptime',
    ERRORS_24H: 'Errors 24h',
    LAST_POLL: 'Last poll',
    HISTORY_HEADER: 'RECENT CLOSES',
    HISTORY_EMPTY: 'No closes recorded yet.',
    PNL_PERIOD_HEADER: 'PnL',
    EXPORT_CAPTION: 'Closes export',
    STATUS_OK: 'Bot online',
    SUMMARY_FORCED: 'Forcing weekly summary...',
    PNL_DISCREPANCY: 'PnL DISCREPANCY DETECTED',
  },

  // ──────────────────────────── /start ──────────────────────────────────────
  start: {
    title: '🍐 *Pear Protocol Alerts*',
    first_time_intro: 'Your on-chain trading copilot. I ping you when something material happens on the wallets you follow — your own or top traders.',
    first_time_what: '*⚡ What you can do:*',
    first_time_b1: '🎯 Track top-trader wallets on HyperLiquid',
    first_time_b2: '📋 Real-time alerts when they open/close baskets',
    first_time_b3: '🔗 Copy their trades in one tap (exact pairs in Pear)',
    first_time_b4: '🎯 Monitor TP/SL and free funds',
    first_time_b5: '🏦 HyperLend borrow alerts',
    first_time_tz_hint: '🌎 Set your timezone with /timezone',
    first_time_track_hint: '📡 Start tracking with /track',
    tz_detected: '🌎 I detected your timezone as `{tz}`. Change it with /timezone if it\'s wrong.',
    recurring_welcome: 'Welcome back 👋',
    recurring_setup: '📊 *Your setup:*',
    recurring_tz: '  🌎 TZ: `{tz}`',
    recurring_wallets: '  📡 Tracked wallets: {count}/{max}',
    recurring_status: '  🟢 Bot status: active',
    kb_track_add: '🎯 Track wallet',
    kb_track_list: '📋 My wallets',
    kb_copy_trading: '🤖 Copy Trading',
    kb_status: '📊 Status',
    kb_tz: '🌎 My TZ',
    kb_learn: '📚 Learn',
    kb_pear: '🍐 Open Pear Protocol',
    track_max_reached: '🚫 You hit the {max}-wallet limit.\n\nRemove one first via /track → 📋 My wallets.',
    track_add_prompt: '📡 *Track new wallet*\n\nSend the address (`0x...`) you want to follow.\n\n💡 _Tip: you can track up to {max} top-trader wallets._\n_When they open a basket, I ping you instantly with\na one-tap copy button._\n\n(type /cancel to go back)',
    list_empty: '📋 You\'re not tracking any wallet yet.\n\nUse /track and tap *🎯 Track wallet*.',
    list_header: '📋 *YOUR TRACKED WALLETS*',
    list_total: 'Total: {count}/{max}',
    tz_menu_title: '🌎 *Your timezone*',
    tz_menu_current: 'Current: `{tz}`',
    tz_menu_howto: 'To change it:\n  • `/timezone <IANA>` (e.g. `/timezone America/New_York`)\n  • `/timezone auto` to auto-detect',
    tap_signals: 'Tap /signals.',
    tap_copyauto: 'Tap /copy_auto.',
    tap_copytrading: 'Tap /copy_trading.',
    tap_learn: 'Tap /learn.',
    tap_learn_full: 'Tap /learn to see the tutorials.',
    status_title: '📊 *Active alerts*',
    status_bot: '🟢 Bot: active',
    status_tz: '🌎 TZ: `{tz}`',
    status_wallets: '📡 Tracked wallets: {count}/{max}',
    status_recv: '_You\'ll get an alert when these wallets open or close baskets._',
    status_empty_cta: 'Tap *🎯 Track wallet* to get started.',
    muted_wallet: '🔕 Wallet `{addr}` muted. You won\'t get any more alerts from this wallet.\n\n_You can re-track it anytime with /track._',
    muted_callback: 'Wallet muted.',
    not_in_list: 'ℹ️ That wallet isn\'t in your tracking list.',
  },

  // ──────────────────────────── /track ──────────────────────────────────────
  track: {
    menu_kb_add: '➕ Add wallet',
    menu_kb_list: '📋 My tracked wallets',
    menu_kb_remove: '🔕 Stop tracking',
    menu_title: '🎯 *TRACK — External wallets*',
    menu_body: 'Track any Hyperliquid wallet and get alerts when it opens or closes baskets, with a one-tap button to copy the trade on Pear.',
    list_empty: '📋 You\'re not tracking any wallet yet.\n\nUse `/track` and tap *Add wallet*.',
    list_header: '📋 *YOUR TRACKED WALLETS*',
    list_total: 'Total: {count}/{max}',
    add_prompt: '📥 Send the wallet address (format `0x...` with 40 hex chars):',
    no_tracked: 'You\'re not tracking any wallet.',
    remove_title: '🔕 *Remove wallet*',
    remove_prompt: 'Send the address (or a shortcut like `0x6abc...`) you want to stop tracking:',
    invalid_addr: '⚠️ That address doesn\'t look valid. It must be `0x` followed by 40 hex chars (e.g. `0x1234abcd...0000`).\n\nSend it again or /cancel.',
    addr_validated: '✅ Address `{addr}` validated.\n\nWant to give it a label? (e.g. *Whale 1*) or send `/skip` to save without one.',
    save_failed: '⚠️ Couldn\'t save: {error}',
    saved_with_label: '✅ Wallet `{addr}` ({label}) tracked.\n\nI\'ll ping you when it opens or closes baskets.',
    saved_no_label: '✅ Wallet `{addr}` tracked (no label).',
    not_found: '⚠️ Couldn\'t find a wallet matching `{q}`. Use /track to see your list.',
    removed_ok: '✅ Wallet `{addr}` removed ({n} record).',
    remove_failed: '⚠️ Couldn\'t remove.',
    error_unknown: 'unknown error',
    cancelled: 'Cancelled. Send /track to start over.',
    err_invalid_addr: 'Invalid address — must be 0x + 40 hex chars',
    err_max_reached: 'You hit the {max}-wallet limit — remove one with /track before adding another.',
    err_already: 'You\'re already tracking that wallet.',
  },

  // ──────────────────────── /timezone ───────────────────────────────────────
  timezone: {
    detected_msg: 'I detected your TZ as `{tz}`. If it\'s wrong, use `/timezone <IANA>` to fix.',
    invalid: '⚠️ {error}.\n\nUse an IANA name like `America/New_York`.',
    err_invalid: 'Invalid timezone: {tz}',
  },

  // ──────────────────────── /capital ────────────────────────────────────────
  capital: {
    saved_ok: '✅ Capital set: ${amount} USDC.\n\n_I\'ll use it on the next signals._',
    error: '⚠️ {msg}\n\nUsage: `/capital <amount>` (e.g. `/capital 500`)',
    invalid_amount: 'Invalid amount',
    err_min: 'Minimum amount: ${min}',
    err_max: 'Maximum amount: ${max}',
    err_min_usdc: 'Minimum amount: ${min} USDC',
    err_max_usdc: 'Maximum amount: ${max} USDC',
  },

  // ──────────────────────── /copy_auto ──────────────────────────────────────
  copy_auto: {
    title: '🤖 *COPY AUTO*',
    risk_preset: 'Risk preset: SL {sl}% / Trailing {trailing}% activation {act}%',
    kb_howto: 'ℹ️ How it works',
    capital_set_prompt: '💰 *Set capital*\n\nUsage:\n  `/capital 500` — to set $500 USDC\n\nMin: ${min} · Max: ${max}',
    howto_title: 'ℹ️ *How it works*',
    howto_step1: '1️⃣ Toggle copy auto on.',
    howto_step3: '3️⃣ When a signal hits @BlackCatDeFiSignals, you get an alert with a one-tap Pear button and your capital pre-loaded.',
    howto_step4: '4️⃣ Click + sign in your wallet → executed.',
    howto_modes_manual: '  • *MANUAL* — alert with "Copy on Pear" button',
    howto_modes_auto: '  • *AUTO* — pre-armed alert, wording: "everything\'s ready, you sign"',
    howto_disclaimer: '⚠️ Pear has no public execution API → you always sign from your wallet (only legit way).',
    err_amount_invalid: 'Invalid amount',
  },

  // ──────────────────────── /copy_trading ───────────────────────────────────
  copy_trading: {
    pick_title: 'Pick what to copy:',
    risk_preset_global: '*Global risk preset:* SL 50% / Trailing 10% activation 30%',
    kb_howto: 'ℹ️ How it works',
    bcd_wallet_desc: 'Auto-tracking wallet `{addr}`.',
    bcd_wallet_alert: 'When it opens/closes a basket, you get an alert with a pre-configured Pear link.',
    signals_desc: 'Auto-reading @{channel}.',
    custom_count: 'Wallets you\'re copying: {n}/{max}',
    custom_empty: '_You haven\'t added any yet._',
    howto_title: 'ℹ️ *How Copy Trading works*',
    howto_signals: '📡 *BCD Signals* — the bot reads the public channel @BlackCatDeFiSignals every 30s. When there\'s a signal with a Pear link, you get it.',
    howto_custom: '👥 *Custom Wallets* — add any 0x... wallet and the bot tracks it every 60s with your configured capital.',
    howto_modes_manual: '  • MANUAL — standard "Copy on Pear" button.',
    howto_modes_auto: '  • AUTO — pre-armed alert with "everything\'s ready, you sign" wording.',
    howto_risk: '*Global risk preset:* SL 50% basket / Trailing 10% activation 30%.',
    howto_disclaimer: '⚠️ Pear has no public execution API → you always sign from your wallet.',
    capital_bcd_set: '💰 Set capital:\n\nUsage: `/capital_bcd <amount>`  (e.g. `/capital_bcd 250`)\n\nMin: ${min} · Max: ${max}',
    capital_signals_set: '💰 Set capital:\n\nUsage: `/capital_signals <amount>`  (e.g. `/capital_signals 250`)\n\nMin: ${min} · Max: ${max}',
    addr_invalid: '⚠️ Invalid address. Must be `0x` + 40 hex chars.',
    label_prompt: 'Optional label for this wallet (e.g. "Whale 1"). Or reply `skip`.',
    capital_prompt: 'Capital to use for this wallet (USDC). Min ${min}, Max ${max}. Default: ${dflt}.\n\nReply with a number or "default".',
    amount_invalid: '⚠️ Invalid amount. Try again or "default".',
    capital_current: 'Current capital: {amount}\n\nRange: ${min} – ${max}\n\nUsage: `<command> <amount>`',
    err_generic: '⚠️ {msg}',
    err_amount_invalid: 'Invalid amount',
    err_min: 'Minimum amount: ${min}',
    err_max: 'Maximum amount: ${max}',
    err_invalid_type: 'invalid type: {type}',
    err_addr_invalid: 'Invalid address — must be 0x + 40 hex chars',
    err_max_custom: 'Maximum {max} custom wallets per user.',
  },

  // ──────────────────────── /signals ────────────────────────────────────────
  signals: {
    bullets_b1: '  • Basket composition',
    bullets_b3: '  • One-tap "Copy on Pear" button',
    bullets_sl: '  • SL 50% + Trailing 10% (30% activation) — preset',
    independent: 'That alert is independent of the channel — you can subscribe to the channel anyway to see the full context.',
    tap_copyauto: 'Tap /copy_auto to configure.',
  },

  // ──────────────────────── /share ──────────────────────────────────────────
  share: {
    title: '🎁 *Share the bot*',
    your_link: 'Your unique link:',
    benefit_1: '  • You earn 1 referral',
    benefit_2: '  • After {threshold} referrals → Premium ({prem} slots vs {dflt})',
  },

  // ──────────────────────── /feedback ───────────────────────────────────────
  feedback: {
    not_configured: '⚠️ Feedback temporarily unavailable (owner not configured). Try later.',
    prompt: '💬 *Send feedback*\n\nWrite your message. I\'ll get it directly.\n\n_(type /cancel to go back)_',
    forward_failed: '⚠️ Couldn\'t forward your feedback right now (notifier not ready). Try later.',
  },

  // ──────────────────────── /portfolio ──────────────────────────────────────
  portfolio: {
    title_intro: '📊 *Your portfolio*\n\nConnect a wallet (read-only) to see your equity and positions on HyperLiquid.',
    addr_prompt: '📥 Send your address (`0x` + 40 hex). Read-only — never sign anything here.\n\n(type /cancel to go back)',
    addr_invalid: '⚠️ Invalid address. Must be `0x` + 40 hex chars.',
    err_invalid_addr: 'Invalid address (must be 0x + 40 hex)',
    err_empty: 'Empty response from HyperLiquid',
  },

  // ──────────────────────── /leaderboard ────────────────────────────────────
  leaderboard: {
    not_enough: '_Not enough data yet to build a ranking._',
    track_hint: 'Track wallets with /track so they show up here.',
    tap_to_track: '_Tap a wallet to track it:_',
    already_tracked: 'ℹ️ You\'re already tracking that wallet.',
  },

  // ──────────────────────── /learn ──────────────────────────────────────────
  learn: {
    menu_title: '📚 *Learn the bot*',
    menu_subtitle: 'Short tutorials — tap one to start:',
    not_found: '⚠️ Lesson not found.',
    page_label: '_Lesson {idx} of {total}_',

    track_title: '📘 How to track a wallet (30s)',
    track_h: '*How to track a wallet*',
    track_l1: '1️⃣ Tap /track or the *🎯 Track wallet* button on /start.',
    track_l2: '2️⃣ Paste the wallet address (format `0x...` with 40 hex chars).',
    track_l3: '3️⃣ Optional: give it an alias (e.g. "Whale 1") or tap /skip.',
    track_l4: '4️⃣ Done — you\'ll get alerts when that wallet opens/closes baskets.',
    track_premium: '_You can track up to 10 wallets (25 if you unlock Premium with 3 referrals)._',

    copyauto_title: '📗 How to set up copy auto (1min)',
    copyauto_h: '*How to set up copy auto*',
    copyauto_l1: '1️⃣ Tap /copy_auto.',
    copyauto_l3: '3️⃣ Pick a mode:',
    copyauto_l3_manual: '  • *MANUAL* — you get an alert with a "Copy on Pear" button',
    copyauto_l4: '4️⃣ Toggle *🚦 ON / OFF*.',
    copyauto_when: 'When a signal hits @BlackCatDeFiSignals, you get the direct Pear link with your capital pre-loaded and the exact basket.',
    copyauto_disclaimer: '⚠️ *You always sign from your wallet* — Pear has no public execution API. That\'s the only legit way.',

    basket_title: '📕 What is a basket on Pear (2min)',
    basket_h: '*What is a basket*',
    basket_b1: '  • Intra-theme diversification',
    basket_b2: '  • Reduces idiosyncratic risk of a single token',
    basket_example: '*Example:* L2s SHORT basket = SHORT on ARB+OP+DYDX+PYTH+ENA. If the narrative breaks, all drop together → bigger upside; if not, your SL protects you.',

    risk_sl: '🎯 *SL (Stop Loss)*: max % you\'re willing to lose. Default 50% of basket capital.',
    risk_trailing: '📈 *Trailing Stop*: SL that moves in your favor when in profit.',
    risk_trailing_act: '  • *Activation*: profit % at which trailing kicks in (default 30%).',
    risk_trailing_dist: '  • *Distance*: how much room before closing (default 10%).',
    risk_lev: '⚡ *Leverage*: multiplies gain and loss equally. Default 4x = you win/lose 4x the price move.',
    risk_rule: '_Rule: never risk more than 1-2% of your total capital on a single trade._',

    signals_title: '📒 How to read official signals (1min)',
    signals_h: '*How to read official signals*',
    signals_lev: '⚡ *Leverage*: how many x to apply',
    signals_sl: '🎯 *SL / Trailing*: risk config',
    signals_twap: '⏱️ *TWAP*: how to enter (hours + bullets)',
    signals_with_copy_on: 'If copy auto is ON, you get the personalized signal with your capital and a one-tap Pear button.',
    signals_with_copy_off: 'If not, tap /signals to subscribe to the channel manually.',
  },

  // ──────────────────────── /alerts_config ──────────────────────────────────
  alerts_config: {
    hf_critical: 'Critical HF on tracked wallets',
    tap_to_toggle: 'Tap a category to toggle on/off:',
    err_unknown_cat: 'Unknown category: {cat}',
  },

  // ──────────────────────── /help ───────────────────────────────────────────
  help: {
    copy_auto: '/copy_auto — Auto copy (MANUAL/AUTO)',
    feedback_q: 'Something off? /feedback',
  },

  // ──────────────────────── /pnl & /history & /export ───────────────────────
  history: {
    no_closes: 'No closes recorded yet.',
    invalid_period: 'Invalid period. Use: {valid}',
    no_events: 'No events for period {period}.',
    no_week: 'No closes this week. Come back Sunday 18:00 UTC.',
    ttl_days: 'TTL: {days} days',
  },

  // ──────────────────────── /menu (bot operator dashboard) ──────────────────
  menu: {
    monitored: '✅ You have *{count} wallet(s)* monitored.',
    add_wallet_cta: 'Tap ➕ *Add Wallet* to get started.',
  },

  // ──────────────────────── stats ───────────────────────────────────────────
  stats: {
    days_active: '📅 Days active: {n} day{plural}',
  },

  // ──────────────────────── /signals_channel intel + alerts ─────────────────
  externalWallet: {
    intel_signal: '💡 Intel: possible market signal.',
    entry_was: '💲 Previous entry: ${px}',
    intel_close: '💡 Intel: possible close signal — review your position.',
  },

  // ──────────────────────── compounding ─────────────────────────────────────
  compounding: {
    size_increased: 'Active basket size increased.',
    capital_added: 'Capital added to position — compounding (TWAP entry).',
  },

  // ──────────────────────── alert composition (open/close) ──────────────────
  alertComp: {
    composition: '📊 Composition ({n}):',
    composition_positions: '📊 Composition ({n} positions):',
    duration: '⏱ Duration: {d}',
    closed_label: 'Position closed (trailing/manual)',
  },

  // ──────────────────────── copy alert builder ──────────────────────────────
  copyAlert: {
    composition: '📊 Composition ({n}):',
    risk: '🎯 Risk: SL {sl}% / Trailing {trailing}% activation {act}%',
    auto_mode: '_AUTO mode — link pre-armed, you sign in your wallet._',
    manual_mode: '_Tap the button to open Pear with the basket pre-loaded._',
    sl_trailing: '🎯 SL {sl}% / Trailing {trailing}% activation {act}%',
    pre_loaded: '_Tap the button to open Pear with the basket pre-loaded. Sign in your wallet to execute._',
    direct_link: 'I prepared the direct link. Click + sign in your wallet:',
    no_api: '_Pear has no public execution API — you always sign from your wallet (only legit way)._',
  },

  // ──────────────────────── monitor (open alert title) ──────────────────────
  monitor: {
    new_position: '📈 *New position opened*',
  },

  // ──────────────────────── pnl cross-validation ────────────────────────────
  pnlXval: {
    pear_unavail: 'Pear API unavailable — using bot calc',
    pear_no_pnl: 'Pear API has no PnL — using bot calc',
  },

  // ──────────────────────── alert button labels ────────────────────────────
  alertBtn: {
    cta_default: '⚡ Replicate this trade in one tap:',
  },

  // ──────────────────────── daily digest ────────────────────────────────────
  digest: {
    more_wallets: '  _... and {n} more_',
    config_hint: '_To configure which alerts you get: /alerts_config_',
  },

  // ──────────────────────── walletTracker scheduler ─────────────────────────
  scheduler: {
    composition: '📊 Composition ({n}):',
  },

  // ──────────────────────── short hand for `cancel` flow shared across cmds ─
  common: {
    err_unknown: 'unknown error',
  },
};
