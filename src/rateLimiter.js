'use strict';

/**
 * Round v2 — Defensive rate limiter for outgoing alerts.
 * Bug class to defend against: a runaway loop or burst pathological case
 * spamming the chat 50 alerts in 1 second. Ceiling: MAX_ALERTS_PER_MINUTE.
 *
 * canSendAlert() returns false when the cap is hit and logs a warning.
 * The limiter is process-global (not per-chat) on purpose — we want to
 * cap *outbound traffic* irrespective of who the recipient is. If you need
 * per-chat granularity, wrap with a Map keyed on chatId.
 */

const MAX_ALERTS_PER_MINUTE = parseInt(
  process.env.RATE_LIMIT_ALERTS_PER_MINUTE || '20',
  10
);

const _timestamps = [];
let _droppedTotal = 0;

function _purge(now) {
  const oneMinuteAgo = now - 60 * 1000;
  while (_timestamps.length > 0 && _timestamps[0] < oneMinuteAgo) {
    _timestamps.shift();
  }
}

function canSendAlert() {
  const now = Date.now();
  _purge(now);
  if (_timestamps.length >= MAX_ALERTS_PER_MINUTE) {
    _droppedTotal += 1;
    console.warn(
      `[rateLimiter] cap=${MAX_ALERTS_PER_MINUTE}/min reached; dropping alert. total_dropped=${_droppedTotal}`
    );
    return false;
  }
  _timestamps.push(now);
  return true;
}

function getStats() {
  const now = Date.now();
  _purge(now);
  return {
    alerts_last_60s: _timestamps.length,
    cap_per_minute: MAX_ALERTS_PER_MINUTE,
    total_dropped_lifetime: _droppedTotal,
  };
}

function _resetForTests() {
  _timestamps.length = 0;
  _droppedTotal = 0;
}

module.exports = {
  canSendAlert,
  getStats,
  MAX_ALERTS_PER_MINUTE,
  _resetForTests,
};
