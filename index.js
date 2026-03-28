require('dotenv').config();

const HyperliquidApi = require('./src/hyperliquidApi');
const PositionMonitor = require('./src/monitor');
const createBot = require('./src/bot');
const { addWallet, loadWallets } = require('./src/store');

const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
const TELEGRAM_CHAT_ID = process.env.TELEGRAM_CHAT_ID;
const POLL_INTERVAL = parseInt(process.env.POLL_INTERVAL || '15', 10);
const HYPERLIQUID_API_URL = process.env.HYPERLIQUID_API_URL || 'https://api.hyperliquid.xyz';

if (!TELEGRAM_BOT_TOKEN || !TELEGRAM_CHAT_ID) {
  console.error('Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env');
  process.exit(1);
}

async function main() {
  console.log('Starting Pear Protocol Monitor Bot...');

  const hlApi = new HyperliquidApi(HYPERLIQUID_API_URL);

  // Create monitor with placeholder notify
  const monitor = new PositionMonitor(hlApi, async () => {});

  // Create Telegram bot
  const { bot, sendNotification } = createBot(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, hlApi, monitor);
  monitor.notify = sendNotification;

  // Pre-load the wallet from env if wallets list is empty
  const wallets = loadWallets();
  if (wallets.length === 0) {
    const defaultWallet = '0x171b7880939d76abbc6b6b2094f54e6636f829a7';
    console.log(`Adding default wallet: ${defaultWallet}`);
    addWallet(defaultWallet, 'Main Wallet');
  }

  // Start monitoring
  await monitor.start(POLL_INTERVAL);

  await sendNotification('🍐 *Pear Monitor is live!*\nWatching your positions on Pear / Hyperliquid.');

  console.log(`Bot running. Polling every ${POLL_INTERVAL}s. Wallets: ${loadWallets().length}`);
}

main().catch(err => {
  console.error('Fatal error:', err);
  process.exit(1);
});
