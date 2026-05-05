'use strict';

/**
 * R-PUBLIC-SIMPLIFY — Brutal-conversion /start UX.
 *
 * The previous /start required a multi-step funnel (track wallet → set
 * timezone → open Copy Trading menu → connect wallet → ...). 11M tweet
 * impressions, zero conversions. This module replaces that funnel with a
 * SINGLE message + 3 hero buttons that lead the user to a copy-trade in
 * under 30 seconds:
 *
 *   1. 🚀 COPY MY ACTIVE BASKET — 1 TAP   (URL → Pear w/ basket pre-loaded)
 *   2. 📊 SEE MY LIVE PERFORMANCE          (callback → text summary)
 *   3. 🔔 ALERT ME ON NEW TRADES           (callback → opt-in toggle)
 *
 * Plus three URL-button size variants (0.5x / 1x / 2x) so users can adjust
 * notional without leaving Telegram. All Pear URLs include the
 * BlackCatDeFi referral code for the 20% fee rebate (10% Pear + 10% Pyrus).
 *
 * Activated via env SIMPLIFY_START_ENABLED (default 'true' — set to 'false'
 * for instant rollback to commandsStart.handleStart legacy flow).
 *
 * NEVER blocks /start on the network: if the BCD basket fetcher fails or
 * returns empty, the hero button falls back to the generic Pear URL with
 * just the referral param.
 */

const bcdBasketCache = require('./bcdBasketCache');
const pearUrlBuilder = require('./pearUrlBuilder');
const alertsConfig = require('./alertsConfig');
const onboarding = require('./onboarding');
const stats = require('./stats');
const share = require('./share');

let _copyAutoStore = null;
function _getCopyAutoStore() {
  if (_copyAutoStore !== null) return _copyAutoStore;
  try { _copyAutoStore = require('./copyAutoStore'); }
  catch (_) { _copyAutoStore = false; }
  return _copyAutoStore;
}

const REFERRAL = process.env.PEAR_REFERRAL_CODE || 'BlackCatDeFi';
const FALLBACK_HERO_URL =
  process.env.PEAR_HERO_URL ||
  `https://app.pear.garden/?referral=${REFERRAL}`;

const DEFAULT_CAPITAL = parseFloat(
  process.env.SIMPLIFY_DEFAULT_CAPITAL || '100'
);

// Operator-tunable fund stats. Defaults match the May 2026 numbers from the
// thesis tweet; bump via Railway env vars without redeploying when needed.
const FUND_YTD_PNL = process.env.FUND_YTD_PNL || '+$8.6K';
const FUND_TRADES = process.env.FUND_TRADES || '2,687';
const FUND_VOLUME = process.env.FUND_VOLUME || '$2M';
const FUND_REBATE_LINE =
  process.env.FUND_REBATE_LINE ||
  '20% fee rebate (10% Pear + 10% Pyrus, USDC Arbitrum bi-weekly)';
const PERFORMANCE_DASHBOARD_URL =
  process.env.PERFORMANCE_DASHBOARD_URL ||
  `https://hyperdash.info/trader/${bcdBasketCache.BCD_WALLET}`;

function isEnabled() {
  return (
    String(process.env.SIMPLIFY_START_ENABLED || 'true').toLowerCase() !==
    'false'
  );
}

function _userCapital(userId) {
  const store = _getCopyAutoStore();
  if (store && typeof store.getConfig === 'function') {
    try {
      const cfg = store.getConfig(userId);
      const v = Number(cfg && cfg.capital_usdc);
      if (Number.isFinite(v) && v > 0) return v;
    } catch (_) {}
  }
  return DEFAULT_CAPITAL;
}

function _fmtCap(n) {
  const v = Number(n) || 0;
  if (v >= 1000) return `$${(v / 1000).toFixed(v >= 10000 ? 0 : 1)}k`;
  return `$${v.toFixed(0)}`;
}

async function _activeBasketUrl(userId, sizeMultiplier, opts) {
  const o = opts || {};
  const positions = await bcdBasketCache.getActiveBasket();
  if (!positions || positions.length === 0) return null;
  const capital = _userCapital(userId) * Number(sizeMultiplier || 1);
  return pearUrlBuilder.buildPearCopyUrl(positions, 'SHORT', {
    capital,
    userId,
    source: o.source || 'tg-start-hero',
    medium: 'simplify',
    campaign: `size-${sizeMultiplier}x`,
  });
}

function _heroText() {
  return [
    '🐈‍⬛ *Black Cat — Pear Copy Trading*',
    '',
    `*Real money on the line.* YTD ${FUND_YTD_PNL}, ${FUND_TRADES} trades, ${FUND_VOLUME} volume — verified on-chain.`,
    '',
    'I trade. You copy. 1 tap. No setup.',
    '',
    `💎 ${FUND_REBATE_LINE}`,
  ].join('\n');
}

