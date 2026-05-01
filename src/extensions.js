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
// R(v3) — TWAP-aware compounding gate + external wallet tracker + timestamp
const compoundingGate = require('./compoundingGate');
const externalWalletTracker = require('./externalWalletTracker');
const { isTWAPActive } = require('./twapDetector');
const { withTimestamp } = require('./timestampHelper');
// R(v4) — Basket dedup. Gates BASKET_OPEN dispatch on a SHA-256 hash of
// (wallet + sorted positions) that survives bot restarts. Root cause of
// the apr-30 duplicate "NUEVA BASKET ABIERTA" was lastSeenSnapshots being
// in-memory only — every redeploy re-classified active positions as new.
const basketDedup = require('./basketDedup');
// R-PUBLIC — public wallet tracker UI + scheduler + per-user timezone.
const commandsTrack = require('./commandsTrack');
const commandsTimezone = require('./commandsTimezone');
const walletTrackerScheduler = require('./walletTrackerScheduler');
// R-START — /start onboarding (first-time vs recurring + inline keyboard).
const commandsStart = require('./commandsStart');
// R-AUTOCOPY — auto copy-trading + best-bot features.
const signalsChannel = require('./signalsChannel');
const copyAuto = require('./copyAuto');
const dailyDigest = require('./dailyDigest');
const commandsSignals = require('./commandsSignals');
const commandsCopyAuto = require('./commandsCopyAuto');
const commandsCapital = require('./commandsCapital');
const commandsPortfolio = require('./commandsPortfolio');
const commandsLeaderboard = require('./commandsLeaderboard');
const commandsAlertsConfig = require('./commandsAlertsConfig');
const commandsStats = require('./commandsStats');
const commandsShare = require('./commandsShare');
const commandsLearn = require('./commandsLearn');
const commandsFeedback = require('./commandsFeedback');
const commandsHelp = require('./commandsHelp');

function _safeInt(v, d) {
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? n : d;
}

/**
 * Wraps the bot's sendNotification to enforce the rate limit + branding
 * footer + R(v3) timestamp. Called once during bootstrap; the wrapped fn
 * replaces the raw sendNotification on the monitor.
 *
 * Timestamp is added to STRING bodies only and is idempotent — if a caller
 * has already pre-stamped a message, withTimestamp will append a second
 * one, so callers should pass un-stamped strings. The funds-available and
 * compounding paths upstream pre-stamp; we detect that with a marker so
 * we don't double-stamp.
 */
