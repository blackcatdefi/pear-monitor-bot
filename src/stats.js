'use strict';

/**
 * R-AUTOCOPY — Per-user usage statistics.
 *
 * Lightweight counter store for /stats display. We track:
 *   • days_active — counted via "first_seen" timestamp
 *   • signals_received — incremented when copyAuto.dispatchSignal sends to user
 *   • copy_clicks — incremented when callback `copyauto:click:<sigid>` arrives
 *   • feedback_count — increments when /feedback completes
 *
 * Storage: JSON
 *   {
 *     "{userId}": {
 *       first_seen: 173..., last_seen: 173...,
 *       signals_received: 0, copy_clicks: 0, feedback_count: 0,
 *     }
 *   }
 */

const fs = require('fs');
const path = require('path');
const wt = require('./walletTracker');
const tzMgr = require('./timezoneManager');
const share = require('./share');

function _resolveDbPath() {
  return (
    process.env.STATS_DB_PATH ||
    path.join(
      process.env.RAILWAY_VOLUME_MOUNT_PATH || '/app/data',
      'user_stats.json'
    )
  );
}

const DB_PATH = _resolveDbPath();
let _store = null;

function _ensureDir() {
  try { fs.mkdirSync(path.dirname(DB_PATH), { recursive: true }); }
  catch (_) {}
}

function _load() {
  if (_store !== null) return _store;
  try {
    if (fs.existsSync(DB_PATH)) {
      const raw = JSON.parse(fs.readFileSync(DB_PATH, 'utf-8'));
      _store = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {};
    } else {
      _store = {};
    }
  } catch (e) {
    console.error('[stats] load failed:', e && e.message ? e.message : e);
    _store = {};
  }
  return _store;
}

function _save() {
  try {
    _ensureDir();
    const tmp = DB_PATH + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(_load(), null, 2));
    fs.renameSync(tmp, DB_PATH);
  } catch (e) {
    console.error('[stats] save failed:', e && e.message ? e.message : e);
  }
}

function _ensureRec(userId) {
  const s = _load();
  const k = String(userId);
  if (!s[k]) {
    s[k] = {
      first_seen: Date.now(),
      last_seen: Date.now(),
      signals_received: 0,
      copy_clicks: 0,
      feedback_count: 0,
    };
  }
  return s[k];
}

function touch(userId) {
  if (userId == null) return;
  const rec = _ensureRec(userId);
  rec.last_seen = Date.now();
  _save();
}

function incrementSignal(userId) {
  if (userId == null) return;
  const rec = _ensureRec(userId);
  rec.signals_received = (rec.signals_received || 0) + 1;
  rec.last_seen = Date.now();
  _save();
}

function incrementCopy(userId) {
  if (userId == null) return;
  const rec = _ensureRec(userId);
  rec.copy_clicks = (rec.copy_clicks || 0) + 1;
  rec.last_seen = Date.now();
  _save();
}

function incrementFeedback(userId) {
  if (userId == null) return;
  const rec = _ensureRec(userId);
  rec.feedback_count = (rec.feedback_count || 0) + 1;
  rec.last_seen = Date.now();
  _save();
}

function getStats(userId) {
  const rec = _ensureRec(userId);
  const days = Math.max(1, Math.floor((Date.now() - rec.first_seen) / 86400000));
  return {
    daysActive: days,
    signalsReceived: rec.signals_received || 0,
    copyClicks: rec.copy_clicks || 0,
    feedbackCount: rec.feedback_count || 0,
    firstSeen: rec.first_seen,
    lastSeen: rec.last_seen,
  };
}

function formatStats(userId) {
  const s = getStats(userId);
  const tz = tzMgr.getUserTz(userId);
  const wallets = wt.getUserWallets(userId);
  const maxSlots = share.getMaxSlots(userId);
  const refStats = share.getStats(userId);
  const lines = [
    '📊 *Your stats*',
    '',
    `📅 Bot usage: ${s.daysActive} day${s.daysActive === 1 ? '' : 's'}`,
    `📡 Tracked wallets: ${wallets.length}/${maxSlots}`,
    `📨 Signals received: ${s.signalsReceived}`,
    `🍐 Trades copied (clicks): ${s.copyClicks}`,
    `🌎 TZ: \`${tz}\``,
    `💬 Feedback sent: ${s.feedbackCount}`,
    `🎁 Referrals: ${refStats.count}${refStats.premium ? ' ✨ (Premium)' : ''}`,
  ];
  return lines.join('\n');
}

function _resetForTests(customPath) {
  _store = {};
  try {
    const p = customPath || DB_PATH;
    if (fs.existsSync(p)) fs.unlinkSync(p);
  } catch (_) {}
}

module.exports = {
  DB_PATH,
  touch,
  incrementSignal,
  incrementCopy,
  incrementFeedback,
  getStats,
  formatStats,
  _resetForTests,
};
