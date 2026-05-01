'use strict';

/**
 * R-AUTOCOPY — User feedback channel.
 *
 * /feedback opens an in-bot text capture (state machine), and the next
 * plain-text message is forwarded to OWNER_USER_ID via the bot's notifier.
 *
 * No public storage — feedback is forwarded directly so the owner sees it
 * in their own Telegram chat. We do persist a simple JSON log for audit
 * (date, userId, length) without the body so we don't leak private data.
 */

const fs = require('fs');
const path = require('path');

const MAX_FEEDBACK_LEN = 2000;

function _ownerUserId() {
  const v = process.env.OWNER_USER_ID || process.env.BCD_TELEGRAM_CHAT_ID || '';
  const n = parseInt(v, 10);
  return Number.isFinite(n) && n !== 0 ? n : null;
}

function _resolveLogPath() {
  return (
    process.env.FEEDBACK_LOG_PATH ||
    path.join(
      process.env.RAILWAY_VOLUME_MOUNT_PATH || '/app/data',
      'feedback_audit.json'
    )
  );
}

const LOG_PATH = _resolveLogPath();

function _logAudit(userId, len) {
  try {
    fs.mkdirSync(path.dirname(LOG_PATH), { recursive: true });
    let arr = [];
    if (fs.existsSync(LOG_PATH)) {
      const raw = fs.readFileSync(LOG_PATH, 'utf-8');
      try { arr = JSON.parse(raw) || []; } catch (_) {}
    }
    if (!Array.isArray(arr)) arr = [];
    arr.push({ ts: Date.now(), userId: String(userId), len });
    if (arr.length > 500) arr = arr.slice(-500);
    fs.writeFileSync(LOG_PATH, JSON.stringify(arr, null, 2));
  } catch (e) {
    console.error('[feedback] audit log failed:', e && e.message ? e.message : e);
  }
}

function truncate(text) {
  if (typeof text !== 'string') return '';
  return text.length > MAX_FEEDBACK_LEN
    ? text.slice(0, MAX_FEEDBACK_LEN) + '\n\n_(truncado)_'
    : text;
}

/**
 * Forward a feedback message to the owner.
 *
 *   forwardFeedback({
 *     notify,
 *     fromUserId,
 *     fromUsername,    // optional Telegram @handle
 *     text,
 *   })
 *
 * Returns { ok: true } or { ok: false, error: '...' }
 */
async function forwardFeedback({ notify, fromUserId, fromUsername, text }) {
  if (typeof notify !== 'function') {
    return { ok: false, error: 'notify_not_attached' };
  }
  const owner = _ownerUserId();
  if (!owner) {
    return { ok: false, error: 'owner_user_id_not_set' };
  }
  const safe = truncate(text);
  const handle = fromUsername ? `@${fromUsername}` : `(no username)`;
  const body = [
    '📬 *FEEDBACK USUARIO*',
    '',
    `De: \`${fromUserId}\` — ${handle}`,
    `Largo: ${safe.length} chars`,
    '',
    safe,
  ].join('\n');
  try {
    await notify(owner, body, { parse_mode: 'Markdown' });
    _logAudit(fromUserId, safe.length);
    return { ok: true };
  } catch (e) {
    return { ok: false, error: e && e.message ? e.message : 'send_failed' };
  }
}

function ownerConfigured() {
  return _ownerUserId() != null;
}

module.exports = {
  MAX_FEEDBACK_LEN,
  LOG_PATH,
  forwardFeedback,
  ownerConfigured,
  truncate,
  _ownerUserId,
};
