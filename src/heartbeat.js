'use strict';

/**
 * R-NOSPAM (2 may 2026) — heartbeat is now DISABLED BY DEFAULT.
 *
 * Original purpose (R(v2)): periodic "✅ Pear Alerts Bot online" message
 * every 6h to BCD's chat to confirm liveness.
 *
 * Why we killed the broadcast:
 *   On Sat 2 may 2026 09:29 AR (12:29 UTC) the heartbeat woke BCD and his
 *   girlfriend mid-sleep with sound notifications. Heartbeats are
 *   monitoring telemetry — they belong in Railway logs / health endpoint,
 *   NOT in a Telegram broadcast that vibrates phones.
 *
 * New behavior:
 *   - HEARTBEAT_ENABLED defaults to 'false'. The bot must NEVER auto-emit
 *     heartbeats unless an operator sets HEARTBEAT_ENABLED=true explicitly.
 *   - sendHeartbeat() returns false immediately when disabled, with no
 *     side effects.
 *   - startSchedule() logs "[heartbeat] disabled (R-NOSPAM)" and returns
 *     null, never installs a setInterval.
 *
 * Liveness can still be monitored via:
 *   - GET /health (port 8080) — returns uptime + last-poll + error count
 *   - Railway logs — `[monitor] Polling every Ns` heartbeat
 *
 * See docs/PUBLIC_BOT_RULES.md for the canonical rule:
 *   "Public bot = silence by default. Heartbeats/uptime/status NEVER
 *    broadcast — that's logs/health endpoint territory."
 */

const healthServer = require('./healthServer');

const INTERVAL_HOURS = parseFloat(
  process.env.HEARTBEAT_INTERVAL_HOURS || '6'
);

function isEnabled() {
  // R-NOSPAM: default flipped from 'true' → 'false'. Heartbeat must be
  // explicitly opted into via env var, never on by default.
  return (
    (process.env.HEARTBEAT_ENABLED || 'false').toLowerCase() === 'true'
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
    // disable_notification=true so even if an operator opts in, the
    // heartbeat never wakes a sleeping user with a sound notification.
    await notifier(chatId, msg, { silent: true, disable_notification: true });
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
    console.log('[heartbeat] disabled (R-NOSPAM — public bot must not broadcast uptime/status)');
    return null;
  }
  const intervalMs = INTERVAL_HOURS * 60 * 60 * 1000;
  console.log(
    `[heartbeat] scheduled every ${INTERVAL_HOURS}h (HEARTBEAT_ENABLED=true override)`
  );
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
