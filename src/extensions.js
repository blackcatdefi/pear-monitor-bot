'use strict';

/**
 * Round v2 — Extensions bootstrap.
 *
 * One entry point that wires together all R(v2) modules into the existing
 * bot/monitor pipeline without rewriting them. Called from index.js after
 * createBot() returns. Delivers:
 *
 *   1. Health/status HTTP server on HEALTH_PORT
 *   2. Heartbeat every HEARTBEAT_INTERVAL_HOURS
 *   3. Weekly summary scheduler (Sun 18:00 UTC default)
 *   4. /history /pnl /status /export /summary commands
 *   5. Open-alerts + compounding hooks via observe()
 *   6. Rate-limit wrapped notifier
 */

const healthServer = require('./healthServer');
const heartbeat = require('./heartbeat');
const weeklySummary = require('./weeklySummary');
const commands = require('./commands');
const rateLimiter = require('./rateLimiter');
const eventLog = require('./eventLog');
const openAlerts = require('./openAlerts');
const compoundingDetector = require('./compoundingDetector');
const pnlCrossValidation = require('./pnlCrossValidation');
const walletConfig = require('./walletConfig');
const closeClassifier = require('./closeClassifier');
const principalBridge = require('./principalBridge');
const { appendFooter } = require('./branding');

function _safeInt(v, d) {
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? n : d;
}

/**
 * Wraps the bot's sendNotification to enforce the rate limit + branding
 * footer. Called once during bootstrap; the wrapped fn replaces the raw
 * sendNotification on the monitor.
 */
function wrapNotifier(rawNotify) {
  return async function wrappedNotify(chatId, message, opts) {
    if (!rateLimiter.canSendAlert()) {
      // dropped — already logged inside canSendAlert()
      return;
    }
    return rawNotify(chatId, message, opts);
  };
}

/**
 * Hooks invoked by monitor.poll() each cycle. Plug-points are exposed on
 * the returned object so the caller can opt into them without restructuring
 * monitor.js.
 */
