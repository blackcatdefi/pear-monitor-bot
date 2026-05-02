const {
  getAllChatIds,
  getWallets,
  getBorrowWallets,
  loadState,
  saveState,
  shortenAddress,
} = require('./store');
const {
  shouldSendAlert,
  aggregateClosePnl,
  classifyCloseReason,
  trackCloseForBasket,
  formatCloseAlert,
  formatBasketSummary,
} = require('./closeAlerts');
// R(v3) — TWAP-aware gating + timestamp helper. Imported here so the
// existing edge-triggered funds-available branch can pass through the
// gate without any structural rewrite of monitor.js.
const { recordOpenEvent, isTWAPActive } = require('./twapDetector');
const { shouldFireFundsAvailable } = require('./fundsAvailableGate');
const { withTimestamp } = require('./timestampHelper');

class PositionMonitor {
  constructor(hlApi, notifyFn, hlendApi = null) {
    this.hlApi = hlApi;
    this.hlendApi = hlendApi;
    this.notify = notifyFn; // async (chatId, message) => {}
    this.interval = null;
    this.minAvailableBalance = 50;
    this.minBorrowAvailable = 50; // HyperLend: alert when >= $50 borrowable
  }

  async start(intervalSeconds = 30) {
    console.log(`Monitor started, polling every ${intervalSeconds}s`);
    await this.poll(true);
    this.interval = setInterval(() => this.poll(false), intervalSeconds * 1000);
  }

  stop() {
    if (this.interval) {
      clearInterval(this.interval);
      this.interval = null;
    }
  }

  async poll(silent = false) {
    const chatIds = getAllChatIds();
    if (chatIds.length === 0) return;

    for (const chatId of chatIds) {
      const wallets = getWallets(chatId);
      const state = loadState(chatId);

      for (const wallet of wallets) {
        try {
          await this.checkWallet(chatId, wallet, state, silent);
        } catch (error) {
          console.error(
            `Error checking wallet ${wallet.label} for chat ${chatId}:`,
            error.message
          );
        }
      }

      if (this.hlendApi) {
        const borrowWallets = getBorrowWallets(chatId);
        for (const wallet of borrowWallets) {
          try {
            await this.checkBorrowWallet(chatId, wallet, state, silent);
          } catch (error) {
            console.error(
              `Error checking HyperLend wallet ${wallet.label} for chat ${chatId}:`,
              error.message
            );
          }
        }
      }

      saveState(chatId, state);
    }
  }

  async checkBorrowWallet(chatId, wallet, state, silent) {
    const addr = wallet.address;
    const label = wallet.label || shortenAddress(addr);

    if (!state.borrow) state.borrow = {};
    if (!state.borrow[addr]) state.borrow[addr] = { hadBorrowAvailable: null };
    const bs = state.borrow[addr];

    const data = await this.hlendApi.getAccountData(addr);
    const available = data.availableBorrowsUsd;

    const crossedThreshold =
      available >= this.minBorrowAvailable && bs.hadBorrowAvailable === false;
    if (crossedThreshold && !silent) {
      const hf = data.healthFactor === Infinity ? '∞' : data.healthFactor.toFixed(2);
      await this.notify(
        chatId,
        [
          `🏦 *HyperLend — Borrow Available!*`,
          ``,
          `📍 Wallet: ${label}`,
          `💸 Available to borrow: $${available.toFixed(2)}`,
          `🔒 Collateral: $${data.totalCollateralUsd.toFixed(2)}`,
          `💳 Current debt: $${data.totalDebtUsd.toFixed(2)}`,
          `❤️ Health factor: ${hf}`,
        ].join('\n')
      );
    }
    bs.hadBorrowAvailable = available >= this.minBorrowAvailable;
  }

