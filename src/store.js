const fs = require('fs');
const path = require('path');

const DATA_DIR = path.join(__dirname, '..', 'data');
const WALLETS_FILE = path.join(DATA_DIR, 'wallets.json');
const STATE_FILE = path.join(DATA_DIR, 'state.json');

function ensureDataDir() {
  if (!fs.existsSync(DATA_DIR)) {
    fs.mkdirSync(DATA_DIR, { recursive: true });
  }
}

function loadWallets() {
  ensureDataDir();
  if (!fs.existsSync(WALLETS_FILE)) return [];
  return JSON.parse(fs.readFileSync(WALLETS_FILE, 'utf8'));
}

function saveWallets(wallets) {
  ensureDataDir();
  fs.writeFileSync(WALLETS_FILE, JSON.stringify(wallets, null, 2));
}

function addWallet(address, label) {
  const wallets = loadWallets();
  const existing = wallets.find(w => w.address.toLowerCase() === address.toLowerCase());
  if (existing) {
    existing.label = label || existing.label;
  } else {
    wallets.push({ address: address.toLowerCase(), label: label || shortenAddress(address) });
  }
  saveWallets(wallets);
  return wallets;
}

function removeWallet(address) {
  let wallets = loadWallets();
  wallets = wallets.filter(w => w.address.toLowerCase() !== address.toLowerCase());
  saveWallets(wallets);
  return wallets;
}

function shortenAddress(addr) {
  return `${addr.slice(0, 6)}...${addr.slice(-4)}`;
}

function loadState() {
  ensureDataDir();
  if (!fs.existsSync(STATE_FILE)) return {};
  return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8'));
}

function saveState(state) {
  ensureDataDir();
  fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 2));
}

module.exports = { loadWallets, saveWallets, addWallet, removeWallet, loadState, saveState, shortenAddress };
