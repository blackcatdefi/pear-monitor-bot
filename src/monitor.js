const { loadWallets, loadState, saveState, shortenAddress } = require('./store');

class PositionMonitor {
  constructor(hlApi, notifyFn) {
    this.hlApi = hlApi;
    this.notify = notifyFn;
    this.interval = null;
    this.minAvailableBalance = 10; // USDC threshold
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
    const wallets = loadWallets();
    if (wallets.length === 0) return;

    const state = loadState();

    for (const wallet of wallets) {
      try {
        await this.checkWallet(wallet, state, silent);
      } catch (error) {
        console.error(`Error checking wallet ${wallet.label}:`, error.message);
      }
    }

    saveState(state);
  }

  async checkWallet(wallet, state, silent) {
    const addr = wallet.address;
    const label = wallet.label || shortenAddress(addr);

    // Get ALL states: native + HIP-3 dexes
    const allStates = await this.hlApi.getAllClearinghouseStates(addr);
    if (!allStates || allStates.length === 0) return;

    if (!state[addr]) {
      state[addr] = { positions: {}, triggerOrders: {}, hadFunds: null };
    }
    const ws = state[addr];
    if (!ws.triggerOrders) ws.triggerOrders = {};

    // --- Positions across all dexes ---
    const allPositions = this.hlApi.aggregatePositions(allStates);
    const currentKeys = new Set(allPositions.map(p => `${p.coin}`));

    // Detect newly opened positions
    for (const pos of allPositions) {
      const key = `${pos.coin}`;
      if (!ws.positions[key]) {
        ws.positions[key] = {
          coin: pos.coin,
          dex: pos.dex,
          side: pos.side,
          size: pos.size,
          entryPrice: pos.entryPrice,
          openedAt: new Date().toISOString()
        };
        if (!silent) {
          const dexTag = pos.dex !== 'Native' ? ` _(${pos.dex})_` : '';
          await this.notify([
            `📈 *New position opened*`,
            ``,
            `📍 Wallet: ${label}`,
            `🪙 ${pos.coin}${dexTag} ${pos.side}`,
            `📏 Size: ${Math.abs(pos.size).toFixed(4)}`,
            `💲 Entry: $${pos.entryPrice.toFixed(2)}`,
            pos.leverage ? `⚡ Leverage: ${pos.leverage}x` : '',
          ].filter(Boolean).join('\n'));
        }
      }
    }

    // --- Check trigger orders (TP/SL) ---
    await this.hlApi.sleep(500);
    const currentTriggers = await this.hlApi.getAllTriggerOrders(addr);
    const currentTriggerIds = new Set(currentTriggers.map(o => String(o.oid)));

    // Detect executed trigger orders (was there before, now gone)
    if (!silent) {
      for (const [oid, oldOrder] of Object.entries(ws.triggerOrders)) {
        if (!currentTriggerIds.has(oid)) {
          // Trigger order disappeared - check if position also closed
          const posKey = `${oldOrder.coin}`;
          const positionStillOpen = currentKeys.has(posKey);

          const isTP = oldOrder.orderType.includes('Take Profit');
          const isSL = oldOrder.orderType.includes('Stop');

          if (isTP || isSL) {
            // Get PnL from fills
            await this.hlApi.sleep(500);
            const fills = await this.hlApi.getUserFills(addr);
            const recentFill = fills?.filter(f => f.coin === oldOrder.coin)
              .sort((a, b) => b.time - a.time)[0];

            const closedPnl = recentFill ? parseFloat(recentFill.closedPnl || 0) : 0;
            const pnlStr = closedPnl >= 0 ? `+$${closedPnl.toFixed(2)}` : `-$${Math.abs(closedPnl).toFixed(2)}`;
            const pnlEmoji = closedPnl >= 0 ? '🟢' : '🔴';

            const oldPos = ws.positions[posKey];
            const dexTag = oldOrder.dex !== 'Native' ? ` _(${oldOrder.dex})_` : '';

            let typeLabel;
            if (isTP) typeLabel = '🎯 *TAKE PROFIT hit!*';
            else typeLabel = '🛑 *STOP LOSS triggered!*';

            await this.notify([
              typeLabel,
              ``,
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

    // Update stored trigger orders
    ws.triggerOrders = {};
    for (const o of currentTriggers) {
      ws.triggerOrders[String(o.oid)] = {
        oid: o.oid,
        coin: o.coin,
        dex: o.dex,
        orderType: o.orderType,
        triggerPx: o.triggerPx,
        triggerCondition: o.triggerCondition,
        side: o.side,
      };
    }

    // Detect closed positions (without trigger - manual close or liquidation)
    const closedKeys = Object.keys(ws.positions).filter(k => !currentKeys.has(k));
    for (const key of closedKeys) {
      const oldPos = ws.positions[key];
      // Only notify if we didn't already notify via trigger detection above
      const wasTrigger = Object.values(ws.triggerOrders || {}).some(t => `${t.coin}` === key);
      if (!silent && !wasTrigger) {
        await this.notifyManualClose(addr, label, oldPos);
      }
      delete ws.positions[key];
    }

    // Update stored positions
    for (const pos of allPositions) {
      const key = `${pos.coin}`;
      ws.positions[key] = {
        ...ws.positions[key],
        size: pos.size,
        unrealizedPnl: pos.unrealizedPnl,
        markPrice: pos.markPrice,
      };
    }

    // --- Check funds ---
    const agg = this.hlApi.aggregateBalances(allStates);
    const available = agg.totalWithdrawable;
    const hadFunds = ws.hadFunds;

    if (available >= this.minAvailableBalance && hadFunds === false) {
      if (!silent) {
        await this.notify([
          `💰 *Funds available to trade!*`,
          ``,
          `📍 Wallet: ${label}`,
          `💵 Available: $${available.toFixed(2)}`,
          `📊 Account value: $${agg.totalAccountValue.toFixed(2)}`,
          `📈 Margin used: $${agg.totalMarginUsed.toFixed(2)}`,
        ].join('\n'));
      }
    }

    ws.hadFunds = available >= this.minAvailableBalance;
  }

  async notifyManualClose(addr, label, oldPos) {
    await this.hlApi.sleep(500);
    const fills = await this.hlApi.getUserFills(addr);
    const recentFill = fills?.filter(f => f.coin === oldPos.coin)
      .sort((a, b) => b.time - a.time)[0];

    if (!recentFill) return;

    const closedPnl = parseFloat(recentFill.closedPnl || 0);
    if (closedPnl === 0) return; // Opening fill, not a close

    const pnlStr = closedPnl >= 0 ? `+$${closedPnl.toFixed(2)}` : `-$${Math.abs(closedPnl).toFixed(2)}`;
    const pnlEmoji = closedPnl >= 0 ? '🟢' : '🔴';
    const dexTag = oldPos.dex && oldPos.dex !== 'Native' ? ` _(${oldPos.dex})_` : '';

    await this.notify([
      `📋 *Position closed*`,
      ``,
      `📍 Wallet: ${label}`,
      `🪙 ${oldPos.coin}${dexTag} ${oldPos.side || ''}`,
      `${pnlEmoji} PnL: ${pnlStr}`,
      oldPos.entryPrice ? `💲 Entry: $${oldPos.entryPrice.toFixed(2)}` : '',
      `💲 Close: $${parseFloat(recentFill.px).toFixed(2)}`,
    ].filter(Boolean).join('\n'));
  }
}

module.exports = PositionMonitor;
