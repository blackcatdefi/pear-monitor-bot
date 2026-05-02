'use strict';

/**
 * Round v2 — Periodic heartbeat to confirm the bot is alive.
 * Sends a silent message to BCD's chat every HEARTBEAT_INTERVAL_HOURS.
 * Default interval: 6h.
 */

const healthServer = require('./healthServer');

const INTERVAL_HOURS = parseFloat(
  process.env.HEARTBEAT_INTERVAL_HOURS || '6'
);

function isEnabled() {
  return (
    (process.env.HEARTBEAT_ENABLED || 'true').toLowerCase() !== 'false'
  );
}

function _formatHours(ms) {
  return (ms / (60 * 60 * 1000)).toFixed(1);
}

async function sendHeartbeat(notifier, chatId) {
  if (!isEnabled()) return false;
  const status = healthServer.getStatus();
  const msg = [
    `✅ Pear Alerts Bot online`,
    `· Uptime ${_formatHours(status.uptime_ms)}h`,
    `· Errors 24h: ${status.errors_24h_count}`,
    status.last_successful_poll
      ? `· Last poll: ${status.last_successful_poll.replace('T', ' ').slice(0, 16)} UTC`
      : '· Last poll: never',
  ].join('\n');
  try {
    await notifier(chatId, msg, { silent: true });
    return true;
  } catch (e) {
    console.error(
      '[heartbeat] send failed:',
      e && e.message ? e.message : e
    );
    return false;
  }
}

function startSchedule(notifier, chatId) {
  if (!isEnabled()) {
    console.log('[heartbeat] disabled');
    return null;
  }
  const intervalMs = INTERVAL_HOURS * 60 * 60 * 1000;
  console.log(`[heartbeat] scheduled every ${INTERVAL_HOURS}h`);
  const tick = setInterval(() => {
    sendHeartbeat(notifier, chatId).catch((e) =>
      console.error('[heartbeat] tick error:', e && e.message ? e.message : e)
    );
  }, intervalMs);
  if (typeof tick.unref === 'function') tick.unref();
  return tick;
}

module.exports = {
  isEnabled,
  sendHeartbeat,
  startSchedule,
  INTERVAL_HOURS,
};