function wrapNotifier(rawNotify) {
  return async function wrappedNotify(chatId, message, opts) {
    if (!rateLimiter.canSendAlert()) {
      // dropped — already logged inside canSendAlert()
      return;
    }
    let outgoing = message;
    if (typeof outgoing === 'string' && !outgoing.includes('🕐')) {
      // For private chats chatId === userId, so we can use it as a TZ key.
      // Group chats fall through and render UTC (timezoneManager returns
      // DEFAULT_TZ for unknown ids).
      outgoing = withTimestamp(outgoing, 'bottom', chatId);
    }
    return rawNotify(chatId, outgoing, opts);
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
          // R(v4) — persistent BASKET_OPEN dedup gate.
          //
          // If 3+ "new" positions appear in one cycle, openAlerts will
          // classify it as BASKET_OPEN. Before letting it fire, hash
          // (wallet + sorted positions) and check the persisted JSON
          // store. If we already alerted this exact basket in the last
          // 7 days (or whatever BASKET_DEDUP_TTL_DAYS is set to), drop
          // the alert and update the in-memory snapshot so subsequent
          // cycles see prev=current and don't re-classify.
          //
          // Sub-3-position openings (INDIVIDUAL_OPEN) fall through
          // unchanged — those are real new opens, not basket re-fires.
          let suppressedByDedup = false;
          const isBasketCandidate =
            newPositions.length >= openAlerts.BASKET_MIN_COUNT;
          let basketDedupPositions = null;
          if (isBasketCandidate && basketDedup.ENABLED) {
            basketDedupPositions = newPositions.map((p) => ({
              coin: p.coin,
              side: p.side || (p.size < 0 ? 'SHORT' : 'LONG'),
              entryPx: p.entryPrice,
            }));
            const dedupCheck = basketDedup.checkAlreadyAlerted(
              wallet,
              basketDedupPositions
            );
            if (dedupCheck.wasAlerted) {
              const hoursAgo = (
                (Date.now() - dedupCheck.alertedAt) / 3600000
              ).toFixed(1);
              console.log(
                `[basketDedup] suppressed duplicate BASKET_OPEN for ${label} ` +
                  `— already alerted ${hoursAgo}h ago ` +
                  `(hash=${dedupCheck.hash.slice(0, 12)}...)`
              );
              suppressedByDedup = true;
            }
          }

          if (!suppressedByDedup) {
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
            // Persist the basket hash AFTER successful dispatch so a
            // notify failure doesn't poison the dedup store.
            if (isBasketCandidate && basketDedup.ENABLED && basketDedupPositions) {
              try {
                basketDedup.markAsAlerted(wallet, basketDedupPositions);
              } catch (e) {
                console.error(
                  '[basketDedup] markAsAlerted failed:',
                  e && e.message ? e.message : e
                );
              }
            }
          }
        }
      } catch (e) {
        console.error(
          '[extensions] openAlerts hook failed:',
          e && e.message ? e.message : e
        );
        healthServer.recordError(e);
      }
    }

    // 2. compounding (R(v3): wrapped with TWAP-aware gate)
    //
    // The legacy compoundingDetector triggered on +10% notional growth even
    // when a TWAP was filling — apr-30 false positive ("Notional anterior
    // $20,227 → $22,454 (+11.0%)" while v6 basket TWAP was still mid-fill).
    // We now require BOTH:
    //   • compoundingDetector says COMPOUND_DETECTED (legacy signal)
    //   • compoundingGate.detectCompounding() returns isCompounding=true
    //     (TWAP-active suppression + post-TWAP cooldown + account-value-grew check)
    //
    // The gate also silently maintains its own snapshot so it remains
    // calibrated independently from the legacy detector.
    if (compoundingDetector.isEnabled()) {
      try {
        const legacy = compoundingDetector.checkForCompounding(
          chatId,
          wallet,
          allPositions
        );

        // Compute aggregate notional + account value for the gate snapshot
        const notional = (allPositions || []).reduce((sum, p) => {
          const sz = Math.abs(Number(p.size) || 0);
          const px = Number(p.markPrice || p.entryPrice) || 0;
          return sum + sz * px;
        }, 0);
        // accountValue isn't always wired through this path; fall back to
        // notional + a small buffer when missing so the gate's
        // account-grew check doesn't false-block on mostly-unleveraged
        // baskets. The compounding gate degrades gracefully when prevAcct
        // is 0 (skips that gate).
        const accountValue = Number(
          (allPositions && allPositions.accountValue) ||
            notional * 0.3 ||
            0
        );

        const gate = compoundingGate.detectCompounding(
          wallet,
          allPositions,
          notional,
          accountValue
        );

        if (legacy.type === 'COMPOUND_DETECTED' && gate.isCompounding) {
          const isPrimary = walletConfig.isPrimaryWallet(wallet);
          const baseMsg = compoundingDetector.formatCompoundAlert(label, legacy);
          const msg = withTimestamp(appendFooter(baseMsg, isPrimary), 'bottom');
          await notify(chatId, msg, { parse_mode: 'Markdown' });
          const evt = {
            type: 'COMPOUND',
            chatId: String(chatId),
            wallet,
            prev_notional: legacy.prevNotional,
            current_notional: legacy.currentNotional,
            growth_pct: legacy.growth * 100,
          };
          try { await principalBridge.publish(evt); }
          catch (_) { eventLog.recordEvent(evt); }
        } else if (legacy.type === 'COMPOUND_DETECTED') {
          // Legacy detector wanted to fire but gate blocked → forensic log
          console.log(
            `[extensions] suppressed compounding for ${label}: gate=${gate.reason}`
          );
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

  // 6b. R-PUBLIC — /track UI + /timezone command
  if (bot) {
    try { commandsTrack.attach(bot); }
    catch (e) {
      console.error(
        '[extensions] commandsTrack.attach failed:',
        e && e.message ? e.message : e
      );
    }
    try { commandsTimezone.attach(bot); }
    catch (e) {
      console.error(
        '[extensions] commandsTimezone.attach failed:',
        e && e.message ? e.message : e
      );
    }
    try { commandsStart.attach(bot); }
    catch (e) {
      console.error(
        '[extensions] commandsStart.attach failed:',
        e && e.message ? e.message : e
      );
    }

    // R-AUTOCOPY — wire all R-AUTOCOPY commands. Each attach() is wrapped so
    // a failure in one module doesn't break the rest of the bootstrap.
    const wireAutocopy = [
      ['signalsChannel', () => signalsChannel.attach(bot)],
      ['commandsSignals', () => commandsSignals.attach(bot)],
      ['commandsCopyAuto', () => commandsCopyAuto.attach(bot)],
      ['commandsCapital', () => commandsCapital.attach(bot)],
      ['commandsPortfolio', () => commandsPortfolio.attach(bot)],
      ['commandsLeaderboard', () => commandsLeaderboard.attach(bot)],
      ['commandsAlertsConfig', () => commandsAlertsConfig.attach(bot)],
      ['commandsStats', () => commandsStats.attach(bot)],
      ['commandsShare', () => commandsShare.attach(bot)],
      ['commandsLearn', () => commandsLearn.attach(bot)],
      ['commandsHelp', () => commandsHelp.attach(bot)],
    ];
    for (const [name, fn] of wireAutocopy) {
      try { fn(); }
      catch (e) {
        console.error(
          `[extensions] ${name}.attach failed:`,
          e && e.message ? e.message : e
        );
      }
    }
  }

  // 7. Monkey-patch monitor for lifecycle hooks
  patchMonitor(monitor, hooks);

  // 8. R(v3) — External wallet tracker (legacy env-var-driven path).
  //    Kept for backward-compat; per-user wallet tracking lives in module 9.
  let externalTrackerTimer = null;
  if (primaryChatId && externalWalletTracker.isEnabled()) {
    try {
      externalTrackerTimer = externalWalletTracker.startSchedule({
        notify: wrappedNotify,
        primaryChatId,
      });
    } catch (e) {
      console.error(
        '[extensions] externalWalletTracker.startSchedule failed:',
        e && e.message ? e.message : e
      );
    }
  }

  // 9. R-PUBLIC — Per-user wallet tracker scheduler.
  //    Polls all addresses subscribed via /track (across all users) and
  //    fans out OPEN/CLOSE alerts to each subscriber with their local TZ
  //    + Pear "Copy trade" inline-keyboard button.
  let walletTrackerTimer = null;
  try {
    walletTrackerTimer = walletTrackerScheduler.startSchedule({
      notify: wrappedNotify,
    });
  } catch (e) {
    console.error(
      '[extensions] walletTrackerScheduler.startSchedule failed:',
      e && e.message ? e.message : e
    );
  }

  // 10. R-AUTOCOPY — wire copyAuto dispatcher + /feedback + dailyDigest.
  //     All of these need access to the wrappedNotify (rate-limited + branded).
  try {
    copyAuto.attach(wrappedNotify);
  } catch (e) {
    console.error(
      '[extensions] copyAuto.attach failed:',
      e && e.message ? e.message : e
    );
  }
  if (bot) {
    try {
      commandsFeedback.attach(bot, () => wrappedNotify);
    } catch (e) {
      console.error(
        '[extensions] commandsFeedback.attach failed:',
        e && e.message ? e.message : e
      );
    }
  }
  let dailyDigestTimer = null;
  try {
    dailyDigestTimer = dailyDigest.startSchedule({ notify: wrappedNotify });
  } catch (e) {
    console.error(
      '[extensions] dailyDigest.startSchedule failed:',
      e && e.message ? e.message : e
    );
  }

  console.log('[extensions] R(v2)+R(v3)+R-PUBLIC+R-AUTOCOPY bootstrap complete.');

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
      try {
        if (externalTrackerTimer) clearInterval(externalTrackerTimer);
      } catch (_) {}
      try {
        externalWalletTracker.stopSchedule();
      } catch (_) {}
      try {
        if (walletTrackerTimer) clearInterval(walletTrackerTimer);
      } catch (_) {}
      try { walletTrackerScheduler.stopSchedule(); } catch (_) {}
      try {
        if (dailyDigestTimer) clearInterval(dailyDigestTimer);
      } catch (_) {}
      try { dailyDigest.stopSchedule(); } catch (_) {}
    },
  };
}

module.exports = {
  bootstrap,
  wrapNotifier,
  buildHooks,
  patchMonitor,
};