  async checkWallet(chatId, wallet, state, silent) {
    const addr = wallet.address;
    const label = wallet.label || shortenAddress(addr);

    const allStates = await this.hlApi.getAllClearinghouseStates(addr);
    if (!allStates || allStates.length === 0) return;

    if (!state[addr]) {
      state[addr] = { positions: {}, triggerOrders: {}, hadFunds: null };
    }
    const ws = state[addr];
    if (!ws.triggerOrders) ws.triggerOrders = {};

    const allPositions = this.hlApi.aggregatePositions(allStates);
    const currentKeys = new Set(allPositions.map((p) => `${p.coin}`));

    // 1. Detect newly opened positions
    for (const pos of allPositions) {
      const key = `${pos.coin}`;
      if (!ws.positions[key]) {
        ws.positions[key] = {
          coin: pos.coin,
          dex: pos.dex,
          dexDisplay: pos.dexDisplay || pos.dex,
          side: pos.side,
          size: pos.size,
          entryPrice: pos.entryPrice,
          openedAt: new Date().toISOString(),
        };
        // R(v3): feed the TWAP detector with each new open so that 3+
        // distinct coins inside its sliding window flips the wallet into
        // "TWAP active" state and downstream gates suppress noise.
        try {
          recordOpenEvent(addr, pos.coin);
        } catch (_) {
          /* never let detection break the poll cycle */
        }
        if (!silent) {
          const dexTag =
            pos.dex !== 'Native' ? ` _(${pos.dexDisplay || pos.dex})_` : '';
          await this.notify(
            chatId,
            [
              `📈 *New position opened*`,
              ``,
              `📍 Wallet: ${label}`,
              `🪙 ${pos.coin}${dexTag} ${pos.side}`,
              `📏 Size: ${Math.abs(pos.size).toFixed(4)}`,
              `💲 Entry: $${pos.entryPrice.toFixed(2)}`,
              pos.leverage ? `⚡ Leverage: ${pos.leverage}x` : '',
            ]
              .filter(Boolean)
              .join('\n')
          );
        }
      }
    }

    // 2. Snapshot current trigger orders and find which disappeared per coin
    await this.hlApi.sleep(500);
    const currentTriggers = await this.hlApi.getAllTriggerOrders(addr);
    const currentTriggerIds = new Set(currentTriggers.map((o) => String(o.oid)));

    const disappearedTriggersByCoin = {};
    for (const [oid, oldOrder] of Object.entries(ws.triggerOrders)) {
      if (!currentTriggerIds.has(oid)) {
        const c = oldOrder.coin;
        if (!disappearedTriggersByCoin[c]) disappearedTriggersByCoin[c] = [];
        disappearedTriggersByCoin[c].push(oldOrder);
      }
    }

    // Persist current triggers BEFORE handling closes (idempotent on retry)
    ws.triggerOrders = {};
    for (const o of currentTriggers) {
      ws.triggerOrders[String(o.oid)] = {
        oid: o.oid,
        coin: o.coin,
        dex: o.dex,
        dexDisplay: o.dexDisplay || o.dex,
        orderType: o.orderType,
        triggerPx: o.triggerPx,
        triggerCondition: o.triggerCondition,
        side: o.side,
      };
    }

    // 3. Detect closed coins (= disappeared from positions). ONE alert per coin
    //    with reason classified from disappeared triggers and aggregated PnL
    //    summed from ALL fills since the position was opened.
    const closedCoins = Object.keys(ws.positions).filter((k) => !currentKeys.has(k));
    let fills = null; // lazy

    for (const coin of closedCoins) {
      const oldPos = ws.positions[coin];
      const dexTag =
        oldPos && oldPos.dex && oldPos.dex !== 'Native'
          ? ` _(${oldPos.dexDisplay || oldPos.dex})_`
          : '';

      if (!silent && shouldSendAlert(addr, coin)) {
        if (fills === null) {
          await this.hlApi.sleep(500);
          fills = (await this.hlApi.getUserFills(addr)) || [];
        }

        const sinceMs = oldPos.openedAt
          ? new Date(oldPos.openedAt).getTime()
          : Date.now() - 7 * 24 * 60 * 60 * 1000;
        const { pnl, exitPrice, fees } = aggregateClosePnl(fills, coin, sinceMs);

        const reason = classifyCloseReason(
          disappearedTriggersByCoin[coin] || [],
          exitPrice
        );

        const msg = formatCloseAlert({ label, oldPos, pnl, exitPrice, reason, dexTag });
        await this.notify(chatId, msg);

        // Track for basket-close summary (3+ within 5 minutes)
        trackCloseForBasket(
          chatId,
          addr,
          label,
          {
            coin,
            pnl,
            fees,
            side: oldPos.side,
            entryPrice: oldPos.entryPrice,
            exitPrice,
            reason,
          },
          async (cId, _w, lbl, closes) => {
            const summary = formatBasketSummary(lbl, closes);
            await this.notify(cId, summary);
          }
        );
      }

      delete ws.positions[coin];
    }

    // 4. Update positions for those still open (sizes / unrealized PnL / mark)
    for (const pos of allPositions) {
      ws.positions[`${pos.coin}`] = {
        ...ws.positions[`${pos.coin}`],
        size: pos.size,
        unrealizedPnl: pos.unrealizedPnl,
        markPrice: pos.markPrice,
      };
    }

    // 5. Funds-available alert (edge-triggered + R(v3) TWAP-aware gate)
    //
    // The legacy logic only fired on the rising edge (hadFunds: false → true),
    // but during a TWAP fill BCD's wallet rapidly toggles around the
    // minAvailableBalance ($50) threshold, giving the user 5+ noise alerts
    // per basket. R(v3) routes the candidate alert through
    // shouldFireFundsAvailable() which suppresses:
    //   1. anything during an active TWAP
    //   2. residuals below FUNDS_AVAILABLE_THRESHOLD_USD ($200 default)
    //   3. duplicates within a 1h window for the same wallet+amount bucket
    //
    // ws.hadFunds is still maintained so the original edge-trigger remains
    // a candidate gate — the new gate stacks ON TOP, never bypasses it.
    const agg = this.hlApi.aggregateBalances(allStates);
    const available = agg.totalWithdrawable;

    if (available >= this.minAvailableBalance && ws.hadFunds === false) {
      if (!silent) {
        const gate = shouldFireFundsAvailable(addr, available);
        if (gate.shouldFire) {
          const baseMsg = [
            `💰 *Funds available to trade!*`,
            ``,
            `📍 Wallet: ${label}`,
            `💵 Available: $${available.toFixed(2)}`,
            `📊 Account value: $${agg.totalAccountValue.toFixed(2)}`,
            `📈 Margin used: $${agg.totalMarginUsed.toFixed(2)}`,
          ].join('\n');
          await this.notify(chatId, withTimestamp(baseMsg, 'bottom'));
        } else {
          // Helpful for forensic log review when investigating why an alert
          // didn't fire — surfaces TWAP_ACTIVE / BELOW_THRESHOLD_x / RECENTLY_ALERTED
          console.log(
            `[monitor] suppressed funds-available for ${label} (` +
              `$${available.toFixed(2)}): ${gate.reason}`
          );
        }
      }
    }
    ws.hadFunds = available >= this.minAvailableBalance;
  }
}

module.exports = PositionMonitor;
