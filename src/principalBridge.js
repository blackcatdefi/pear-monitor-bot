'use strict';

/**
 * Round v2 — Feature 3.3: Bidirectional bridge to the Python bot principal
 * (Railway service `amusing-acceptance`).
 *
 * Why a bridge? The Node alerts bot (gentle-luck) sees Pear/HL closes in real
 * time. The Python bot owns positions state, fund accounting, and the user's
 * /reporte. When a basket closes, /reporte should know within seconds without
 * waiting on the next polling cycle.
 *
 * Strategy:
 *   1. Append an event to the JSONL volume at $RAILWAY_VOLUME_MOUNT_PATH/data/events.jsonl.
 *      Python tails this on demand (already wired in eventLog.js).
 *   2. Optionally POST a webhook to PRINCIPAL_WEBHOOK_URL with the event payload
 *      (HMAC-SHA256 signed via PRINCIPAL_WEBHOOK_SECRET if set).
 *
 * Both paths are best-effort and never throw — alerting must never be blocked
 * by integration outages.
 */

const crypto = require('crypto');
const axios = require('axios');

const { recordEvent } = require('./eventLog');

const WEBHOOK_URL = process.env.PRINCIPAL_WEBHOOK_URL || '';
const WEBHOOK_SECRET = process.env.PRINCIPAL_WEBHOOK_SECRET || '';
const WEBHOOK_TIMEOUT_MS = parseInt(
  process.env.PRINCIPAL_WEBHOOK_TIMEOUT_MS || '4000',
  10
);
const WEBHOOK_ENABLED = Boolean(WEBHOOK_URL) &&
  (process.env.PRINCIPAL_WEBHOOK_ENABLED || 'true').toLowerCase() !== 'false';

let _stats = { sent: 0, failed: 0, last_attempt_at: null, last_status: null };

function _sign(body) {
  if (!WEBHOOK_SECRET) return '';
  return crypto.createHmac('sha256', WEBHOOK_SECRET).update(body).digest('hex');
}

async function _postWebhook(payload) {
  if (!WEBHOOK_ENABLED) return { skipped: true };
  const body = JSON.stringify(payload);
  const headers = {
    'Content-Type': 'application/json',
    'User-Agent': 'pear-alerts-rv2/1.0',
  };
  const sig = _sign(body);
  if (sig) headers['X-Pear-Signature'] = `sha256=${sig}`;
  try {
    const res = await axios.post(WEBHOOK_URL, body, {
      timeout: WEBHOOK_TIMEOUT_MS,
      headers,
      validateStatus: () => true,
    });
    _stats.sent += 1;
    _stats.last_attempt_at = Date.now();
    _stats.last_status = res.status;
    return { ok: res.status >= 200 && res.status < 300, status: res.status };
  } catch (e) {
    _stats.failed += 1;
    _stats.last_attempt_at = Date.now();
    _stats.last_status = e && e.code ? e.code : 'NET_ERR';
    return { ok: false, error: e && e.message ? e.message : String(e) };
  }
}

/**
 * publish — main entry. Records to JSONL log AND fires webhook in parallel.
 * Returns a single object with both outcomes; never throws.
 */
async function publish(event) {
  const enriched = {
    ...event,
    source: event.source || 'pear-alerts',
    emitted_at: event.emitted_at || new Date().toISOString(),
  };
  let logged = false;
  try {
    recordEvent(enriched);
    logged = true;
  } catch (e) {
    console.warn('[principalBridge] eventLog.recordEvent failed:', e && e.message);
  }
  // Webhook is fire-and-forget but await briefly so callers can opt to log result
  let webhook = { skipped: true };
  try {
    webhook = await _postWebhook(enriched);
  } catch (e) {
    webhook = { ok: false, error: String(e) };
  }
  return { logged, webhook };
}

function getStats() {
  return {
    webhook_enabled: WEBHOOK_ENABLED,
    webhook_url_configured: Boolean(WEBHOOK_URL),
    sent: _stats.sent,
    failed: _stats.failed,
    last_attempt_at: _stats.last_attempt_at
      ? new Date(_stats.last_attempt_at).toISOString()
      : null,
    last_status: _stats.last_status,
  };
}

function _resetForTests() {
  _stats = { sent: 0, failed: 0, last_attempt_at: null, last_status: null };
}

module.exports = {
  publish,
  getStats,
  WEBHOOK_ENABLED,
  WEBHOOK_URL,
  _resetForTests,
};