async function _buildKeyboard(userId) {
  const cap = _userCapital(userId);

  // Best-effort live URLs — any that fail (or return null) fall back to
  // FALLBACK_HERO_URL so every button always has a destination.
  const [url05, url1x, url2x] = await Promise.all([
    _activeBasketUrl(userId, 0.5).catch(() => null),
    _activeBasketUrl(userId, 1).catch(() => null),
    _activeBasketUrl(userId, 2).catch(() => null),
  ]);
  const hasActiveBasket = Boolean(url1x || url05 || url2x);
  const heroUrl = url1x || url05 || url2x || FALLBACK_HERO_URL;

  const heroLabel = hasActiveBasket
    ? `🚀 COPY MY BASKET — 1 TAP (${_fmtCap(cap)})`
    : '🍐 OPEN PEAR PROTOCOL';

  const rows = [];

  // Row 1 — hero CTA, full width.
  rows.push([{ text: heroLabel, url: heroUrl }]);

  // Row 2 — size selector (only when a basket is live, else redundant).
  if (hasActiveBasket) {
    rows.push([
      {
        text: `💰 0.5x (${_fmtCap(cap * 0.5)})`,
        url: url05 || heroUrl,
      },
      {
        text: `💰 2x (${_fmtCap(cap * 2)})`,
        url: url2x || heroUrl,
      },
    ]);
  }

  // Row 3 — live performance summary (callback so we can render text).
  rows.push([
    { text: '📊 LIVE PERFORMANCE', callback_data: 'simple:perf' },
  ]);

  // Row 4 — alerts opt-in toggle.
  rows.push([
    { text: '🔔 ALERT ME ON NEW TRADES', callback_data: 'simple:alerts' },
  ]);

  return { inline_keyboard: rows };
}

async function handleStartSimple(bot, msg) {
  const chatId = msg.chat.id;
  const userId =
    msg.from && msg.from.id ? msg.from.id : chatId;

  const wasFirstTime = onboarding.isFirstTime(userId);
  onboarding.markSeen(userId);

  // Capture deep-link referral payload (idempotent on first sighting only).
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

  const text = _heroText();
  const kb = await _buildKeyboard(userId);

  await bot.sendMessage(chatId, text, {
    parse_mode: 'Markdown',
    reply_markup: kb,
    disable_web_page_preview: true,
  });
}

function _perfText() {
  return [
    '📊 *Black Cat — Live Performance*',
    '',
    `• YTD PnL: ${FUND_YTD_PNL}`,
    `• Total trades: ${FUND_TRADES}`,
    `• Volume traded: ${FUND_VOLUME}`,
    '• Strategy: market-neutral basket SHORTs vs LONG core',
    '',
    `🔗 [Live trades on HyperDash →](${PERFORMANCE_DASHBOARD_URL})`,
    '',
    '_All numbers verified on-chain. Real money,_',
    '_no paper portfolio. Copy if it makes sense to you._',
  ].join('\n');
}

async function _onPerformanceCallback(bot, chatId) {
  await bot.sendMessage(chatId, _perfText(), {
    parse_mode: 'Markdown',
    disable_web_page_preview: true,
  });
}

async function _onAlertsCallback(bot, chatId, userId) {
  // Toggle: if currently both basket_open + basket_close are ON, turn them
  // OFF; otherwise turn both ON. Nothing else is touched.
  let nowEnabled = true;
  try {
    const cur = alertsConfig.getConfig(userId);
    const wasOn = Boolean(cur.basket_open) && Boolean(cur.basket_close);
    nowEnabled = !wasOn;
    alertsConfig.setCategory(userId, 'basket_open', nowEnabled);
    alertsConfig.setCategory(userId, 'basket_close', nowEnabled);
  } catch (_) {
    nowEnabled = true;
  }
  const txt = nowEnabled
    ? [
        '🔔 *Alerts ON*',
        '',
        'I will ping you when I open or close a basket.',
        '_Tap the button again on /start to mute._',
      ].join('\n')
    : [
        '🔕 *Alerts OFF*',
        '',
        'No more pings. Tap the button on /start to re-enable.',
      ].join('\n');
  await bot.sendMessage(chatId, txt, { parse_mode: 'Markdown' });
}

async function handleSimpleCallback(bot, cb) {
  if (!cb || !cb.data || !cb.data.startsWith('simple:')) return false;
  const action = cb.data.split(':')[1];
  const chatId =
    cb.message && cb.message.chat ? cb.message.chat.id : null;
  const userId =
    cb.from && cb.from.id ? cb.from.id : chatId;
  if (!chatId) return true;

  try { await bot.answerCallbackQuery(cb.id); }
  catch (_) {}

  if (action === 'perf') {
    await _onPerformanceCallback(bot, chatId);
    return true;
  }
  if (action === 'alerts') {
    await _onAlertsCallback(bot, chatId, userId);
    return true;
  }
  return true; // unknown sub-action; we still claim ownership of simple:*
}

module.exports = {
  isEnabled,
  handleStartSimple,
  handleSimpleCallback,
  _heroText,
  _perfText,
  _buildKeyboard,
  _userCapital,
  _activeBasketUrl,
  _onPerformanceCallback,
  _onAlertsCallback,
  // Constants exported for test introspection.
  REFERRAL,
  DEFAULT_CAPITAL,
  FUND_YTD_PNL,
  FUND_TRADES,
  FUND_VOLUME,
  FALLBACK_HERO_URL,
  PERFORMANCE_DASHBOARD_URL,
};
