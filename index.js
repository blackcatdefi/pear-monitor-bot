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

const axios = require('axios');
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

  // R-PUBLIC-START-FIX-V2: register SIGTERM/SIGINT handlers BEFORE the long
  // `await monitor.start()` first-poll. Without this, if Railway sends SIGTERM
  // to the old container while the new container is mid-startup-poll (which
  // can take seconds per wallet), the old instance exits without calling
  // stopPolling() and its Telegram getUpdates connection stays open, causing
  // 409 Conflict for the new instance.
  function _shutdown(signal) {
    console.log(`[index] ${signal} received — stopping polling gracefully`);
    try { bot.stopPolling(); } catch (_) {}
    try { rv2.stop(); } catch (_) {}
    setTimeout(() => process.exit(0), 1500);
  }
  process.once('SIGTERM', () => _shutdown('SIGTERM'));
  process.once('SIGINT',  () => _shutdown('SIGINT'));

  // R-PUBLIC-START-FIX-V3: use axios directly for all startup Telegram API
  // calls so Railway logs show the exact result of each step and we can
  // diagnose 401 (revoked token), active webhook, or 409 loops.
  //
  // V2 used bot.deleteWebhook() which silently no-ops if the underlying
  // request fails AND doesn't log the API response body. That made it
  // impossible to tell from logs whether the webhook was actually cleared.
  //
  // V3: explicit axios calls with full response logging. Each step is
  // non-fatal so the bot starts even if one call has a transient error.
  const _TG = `https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}`;

  // Step 1 — verify token is alive
  try {
    const meRes = await axios.get(`${_TG}/getMe`);
    const me = meRes.data && meRes.data.result;
    console.log(`[index] getMe OK — @${me && me.username} id=${me && me.id}`);
  } catch (err) {
    const status = err.response && err.response.status;
    const body   = err.response && err.response.data;
    console.error(`[index] getMe FAILED (status=${status}):`, JSON.stringify(body || err.message));
    if (status === 401) {
      console.error('[index] FATAL: TELEGRAM_BOT_TOKEN is revoked or invalid — exiting');
      process.exit(1);
    }
  }

  // Step 2 — read current webhook state before touching it
  let _webhookBefore = '?';
  try {
    const wiRes = await axios.get(`${_TG}/getWebhookInfo`);
    const wi = wiRes.data && wiRes.data.result;
    _webhookBefore = wi && wi.url ? wi.url : '(empty)';
    console.log(`[index] webhookInfo BEFORE: url="${_webhookBefore}" pending=${wi && wi.pending_update_count}`);
    if (wi && wi.last_error_message) {
      console.warn(`[index] webhookInfo last_error: ${wi.last_error_message}`);
    }
  } catch (err) {
    console.error('[index] getWebhookInfo(before) failed:', err && err.message ? err.message : err);
  }

  // Step 3 — delete webhook (idempotent, safe to call when no webhook set)
  try {
    const delRes = await axios.post(`${_TG}/deleteWebhook`, { drop_pending_updates: false });
    console.log('[index] deleteWebhook result:', JSON.stringify(delRes.data));
  } catch (err) {
    const body = err.response && err.response.data;
    console.error('[index] deleteWebhook FAILED:', JSON.stringify(body || err.message));
  }

  // Step 4 — confirm webhook is gone
  try {
    const wiRes2 = await axios.get(`${_TG}/getWebhookInfo`);
    const wi2 = wiRes2.data && wiRes2.data.result;
    const afterUrl = wi2 && wi2.url ? wi2.url : '(empty)';
    if (afterUrl && afterUrl !== '(empty)') {
      console.error(`[index] ERROR: webhook STILL ACTIVE after delete → "${afterUrl}" — polling will get 409`);
    } else {
      console.log('[index] webhookInfo AFTER: url="" — clear to poll');
    }
  } catch (err) {
    console.error('[index] getWebhookInfo(after) failed:', err && err.message ? err.message : err);
  }

  // Start polling NOW — webhook is confirmed absent.
  bot.startPolling();
  console.log('[index] polling started');

  // R-PUBLIC-START-NUCLEAR — record polling start in healthServer so /health
  // exposes telegram.polling_started_at and the lifetime counter starts at 0.
  // Wire a global bot.on('message') counter here so EVERY consumed update
  // (not just /start) increments lifetime — proves the poll loop is alive
  // even when no command-specific handler is attached yet.
  try {
    const _hs = require('./src/healthServer');
    _hs.recordPollingStarted();
    bot.on('message', (msg) => {
      try { _hs.recordTelegramUpdate(msg); }
      catch (_) {}
    });
  } catch (e) {
    console.error(
      '[index] healthServer instrumentation failed (non-fatal):',
      e && e.message ? e.message : e
    );
  }

  await monitor.start(POLL_INTERVAL);

  console.log(`Bot running. Polling every ${POLL_INTERVAL}s. Public mode - any user can add wallets.`);
  console.log(`HyperLend enabled via ${HYPEREVM_RPC_URL} (Pool ${HYPERLEND_POOL_ADDRESS})`);
  console.log(`R(v2) extensions active. Health on :${process.env.HEALTH_PORT || 8080}`);
}

main().catch(err => {
  console.error('Fatal error:', err);
  process.exit(1);
});
