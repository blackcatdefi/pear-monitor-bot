'use strict';

/**
 * R-PUBLIC — Wallet tracker (per-user external-wallet subscriptions).
 *
 * Public users add wallets via /track. Their subscriptions live in a JSON
 * store on disk (Railway Volume /app/data, same mount used by basketDedup).
 * Polling de-duplicates addresses (multiple users tracking the same whale =
 * one HL fetch + fan-out to N userIds).
 *
 * Storage shape:
 *   {
 *     "byUser": {
 *       "{userId}": [
 *         { "address": "0x...", "label": "Whale 1", "added_at": 173... },
 *         ...
 *       ]
 *     },
 *     "lastSnapshots": {
 *       "{address_lc}": [ { coin, side, size, entryPx, ... }, ... ]
 *     }
 *   }
 */

const fs = require('fs');
const path = require('path');

const ADDRESS_REGEX = /^0x[a-fA-F0-9]{40}$/;
const MAX_WALLETS_PER_USER = parseInt(
  process.env.TRACK_MAX_WALLETS_PER_USER || '10', 10
);

function _resolveDbPath() {
  return (
    process.env.TRACK_DB_PATH ||
    path.join(
      process.env.RAILWAY_VOLUME_MOUNT_PATH || '/app/data',
      'tracked_wallets.json'
    )
  );
}

const DB_PATH = _resolveDbPath();
let _store = null;

function _ensureDir() {
  try {
    fs.mkdirSync(path.dirname(DB_PATH), { recursive: true });
  } catch (_) {}
}

function _emptyStore() {
  return { byUser: {}, lastSnapshots: {} };
}

function _load() {
  if (_store !== null) return _store;
  try {
    if (fs.existsSync(DB_PATH)) {
      const raw = JSON.parse(fs.readFileSync(DB_PATH, 'utf-8'));
      if (raw && typeof raw === 'object' && raw.byUser) {
        _store = raw;
      } else {
        _store = _emptyStore();
      }
    } else {
      _store = _emptyStore();
    }
  } catch (e) {
    console.error(
      '[walletTracker] load failed, starting empty:',
      e && e.message ? e.message : e
    );
    _store = _emptyStore();
  }
  if (!_store.byUser) _store.byUser = {};
  if (!_store.lastSnapshots) _store.lastSnapshots = {};
  return _store;
}

function _save() {
  try {
    _ensureDir();
    const tmp = DB_PATH + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(_load(), null, 2));
    fs.renameSync(tmp, DB_PATH);
  } catch (e) {
    console.error(
      '[walletTracker] save failed:',
      e && e.message ? e.message : e
    );
  }
}

function isValidAddress(addr) {
  return typeof addr === 'string' && ADDRESS_REGEX.test(addr);
}

function getUserWallets(userId) {
  const s = _load();
  const list = s.byUser[String(userId)] || [];
  return list.map((w) => ({ ...w }));
}

function hasWallet(userId, address) {
  const lc = String(address || '').toLowerCase();
  return getUserWallets(userId).some(
    (w) => String(w.address).toLowerCase() === lc
  );
}

function addWallet(userId, address, label) {
  if (userId == null) throw new Error('userId requerido');
  if (!isValidAddress(address)) {
    throw new Error('Dirección inválida — debe ser 0x + 40 caracteres hex');
  }
  const s = _load();
  const key = String(userId);
  if (!s.byUser[key]) s.byUser[key] = [];
  if (s.byUser[key].length >= MAX_WALLETS_PER_USER) {
    throw new Error(
      `Llegaste al máximo de ${MAX_WALLETS_PER_USER} wallets — eliminá alguna con /track antes de agregar otra.`
    );
  }
  const lc = address.toLowerCase();
  if (s.byUser[key].some((w) => String(w.address).toLowerCase() === lc)) {
    throw new Error('Ya tenés esa wallet trackeada.');
  }
  s.byUser[key].push({
    address,
    label: label && String(label).trim() ? String(label).trim().slice(0, 64) : null,
    added_at: Date.now(),
  });
  _save();
}

function removeWallet(userId, address) {
  const s = _load();
  const key = String(userId);
  const list = s.byUser[key] || [];
  const lc = String(address || '').toLowerCase();
  const next = list.filter((w) => String(w.address).toLowerCase() !== lc);
  s.byUser[key] = next;
  _save();
  return list.length - next.length;
}

function getAllUniqueAddresses() {
  const s = _load();
  const out = new Map(); // lc(addr) -> { address, subscribers: [{userId, label}] }
  for (const userId of Object.keys(s.byUser)) {
    for (const w of s.byUser[userId] || []) {
      const lc = String(w.address || '').toLowerCase();
      if (!lc) continue;
      if (!out.has(lc)) {
        out.set(lc, { address: w.address, subscribers: [] });
      }
      out.get(lc).subscribers.push({ userId, label: w.label || null });
    }
  }
  return Array.from(out.values());
}

function getSubscribersForAddress(address) {
  const lc = String(address || '').toLowerCase();
  const all = getAllUniqueAddresses();
  const found = all.find(
    (a) => String(a.address).toLowerCase() === lc
  );
  return found ? found.subscribers.slice() : [];
}

function getLastSnapshot(address) {
  const s = _load();
  return s.lastSnapshots[String(address || '').toLowerCase()] || null;
}

function setLastSnapshot(address, positions) {
  const s = _load();
  s.lastSnapshots[String(address || '').toLowerCase()] = Array.isArray(positions)
    ? positions
    : [];
  _save();
}

function _resetForTests(customPath) {
  _store = _emptyStore();
  try {
    const p = customPath || DB_PATH;
    if (fs.existsSync(p)) fs.unlinkSync(p);
  } catch (_) {}
}

module.exports = {
  ADDRESS_REGEX,
  MAX_WALLETS_PER_USER,
  DB_PATH,
  isValidAddress,
  getUserWallets,
  hasWallet,
  addWallet,
  removeWallet,
  getAllUniqueAddresses,
  getSubscribersForAddress,
  getLastSnapshot,
  setLastSnapshot,
  _resetForTests,
};
