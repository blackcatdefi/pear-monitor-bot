'use strict';

/**
 * R-AUTOCOPY — Leaderboard of most-tracked wallets.
 *
 * Aggregates walletTracker.getAllUniqueAddresses() into a ranked list by
 * follower count, applying a min-trackers filter (avoid 1-tracker wallets
 * cluttering the list). Returns the top N (default 10).
 *
 * Anonymizes addresses to `0xabc...123` — never exposes private labels
 * (those belong to the user that set them).
 */

const wt = require('./walletTracker');

const MIN_TRACKERS = parseInt(process.env.LEADERBOARD_MIN_TRACKERS || '3', 10);
const TOP_N = parseInt(process.env.LEADERBOARD_TOP_N || '10', 10);

function _shortAddr(a) {
  if (!a) return '?';
  const s = String(a);
  if (s.length < 10) return s;
  return `${s.slice(0, 6)}...${s.slice(-4)}`;
}

/**
 * Returns array of { address, addressShort, count } sorted by count desc,
 * filtered by MIN_TRACKERS, capped at TOP_N.
 */
function getLeaderboard(opts) {
  const o = opts || {};
  const minT = Number.isFinite(o.minTrackers) ? o.minTrackers : MIN_TRACKERS;
  const topN = Number.isFinite(o.topN) ? o.topN : TOP_N;
  const all = wt.getAllUniqueAddresses();
  const ranked = all
    .map((a) => ({
      address: a.address,
      addressShort: _shortAddr(a.address),
      count: (a.subscribers || []).length,
    }))
    .filter((a) => a.count >= minT)
    .sort((a, b) => b.count - a.count || a.address.localeCompare(b.address))
    .slice(0, topN);
  return ranked;
}

function formatLeaderboard(leaderboard) {
  if (!leaderboard || leaderboard.length === 0) {
    return [
      '🏆 *Top tracked wallets*',
      '',
      '_Not enough data yet to build a ranking._',
      `_(need at least ${MIN_TRACKERS} users tracking the same wallet)_`,
      '',
      'Track wallets with /track so they show up here.',
    ].join('\n');
  }
  const lines = [
    '🏆 *Top tracked wallets (anonymized)*',
    '',
  ];
  leaderboard.forEach((row, idx) => {
    const medal = idx === 0 ? '🥇' : idx === 1 ? '🥈' : idx === 2 ? '🥉' : `${idx + 1}.`;
    const word = row.count === 1 ? 'user follows' : 'users follow';
    lines.push(`${medal} \`${row.addressShort}\` — ${row.count} ${word}`);
  });
  lines.push('');
  lines.push('_Tap a wallet to track it:_');
  return lines.join('\n');
}

function buildKeyboard(leaderboard) {
  if (!leaderboard || leaderboard.length === 0) return null;
  const rows = [];
  leaderboard.slice(0, 5).forEach((row, idx) => {
    rows.push([
      {
        text: `➕ Track top ${idx + 1}`,
        callback_data: `lb:track:${row.address.slice(2, 10)}`,
      },
    ]);
  });
  return { inline_keyboard: rows };
}

/**
 * Resolve the full address from the truncated suffix used in the callback
 * (we use 8 hex chars after `0x` to keep callback_data short). If multiple
 * addresses share the prefix, returns the first match.
 */
function resolveAddressByPrefix(prefix8) {
  if (!prefix8 || prefix8.length < 4) return null;
  const all = wt.getAllUniqueAddresses();
  const want = String(prefix8).toLowerCase();
  for (const a of all) {
    const lc = String(a.address || '').toLowerCase();
    if (lc.slice(2, 10) === want) return a.address;
  }
  return null;
}

module.exports = {
  MIN_TRACKERS,
  TOP_N,
  getLeaderboard,
  formatLeaderboard,
  buildKeyboard,
  resolveAddressByPrefix,
  _shortAddr,
};
