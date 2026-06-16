'use strict';

/**
 * Round v2 — Automatic weekly performance summary.
 *
 * Schedule: every Sunday at 18:00 UTC by default. Pulls all CLOSE events
 * from eventLog since start of the ISO week and emits a single summary
 * message (with referral CTA) to BCD's chat.
 *
 * Env vars:
 *   WEEKLY_SUMMARY_ENABLED   default true
 *   WEEKLY_SUMMARY_DOW       default 0 (Sunday)
 *   WEEKLY_SUMMARY_HOUR_UTC  default 18
 *   WEEKLY_SUMMARY_CHAT_ID   target chat (defaults to BCD_TELEGRAM_CHAT_ID)
 */

const weeklyPnl = require('./weeklyPnl');
const { appendFooter } = require('./branding');
const { t } = require('./i18n');

function isEnabled() {
  return (
    (process.env.WEEKLY_SUMMARY_ENABLED || 'true').toLowerCase() !== 'false'
  );
}

function _startOfWeekUTC(date) {
  const d = new Date(date);
  const dow = d.getUTCDay();
  const monday = new Date(
    Date.UTC(
      d.getUTCFullYear(),
      d.getUTCMonth(),
      d.getUTCDate() - ((dow + 6) % 7)
    )
  );
  monday.setUTCHours(0, 0, 0, 0);
  return monday;
}

function _weekNumber(date) {
  return weeklyPnl.weekNumber(date);
}

function _fmtUsd(n) {
  if (!Number.isFinite(n)) return 'n/d';
  const abs = Math.abs(n).toFixed(2);
  return n >= 0 ? `+$${abs}` : `-$${abs}`;
}

/**
 * Build the weekly summary message from REAL Hyperliquid fills (closedPnl).
 *
 * @param {object} opts
 *   hlApi : HyperliquidApi instance (required to fetch fills)
 *   now   : Date override (tests)
 * @returns {Promise<string|null>} message, or null when there were genuinely
 *   zero fills this week (nothing to send).
 */
async function buildSummaryMessage(opts = {}) {
  const { hlApi = null, now = new Date() } = opts;
  const res = await weeklyPnl.buildWeekly(hlApi, { now });
  if (res === null) {
    return null; // genuinely no activity
  }

  const start = new Date(res.startMs);
  const end = new Date(res.endMs);
  const header = [
    `📊 *${t('WEEKLY_SUMMARY_TITLE')}*`,
    '',
    `📅 ${t('WEEKLY_WEEK')} ${_weekNumber(end)} (${start
      .toISOString()
      .slice(0, 10)} → ${end.toISOString().slice(0, 10)})`,
  ];

  // FIX 3 — hard fetch failure: never fabricate a flat week.
  if (res.fetchError || !res.summary) {
    const lines = header.concat([
      `⚠️ ${t('WEEKLY_FETCH_ERROR')}`,
    ]);
    return appendFooter(lines.join('\n'), true);
  }

  const s = res.summary;

  // FIX 3 — calculation failure detected (activity but no realized closes).
  if (s.calc_failure) {
    const lines = header.concat([
      `⚠️ ${t('WEEKLY_CALC_ERROR')}`,
      `📊 ${t('WEEKLY_FILLS')}: ${s.fills.toLocaleString()} · ${t('WEEKLY_VOLUME')}: $${Math.round(s.volume).toLocaleString()}`,
    ]);
    return appendFooter(lines.join('\n'), true);
  }

  const winRateStr =
    s.win_rate_pct === null ? 'n/d' : `${s.win_rate_pct.toFixed(1)}%`;

  const lines = header.concat([
    `💰 ${t('WEEKLY_PNL_NET')}: ${_fmtUsd(s.net_pnl)}`,
    `📊 ${t('WEEKLY_FILLS')}: ${s.fills.toLocaleString()}`,
    `🔁 ${t('WEEKLY_REALIZED_CLOSES')}: ${s.realized_closes} (${s.wins}W / ${s.losses}L${s.breakeven ? ` / ${s.breakeven}BE` : ''})`,
    `🎯 ${t('WEEKLY_WIN_RATE')}: ${winRateStr}`,
  ]);
  if (s.volume > 0) {
    lines.push(
      `💸 ${t('WEEKLY_VOLUME')}: $${Math.round(s.volume).toLocaleString()}`
    );
  }
  if (s.total_fees > 0) {
    lines.push(`💵 ${t('WEEKLY_FEES')}: $${s.total_fees.toFixed(2)}`);
  }
  if (s.best && Number.isFinite(s.best.pnl)) {
    lines.push(`🏆 ${t('WEEKLY_BEST')}: ${s.best.coin} ${_fmtUsd(s.best.pnl)}`);
  }
  if (s.worst && Number.isFinite(s.worst.pnl) && s.worst.coin !== (s.best && s.best.coin)) {
    lines.push(`💀 ${t('WEEKLY_WORST')}: ${s.worst.coin} ${_fmtUsd(s.worst.pnl)}`);
  }
  if (res.partial) {
    lines.push(`ℹ️ ${t('WEEKLY_PARTIAL')}`);
  }
  return appendFooter(lines.join('\n'), true);
}

async function sendWeeklySummary(notifier, chatId, opts = {}) {
  const msg = await buildSummaryMessage(opts);
  if (!msg) {
    console.log('[weeklySummary] skipping — no fills this week');
    return false;
  }
  try {
    await notifier(chatId, msg);
    return true;
  } catch (e) {
    console.error(
      '[weeklySummary] send failed:',
      e && e.message ? e.message : e
    );
    return false;
  }
}

/**
 * Sunday-18:00-UTC scheduler. We loop with setTimeout aligning to the next
 * tick boundary instead of pulling in node-cron. Every minute we check
 * whether (DOW, HOUR_UTC) match. We track a "last_sent" stamp to avoid
 * double-firing in the same hour.
 */
function startSchedule(notifier, chatId, opts = {}) {
  if (!isEnabled()) {
    console.log('[weeklySummary] disabled');
    return null;
  }
  const hlApi = opts.hlApi || null;
  const targetDow = parseInt(
    process.env.WEEKLY_SUMMARY_DOW || '0',
    10
  );
  const targetHour = parseInt(
    process.env.WEEKLY_SUMMARY_HOUR_UTC || '18',
    10
  );
  let lastSentKey = null;
  console.log(
    `[weeklySummary] scheduled DOW=${targetDow} HOUR_UTC=${targetHour}`
  );
  const tick = setInterval(() => {
    const now = new Date();
    if (
      now.getUTCDay() === targetDow &&
      now.getUTCHours() === targetHour
    ) {
      const key = `${now.getUTCFullYear()}-W${_weekNumber(now)}`;
      if (key !== lastSentKey) {
        lastSentKey = key;
        sendWeeklySummary(notifier, chatId, { hlApi }).catch((e) =>
          console.error(
            '[weeklySummary] tick error:',
            e && e.message ? e.message : e
          )
        );
      }
    }
  }, 60 * 1000);
  if (typeof tick.unref === 'function') tick.unref();
  return tick;
}

module.exports = {
  isEnabled,
  buildSummaryMessage,
  sendWeeklySummary,
  startSchedule,
};