function buildHooks({ notify, primaryChatId }) {
  // Per-wallet snapshot of "what positions did we last see" for compound + open
  const lastSeenSnapshots = new Map();

  function _snapshotKey(chatId, wallet) {
    return `${chatId}:${(wallet || '').toLowerCase()}`;
  }

  async function onPollCycleStart() {
    healthServer.recordSuccessfulPoll();
  }

  async function onPollCycleError(err) {
    healthServer.recordError(err);
  }

  /**
   * Called by monitor after it has the current `allPositions` for a wallet.
   * Detects new openings + compound increases and emits alerts.
   */
  async function onWalletPolled({
    chatId,
    wallet,
    label,
    allPositions,
  }) {
    const key = _snapshotKey(chatId, wallet);
    const prev = lastSeenSnapshots.get(key) || [];

    // 1. open-alerts
    if (openAlerts.isEnabled()) {
      try {
        const newPositions = openAlerts.findNewPositions(
          allPositions,
          prev
        );
        if (newPositions.length > 0) {
          await openAlerts.emitAlerts({
            chatId,
            wallet,
            label,
            newPositions,
            notify: async (cId, msg) => {
              const isPrimary = walletConfig.isPrimaryWallet(wallet);
              await notify(cId, appendFooter(msg, isPrimary), {
                parse_mode: 'Markdown',
              });
              for (const p of newPositions) {
                const evt = {
                  type: 'OPEN',
                  chatId: String(chatId),
                  wallet,
                  coin: p.coin,
                  side: p.side || (p.size < 0 ? 'SHORT' : 'LONG'),
                  size: p.size,
                  entryPrice: p.entryPrice,
                  entryNotional: Math.abs(
                    (p.size || 0) * (p.entryPrice || 0)
                  ),
                  leverage: p.leverage || null,
                };
                // Bridge replaces direct eventLog write — it both
                // appends to JSONL AND fires the optional webhook.
                try { await principalBridge.publish(evt); }
                catch (_) { eventLog.recordEvent(evt); }
              }
            },
          });
        }
      } catch (e) {
        console.error(
          '[extensions] openAlerts hook failed:',
          e && e.message ? e.message : e
        );
        healthServer.recordError(e);
      }
    }

    // 2. compounding
    if (compoundingDetector.isEnabled()) {
      try {
        const result = compoundingDetector.checkForCompounding(
          chatId,
          wallet,
          allPositions
        );
        if (result.type === 'COMPOUND_DETECTED') {
          const isPrimary = walletConfig.isPrimaryWallet(wallet);
          const msg = appendFooter(
            compoundingDetector.formatCompoundAlert(label, result),
            isPrimary
          );
          await notify(chatId, msg, { parse_mode: 'Markdown' });
          const evt = {
            type: 'COMPOUND',
            chatId: String(chatId),
            wallet,
            prev_notional: result.prevNotional,
            current_notional: result.currentNotional,
            growth_pct: result.growth * 100,
          };
          try { await principalBridge.publish(evt); }
          catch (_) { eventLog.recordEvent(evt); }
        }
      } catch (e) {
        console.error(
          '[extensions] compounding hook failed:',
          e && e.message ? e.message : e
        );
        healthServer.recordError(e);
      }
    }

    lastSeenSnapshots.set(key, allPositions);
  }

  /**
   * Called by monitor when a coin closed (size→0). The monitor already
   * handles the alert via formatCloseAlert, but we record into eventLog
   * here so /history /pnl /export work.
   *
   * close: { coin, side, openedAt, exitPrice, size, pnl, fees, reason }
   */
  async function onClose({ chatId, wallet, label, close }) {
    try {
      // Optional cross-validation for big PnL
      let pnl = close.pnl;
      let pnlSource = 'bot';
      if (
        pnlCrossValidation.isEnabled() &&
        Number.isFinite(pnl) &&
        Math.abs(pnl) > 0
      ) {
        const notional = Math.abs(
          (close.size || 0) * (close.entryPrice || 0)
        );
        const result = await pnlCrossValidation.validatePnlBeforeAlert({
          wallet,
          coin: close.coin,
          calculatedPnl: pnl,
          notional,
        });
        pnl = result.pnl;
        pnlSource = result.source;
        if (result.flagged && result.note) {
          await notify(
            chatId,
            `⚠️ *PnL CROSS-VALIDATE* — ${close.coin}\n${result.note}`,
            { parse_mode: 'Markdown' }
          );
        }
      }

      const evt = {
        type: 'FULL_CLOSE',
        chatId: String(chatId),
        wallet,
        coin: close.coin,
        side: close.side,
        entryPrice: close.entryPrice,
        exitPrice: close.exitPrice,
        size: close.size,
        pnl,
        pnl_source: pnlSource,
        fees: close.fees,
        reason: close.reason || 'UNKNOWN',
        openedAt: close.openedAt || null,
        entryNotional: Math.abs(
          (close.size || 0) * (close.entryPrice || 0)
        ),
      };
      try { await principalBridge.publish(evt); }
      catch (_) { eventLog.recordEvent(evt); }
      // Compound snapshot resets when basket cleared
      compoundingDetector._resetForTests; // no-op; we just reset on next empty poll
    } catch (e) {
      console.error(
        '[extensions] onClose hook failed:',
        e && e.message ? e.message : e
      );
      healthServer.recordError(e);
    }
  }

  return {
    onPollCycleStart,
    onPollCycleError,
    onWalletPolled,
    onClose,
  };
}

/**
 * Wraps monitor.checkWallet so we can fire onWalletPolled / onClose hooks
 * without touching monitor.js. Captures the positions state pre-call vs
 * post-call to detect closes that the monitor processed.
 */
