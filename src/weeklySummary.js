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

const eventLog = require('./eventLog');
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
  const target = new Date(date.valueOf());
  const dayNr = (target.getUTCDay() + 6) % 7;
  target.setUTCDate(target.getUTCDate() - dayNr + 3);
  const firstThursday = new Date(Date.UTC(target.getUTCFullYear(), 0, 4));
  return (
    1 +
    Math.round(
      ((target - firstThursday) / 86400000 -
        3 +
        ((firstThursday.getUTCDay() + 6) % 7)) /
        7
    )
  );
}

function _fmtUsd(n) {
  if (!Number.isFinite(n)) return '$0.00';
  const abs = Math.abs(n).toFixed(2);
  return n >= 0 ? `+$${abs}` : `-$${abs}`;
}

function buildSummaryMessage() {
  const start = _startOfWeekUTC(new Date());
  const closes = eventLog.closesSince(start.getTime());
  if (closes.length === 0) return null;
  const s = eventLog.summarize(closes);
  const today = new Date();
  const lines = [
    `📊 *${t('WEEKLY_SUMMARY_TITLE')}*`,
    '',
    `📅 ${t('WEEKLY_WEEK')} ${_weekNumber(today)} (${start.toISOString().slice(0, 10)} → ${today.toISOString().slice(0, 10)})`,
    `💰 ${t('WEEKLY_PNL_NET')}: ${_fmtUsd(s.total_pnl)}`,
    `📈 ${t('WEEKLY_TRADES')}: ${s.count} (${s.wins}W / ${s.losses}L)`,
    `🎯 ${t('WEEKLY_WIN_RATE')}: ${s.win_rate_pct.toFixed(1)}%`,
  ];
  if (s.total_notional > 0) {
    lines.push(
      `💸 ${t('WEEKLY_VOLUME')}: $${Math.round(s.total_notional).toLocaleString()}`
    );
  }
  if (s.total_fees > 0) {
    lines.push(`💵 ${t('WEEKLY_FEES')}: $${s.total_fees.toFixed(2)}`);
  }
  if (s.best && Number.isFinite(s.best.pnl)) {
    lines.push(
      `🏆 ${t('WEEKLY_BEST')}: ${s.best.coin} ${_fmtUsd(s.best.pnl)}`
    );
  }
  if (s.worst && Number.isFinite(s.worst.pnl) && s.worst !== s.best) {
    lines.push(
      `💀 ${t('WEEKLY_WORST')}: ${s.worst.coin} ${_fmtUsd(s.worst.pnl)}`
    );
  }
  return appendFooter(lines.join('\n'), true);
}

async function sendWeeklySummary(notifier, chatId) {
  const msg = buildSummaryMessage();
  if (!msg) {
    console.log('[weeklySummary] skipping — no closes this week');
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
function startSchedule(notifier, chatId) {
  if (!isEnabled()) {
    console.log('[weeklySummary] disabled');
    return null;
  }
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
        sendWeeklySummary(notifier, chatId).catch((e) =>
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
