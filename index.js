require('dotenv').config();

const HyperliquidApi = require('./src/hyperliquidApi');
const PositionMonitor = require('./src/monitor');
const createBot = require('./src/bot');

const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
const POLL_INTERVAL = parseInt(process.env.POLL_INTERVAL || '30', 10);
const HYPERLIQUID_API_URL = process.env.HYPERLIQUID_API_URL || 'https://api.hyperliquid.xyz';

if (!TELEGRAM_BOT_TOKEN) {
  console.error('Missing TELEGRAM_BOT_TOKEN in .env');
  process.exit(1);
}

async function main() {
  console.log('Starting Pear Protocol Monitor Bot...');

  const hlApi = new HyperliquidApi(HYPERLIQUID_API_URL);
  const monitor = new PositionMonitor(hlApi, async () => {});
  const { bot, sendNotification } = createBot(TELEGRAM_BOT_TOKEN, hlApi, monitor);

  monitor.notify = sendNotification;

  await monitor.start(POLL_INTERVAL);

  console.log(`Bot running. Polling every ${POLL_INTERVAL}s. Public mode - any user can add wallets.`);
}

main().catch(err => {
  console.error('Fatal error:', err);
  process.exit(1);
});
