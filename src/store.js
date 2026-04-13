const fs = require('fs');
const path = require('path');

// Use persistent volume on Railway, fallback to local data/
const VOLUME = process.env.RAILWAY_VOLUME_MOUNT_PATH;
const DATA_DIR = VOLUME ? path.join(VOLUME, 'data') : path.join(__dirname, '..', 'data');

function ensureDataDir() {
  if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
}

function userFile(chatId) {
  return path.join(DATA_DIR, `user_${chatId}.json`);
}

function stateFile(chatId) {
  return path.join(DATA_DIR, `state_${chatId}.json`);
}

function loadUser(chatId) {
  ensureDataDir();
  const f = userFile(chatId);
  if (!fs.existsSync(f)) return { wallets: [] };
  return JSON.parse(fs.readFileSync(f, 'utf8'));
}

function saveUser(chatId, data) {
  ensureDataDir();
  fs.writeFileSync(userFile(chatId), JSON.stringify(data, null, 2));
}

function getWallets(chatId) {
  return loadUser(chatId).wallets || [];
}

function addWallet(chatId, address, label) {
  const user = loadUser(chatId);
  if (!user.wallets) user.wallets = [];
  const existing = user.wallets.find(w => w.address.toLowerCase() === address.toLowerCase());
  if (existing) {
    existing.label = label || existing.label;
  } else {
    user.wallets.push({ address: address.toLowerCase(), label: label || shortenAddress(address) });
  }
  saveUser(chatId, user);
  return user.wallets;
}

function removeWallet(chatId, address) {
  const user = loadUser(chatId);
  user.wallets = (user.wallets || []).filter(w => w.address.toLowerCase() !== address.toLowerCase());
  saveUser(chatId, user);
  return user.wallets;
}

// --- HyperLend borrow wallets (separate from Hyperliquid position wallets) ---

function getBorrowWallets(chatId) {
  return loadUser(chatId).borrowWallets || [];
}

function addBorrowWallet(chatId, address, label) {
  const user = loadUser(chatId);
  if (!user.borrowWallets) user.borrowWallets = [];
  const existing = user.borrowWallets.find(w => w.address.toLowerCase() === address.toLowerCase());
  if (existing) {
    existing.label = label || existing.label;
  } else {
    user.borrowWallets.push({ address: address.toLowerCase(), label: label || shortenAddress(address) });
  }
  saveUser(chatId, user);
  return user.borrowWallets;
}

function removeBorrowWallet(chatId, address) {
  const user = loadUser(chatId);
  user.borrowWallets = (user.borrowWallets || []).filter(w => w.address.toLowerCase() !== address.toLowerCase());
  saveUser(chatId, user);
  return user.borrowWallets;
}

function shortenAddress(addr) {
  return `${addr.slice(0, 6)}...${addr.slice(-4)}`;
}

function loadState(chatId) {
  ensureDataDir();
  const f = stateFile(chatId);
  if (!fs.existsSync(f)) return {};
  return JSON.parse(fs.readFileSync(f, 'utf8'));
}

function saveState(chatId, state) {
  ensureDataDir();
  fs.writeFileSync(stateFile(chatId), JSON.stringify(state, null, 2));
}

// Get all active chat IDs (users who have wallets or borrow wallets)
function getAllChatIds() {
  ensureDataDir();
  const files = fs.readdirSync(DATA_DIR).filter(f => f.startsWith('user_') && f.endsWith('.json'));
  const chatIds = [];
  for (const f of files) {
    const chatId = f.replace('user_', '').replace('.json', '');
    const user = loadUser(chatId);
    const hasWallets = user.wallets && user.wallets.length > 0;
    const hasBorrow = user.borrowWallets && user.borrowWallets.length > 0;
    if (hasWallets || hasBorrow) {
      chatIds.push(chatId);
    }
  }
  return chatIds;
}

module.exports = {
  getWallets, addWallet, removeWallet,
  getBorrowWallets, addBorrowWallet, removeBorrowWallet,
  loadState, saveState, shortenAddress, getAllChatIds,
};