function patchMonitor(monitor, hooks) {
  if (!monitor || typeof monitor.checkWallet !== 'function') return;
  if (monitor.__rv2Patched) return;
  monitor.__rv2Patched = true;

  const originalCheckWallet = monitor.checkWallet.bind(monitor);
  const originalPoll = monitor.poll
    ? monitor.poll.bind(monitor)
    : null;

  monitor.checkWallet = async function patchedCheckWallet(
    chatId,
    wallet,
    state,
    silent
  ) {
    const addr = wallet.address;
    const label = wallet.label || addr;
    // Snapshot pre
    const pre =
      state && state[addr] && state[addr].positions
        ? Object.values(state[addr].positions).map((p) => ({ ...p }))
        : [];

    let result;
    let err = null;
    try {
      result = await originalCheckWallet(chatId, wallet, state, silent);
    } catch (e) {
      err = e;
    }

    try {
      const post =
        state && state[addr] && state[addr].positions
          ? Object.values(state[addr].positions).map((p) => ({ ...p }))
          : [];
      // Detect closes: coin in pre but not in post
      const postCoins = new Set(post.map((p) => p.coin));
      for (const oldPos of pre) {
        if (!postCoins.has(oldPos.coin)) {
          // closed
          await hooks.onClose({
            chatId,
            wallet: addr,
            label,
            close: {
              coin: oldPos.coin,
              side: oldPos.side,
              entryPrice: oldPos.entryPrice,
              exitPrice: null,
              size: oldPos.size,
              openedAt: oldPos.openedAt,
              pnl: 0, // monitor.js owns the PnL — best-effort log
              fees: 0,
              reason: 'AUTO',
            },
          });
        }
      }
      await hooks.onWalletPolled({
        chatId,
        wallet: addr,
        label,
        allPositions: post,
      });
    } catch (e) {
      console.error(
        '[extensions] post-checkWallet hook failed:',
        e && e.message ? e.message : e
      );
      healthServer.recordError(e);
    }

    if (err) {
      await hooks.onPollCycleError(err);
      throw err;
    }
    return result;
  };

  if (originalPoll) {
    monitor.poll = async function patchedPoll(...args) {
      try {
        await hooks.onPollCycleStart();
      } catch (e) {
        console.error(
          '[extensions] onPollCycleStart hook failed:',
          e && e.message ? e.message : e
        );
      }
      return originalPoll(...args);
    };
  }
}

/**
 * Public bootstrap. Call once from index.js.
 *
 *   bootstrap({
 *     bot,                     // node-telegram-bot-api instance
 *     monitor,                 // PositionMonitor instance
 *     sendNotification,        // raw fn (chatId, msg, opts) => Promise
 *     primaryChatId,           // BCD's chat (for heartbeat + weekly summary)
 *   })
 *
 * Returns: { hooks, healthServer, rateLimitStats, stop }
 */
function bootstrap({
  bot,
  monitor,
  sendNotification,
  primaryChatId,
}) {
  const port = _safeInt(process.env.HEALTH_PORT, 8080);
  console.log('[extensions] bootstrapping R(v2)...');

  // 1. Health server
  const httpServer = healthServer.start(port);

  // 2. Wrap notifier with rate limit
  const wrappedNotify = wrapNotifier(sendNotification);

  // 3. Build hooks (open-alerts, compound, eventLog persist)
  const hooks = buildHooks({
    notify: wrappedNotify,
    primaryChatId,
  });

  // 4. Heartbeat scheduler — only if BCD chat configured
  let heartbeatTimer = null;
  if (primaryChatId) {
    heartbeatTimer = heartbeat.startSchedule(wrappedNotify, primaryChatId);
  } else {
    console.log('[extensions] BCD_TELEGRAM_CHAT_ID not set — heartbeat disabled');
  }

  // 5. Weekly summary scheduler
  let weeklyTimer = null;
  if (primaryChatId) {
    weeklyTimer = weeklySummary.startSchedule(wrappedNotify, primaryChatId);
  }

  // 6. Telegram commands (/history /pnl /status /export /summary)
  if (commands.isEnabled() && bot) {
    commands.attachCommands(bot);
  }

  // 7. Monkey-patch monitor for lifecycle hooks
  patchMonitor(monitor, hooks);

  console.log('[extensions] R(v2) bootstrap complete.');

  return {
    hooks,
    httpServer,
    notify: wrappedNotify,
    rateLimitStats: () => rateLimiter.getStats(),
    stop() {
      try {
        if (httpServer && httpServer.close) httpServer.close();
      } catch (_) {}
      try {
        if (heartbeatTimer) clearInterval(heartbeatTimer);
      } catch (_) {}
      try {
        if (weeklyTimer) clearInterval(weeklyTimer);
      } catch (_) {}
    },
  };
}

module.exports = {
  bootstrap,
  wrapNotifier,
  buildHooks,
  patchMonitor,
};
