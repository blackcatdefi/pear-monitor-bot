require('dotenv').config();

// R-PUBLIC-START-FIX: Node 20 exits on unhandledRejection by default.
// Guard here so a transient Telegram API error during startup (e.g. a
// setMyCommands call racing against Railway's deployment window) does NOT
// kill the process before /start and other handlers are registered.
process.on('unhandledRejection', (reason) => {
  console.error('[index] unhandledRejection (non-fatal):', reason && reason.message ? reason.message : reason);
});
process.on('uncaughtException', (err) => {
  console.error('[index] uncaughtException:', err && err.message ? err.message : err);
});

const HyperliquidApi = require('./src/hyperliquidApi');
const HyperLendApi = require('./src/hyperLendApi');
const PositionMonitor = require('./src/monitor');
const createBot = require('./src/bot');
const extensions = require('./src/extensions');

const TELEGRAM_BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN;
const POLL_INTERVAL = parseInt(process.env.POLL_INTERVAL || '30', 10);
const HYPERLIQUID_API_URL = process.env.HYPERLIQUID_API_URL || 'https://api.hyperliquid.xyz';
const HYPEREVM_RPC_URL = process.env.HYPEREVM_RPC_URL || 'https://rpc.hyperliquid.xyz/evm';
const HYPERLEND_POOL_ADDRESS = process.env.HYPERLEND_POOL_ADDRESS || '0x00A89d7a5A02160f20150EbEA7a2b5E4879A1A8b';

if (!TELEGRAM_BOT_TOKEN) {
  console.error('Missing TELEGRAM_BOT_TOKEN in .env');
  process.exit(1);
}

async function main() {
  console.log('Starting Pear Protocol Monitor Bot...');

  const hlApi = new HyperliquidApi(HYPERLIQUID_API_URL);
  const hlendApi = new HyperLendApi({ rpcUrl: HYPEREVM_RPC_URL, poolAddress: HYPERLEND_POOL_ADDRESS });
  const monitor = new PositionMonitor(hlApi, async () => {}, hlendApi);
  const { bot, sendNotification } = createBot(TELEGRAM_BOT_TOKEN, hlApi, monitor, hlendApi);

  monitor.notify = sendNotification;

  // Round v2 — bootstrap extensions BEFORE start() so monitor patches apply
  // to the very first poll cycle. Health server, heartbeat, weekly summary,
  // /history /pnl /status /export /summary commands all attach here.
  const primaryChatId =
    process.env.BCD_TELEGRAM_CHAT_ID || process.env.WEEKLY_SUMMARY_CHAT_ID || null;
  const rv2 = extensions.bootstrap({
    bot,
    monitor,
    sendNotification,
    primaryChatId,
  });
  monitor.notify = rv2.notify;

  await monitor.start(POLL_INTERVAL);

  console.log(`Bot running. Polling every ${POLL_INTERVAL}s. Public mode - any user can add wallets.`);
  console.log(`HyperLend enabled via ${HYPEREVM_RPC_URL} (Pool ${HYPERLEND_POOL_ADDRESS})`);
  console.log(`R(v2) extensions active. Health on :${process.env.HEALTH_PORT || 8080}`);

  // R-PUBLIC-START-FIX: handle SIGTERM (Railway sends this when stopping the
  // old container during a deploy). Without this, the old instance keeps its
  // Telegram polling connection open for up to 30 s, causing 409 Conflict for
  // the new instance — which means the new bot never receives any updates
  // (including /start) until the OS finally kills the old process.
  function _shutdown(signal) {
    console.log(`[index] ${signal} received — stopping polling gracefully`);
    try { bot.stopPolling(); } catch (_) {}
    try { rv2.stop(); } catch (_) {}
    setTimeout(() => process.exit(0), 1500);
  }
  process.once('SIGTERM', () => _shutdown('SIGTERM'));
  process.once('SIGINT',  () => _shutdown('SIGINT'));
}

main().catch(err => {
  console.error('Fatal error:', err);
  process.exit(1);
});
