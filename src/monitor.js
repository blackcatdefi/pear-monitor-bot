const { getAllChatIds, getWallets, loadState, saveState, shortenAddress } = require('./store');

class PositionMonitor {
  constructor(hlApi, notifyFn) {
    this.hlApi = hlApi;
    this.notify = notifyFn; // async (chatId, message) => {}
    this.interval = null;
    this.minAvailableBalance = 10;
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
          console.error(`Error checking wallet ${wallet.label} for chat ${chatId}:`, error.message);
        }
      }

      saveState(chatId, state);
    }
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
    const currentKeys = new Set(allPositions.map(p => `${p.coin}`));

    // Detect newly opened positions
    for (const pos of allPositions) {
      const key = `${pos.coin}`;
      if (!ws.positions[key]) {
        ws.positions[key] = {
          coin: pos.coin, dex: pos.dex, dexDisplay: pos.dexDisplay || pos.dex,
          side: pos.side, size: pos.size, entryPrice: pos.entryPrice,
          openedAt: new Date().toISOString()
        };
        if (!silent) {
          const dexTag = pos.dex !== 'Native' ? ` _(${pos.dexDisplay || pos.dex})_` : '';
          await this.notify(chatId, [
            `📈 *New position opened*`, ``,
            `📍 Wallet: ${label}`,
            `🪙 ${pos.coin}${dexTag} ${pos.side}`,
            `📏 Size: ${Math.abs(pos.size).toFixed(4)}`,
            `💲 Entry: $${pos.entryPrice.toFixed(2)}`,
            pos.leverage ? `⚡ Leverage: ${pos.leverage}x` : '',
          ].filter(Boolean).join('\n'));
        }
      }
    }

    // Check trigger orders (TP/SL)
    await this.hlApi.sleep(500);
    const currentTriggers = await this.hlApi.getAllTriggerOrders(addr);
    const currentTriggerIds = new Set(currentTriggers.map(o => String(o.oid)));

    if (!silent) {
      for (const [oid, oldOrder] of Object.entries(ws.triggerOrders)) {
        if (!currentTriggerIds.has(oid)) {
          const posKey = `${oldOrder.coin}`;
          const positionStillOpen = currentKeys.has(posKey);
          const isTP = oldOrder.orderType.includes('Take Profit');
          const isSL = oldOrder.orderType.includes('Stop');

          if (isTP || isSL) {
            await this.hlApi.sleep(500);
            const fills = await this.hlApi.getUserFills(addr);
            const recentFill = fills?.filter(f => f.coin === oldOrder.coin)
              .sort((a, b) => b.time - a.time)[0];

            const closedPnl = recentFill ? parseFloat(recentFill.closedPnl || 0) : 0;

            // Skip alert if PnL is less than $1 to avoid false/dust alerts
            if (Math.abs(closedPnl) < 1) continue;

            const pnlStr = closedPnl >= 0 ? `+$${closedPnl.toFixed(2)}` : `-$${Math.abs(closedPnl).toFixed(2)}`;
            const pnlEmoji = closedPnl >= 0 ? '🟢' : '🔴';
            const oldPos = ws.positions[posKey];
            const dexTag = oldOrder.dex !== 'Native' ? ` _(${oldOrder.dexDisplay || oldOrder.dex})_` : '';
            const typeLabel = isTP ? '🎯 *TAKE PROFIT hit!*' : '🛑 *STOP LOSS triggered!*';

            await this.notify(chatId, [
              typeLabel, ``,
              `📍 Wallet: ${label}`,
              `🪙 ${oldOrder.coin}${dexTag}`,
              `${pnlEmoji} PnL: ${pnlStr}`,
              oldPos?.entryPrice ? `💲 Entry: $${oldPos.entryPrice.toFixed(2)}` : '',
              oldOrder.triggerPx ? `💲 Trigger: $${oldOrder.triggerPx}` : '',
              !positionStillOpen ? `📋 Position fully closed` : `📋 Partial close (position still open)`,
            ].filter(Boolean).join('\n'));
          }
        }
      }
    }

    // Update trigger orders
    ws.triggerOrders = {};
    for (const o of currentTriggers) {
      ws.triggerOrders[String(o.oid)] = {
        oid: o.oid, coin: o.coin, dex: o.dex, dexDisplay: o.dexDisplay || o.dex,
        orderType: o.orderType, triggerPx: o.triggerPx,
        triggerCondition: o.triggerCondition, side: o.side,
      };
    }

    // Detect manual closes
    const closedKeys = Object.keys(ws.positions).filter(k => !currentKeys.has(k));
    for (const key of closedKeys) {
      const oldPos = ws.positions[key];
      if (!silent) {
        await this.notifyManualClose(chatId, addr, label, oldPos);
      }
      delete ws.positions[key];
    }

    // Update positions
    for (const pos of allPositions) {
      ws.positions[`${pos.coin}`] = {
        ...ws.positions[`${pos.coin}`],
        size: pos.size, unrealizedPnl: pos.unrealizedPnl, markPrice: pos.markPrice,
      };
    }

    // Check funds
    const agg = this.hlApi.aggregateBalances(allStates);
    const available = agg.totalWithdrawable;

    if (available >= this.minAvailableBalance && ws.hadFunds === false) {
      if (!silent) {
        await this.notify(chatId, [
          `💰 *Funds available to trade!*`, ``,
          `📍 Wallet: ${label}`,
          `💵 Available: $${available.toFixed(2)}`,
          `📊 Account value: $${agg.totalAccountValue.toFixed(2)}`,
          `📈 Margin used: $${agg.totalMarginUsed.toFixed(2)}`,
        ].join('\n'));
      }
    }
    ws.hadFunds = available >= this.minAvailableBalance;
  }

  async notifyManualClose(chatId, addr, label, oldPos) {
    await this.hlApi.sleep(500);
    const fills = await this.hlApi.getUserFills(addr);
    const recentFill = fills?.filter(f => f.coin === oldPos.coin)
      .sort((a, b) => b.time - a.time)[0];
    if (!recentFill) return;

    const closedPnl = parseFloat(recentFill.closedPnl || 0);
    if (closedPnl === 0) return;

    const pnlStr = closedPnl >= 0 ? `+$${closedPnl.toFixed(2)}` : `-$${Math.abs(closedPnl).toFixed(2)}`;
    const pnlEmoji = closedPnl >= 0 ? '🟢' : '🔴';
    const dexTag = oldPos.dex && oldPos.dex !== 'Native' ? ` _(${oldPos.dexDisplay || oldPos.dex})_` : '';

    await this.notify(chatId, [
      `📋 *Position closed*`, ``,
      `📍 Wallet: ${label}`,
      `🪙 ${oldPos.coin}${dexTag} ${oldPos.side || ''}`,
      `${pnlEmoji} PnL: ${pnlStr}`,
      oldPos.entryPrice ? `💲 Entry: $${oldPos.entryPrice.toFixed(2)}` : '',
      `💲 Close: $${parseFloat(recentFill.px).toFixed(2)}`,
    ].filter(Boolean).join('\n'));
  }
}

module.exports = PositionMonitor;
