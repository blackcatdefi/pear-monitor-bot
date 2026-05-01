'use strict';

/**
 * R-PUBLIC — Conversational state machine for /track flows.
 *
 * In-memory only — conversational input is short-lived; if the bot restarts
 * mid-flow the user just sees a friendly "/track again" prompt.
 *
 * States:
 *   IDLE                       (default, no message expected)
 *   AWAITING_WALLET_ADDRESS    (next plain-text msg = address to track)
 *   AWAITING_WALLET_LABEL      (next plain-text msg = label, /skip allowed)
 *   AWAITING_REMOVE_ADDRESS    (next plain-text msg = address to remove)
 *
 * Timeout: configurable via USER_STATE_TIMEOUT_MIN (default 5).
 */

// R-AUTOCOPY: exposes additional conversational states for /portfolio and
// /feedback. NOT frozen — the new commands modules monkey-patch their state
// names at module-load (defensive: keeps R-PUBLIC behavior intact even if a
// commands module isn't loaded).
const STATES = {
  IDLE: 'IDLE',
  AWAITING_WALLET_ADDRESS: 'AWAITING_WALLET_ADDRESS',
  AWAITING_WALLET_LABEL: 'AWAITING_WALLET_LABEL',
  AWAITING_REMOVE_ADDRESS: 'AWAITING_REMOVE_ADDRESS',
  AWAITING_PORTFOLIO_ADDRESS: 'AWAITING_PORTFOLIO_ADDRESS',
  AWAITING_FEEDBACK: 'AWAITING_FEEDBACK',
};

function _timeoutMs() {
  const m = parseInt(process.env.USER_STATE_TIMEOUT_MIN || '5', 10);
  return (Number.isFinite(m) && m > 0 ? m : 5) * 60 * 1000;
}

const _store = new Map(); // key: chatId -> { state, payload, updatedAt }

function _pruneIfExpired(rec) {
  if (!rec) return null;
  const ttl = _timeoutMs();
  if (Date.now() - (rec.updatedAt || 0) > ttl) return null;
  return rec;
}

function getState(chatId) {
  const rec = _pruneIfExpired(_store.get(String(chatId)));
  if (!rec) {
    _store.delete(String(chatId));
    return { state: STATES.IDLE, payload: {}, updatedAt: 0 };
  }
  return rec;
}

function setState(chatId, state, payload) {
  if (!Object.values(STATES).includes(state)) {
    throw new Error(`Invalid state: ${state}`);
  }
  _store.set(String(chatId), {
    state,
    payload: payload || {},
    updatedAt: Date.now(),
  });
}

function reset(chatId) {
  _store.delete(String(chatId));
}

function isExpired(chatId) {
  const raw = _store.get(String(chatId));
  if (!raw) return false; // no record at all
  const ttl = _timeoutMs();
  return Date.now() - (raw.updatedAt || 0) > ttl;
}

function _resetForTests() {
  _store.clear();
}

module.exports = {
  STATES,
  getState,
  setState,
  reset,
  isExpired,
  _resetForTests,
};
