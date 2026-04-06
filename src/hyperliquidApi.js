const axios = require('axios');

class HyperliquidApi {
  constructor(apiUrl = 'https://api.hyperliquid.xyz') {
    this.apiUrl = apiUrl;
    this.dexCache = null;
    this.dexCacheTime = 0;
    this.dexNameMap = {}; // short name -> fullName
  }

  async post(body) {
    const { data } = await axios.post(`${this.apiUrl}/info`, body);
    return data;
  }

  // Get list of all HIP-3 perp DEXs
  async getPerpDexs() {
    // Cache for 5 minutes
    if (this.dexCache && Date.now() - this.dexCacheTime < 5 * 60 * 1000) {
      return this.dexCache;
    }
    try {
      const data = await this.post({ type: 'perpDexs' });
      this.dexCache = data;
      this.dexCacheTime = Date.now();
      // Build name mapping
      this.dexNameMap = {};
      for (const d of data) {
        if (d && d.name && d.fullName) {
          this.dexNameMap[d.name] = d.fullName;
        }
      }
      return data;
    } catch (error) {
      console.error('Failed to get perp dexs:', error.message);
      return [];
    }
  }

  // Get clearinghouse state for native perps (dex = undefined) or a specific HIP-3 dex
  async getClearinghouseState(walletAddress, dex) {
    try {
      const body = { type: 'clearinghouseState', user: walletAddress };
      if (dex) body.dex = dex;
      return await this.post(body);
    } catch (error) {
      if (!error.message?.includes('429')) {
        console.error(`Failed to get HL state for ${walletAddress}${dex ? ` (dex: ${dex})` : ''}:`, error.message);
      }
      return null;
    }
  }

  // Get ALL clearinghouse states: native + all HIP-3 dexes where user has positions
  async getAllClearinghouseStates(walletAddress) {
    const results = [];

    // 1. Native perps
    const native = await this.getClearinghouseState(walletAddress);
    if (native) {
      results.push({ dex: null, state: native });
    }

    // 2. Get all HIP-3 dexes and check each one
    const dexes = await this.getPerpDexs();
    if (Array.isArray(dexes)) {
      for (const dex of dexes) {
        if (!dex || !dex.name) continue; // Skip null entries (index 0 = native)
        const dexName = dex.name;
        await this.sleep(200); // Rate limit protection
        const state = await this.getClearinghouseState(walletAddress, dexName);
        if (state) {
          const positions = this.getPositions(state);
          const balance = this.getBalanceInfo(state);
          // Only include if there are positions or meaningful balance
          if (positions.length > 0 || (balance && balance.accountValue > 0.01)) {
            results.push({ dex: dexName, state });
          }
        }
      }
    }

    return results;
  }

  async getUserFills(walletAddress) {
    try {
      return await this.post({ type: 'userFills', user: walletAddress });
    } catch (error) {
      console.error(`Failed to get fills for ${walletAddress}:`, error.message);
      return null;
    }
  }

  // Get open orders including trigger orders (TP/SL) for a specific dex
  async getFrontendOpenOrders(walletAddress, dex) {
    try {
      const body = { type: 'frontendOpenOrders', user: walletAddress };
      if (dex) body.dex = dex;
      return await this.post(body);
    } catch (error) {
      if (!error.message?.includes('429')) {
        console.error(`Failed to get open orders for ${walletAddress}${dex ? ` (dex: ${dex})` : ''}:`, error.message);
      }
      return null;
    }
  }

  // Get all trigger orders (TP/SL) across native + HIP-3 dexes
  async getAllTriggerOrders(walletAddress) {
    const allOrders = [];

    // Native
    const native = await this.getFrontendOpenOrders(walletAddress);
    if (native) {
      for (const o of native) {
        if (o.isTrigger && o.isPositionTpsl) allOrders.push({ ...o, dex: 'Native' });
      }
    }

    // HIP-3 dexes
    const dexes = await this.getPerpDexs();
    if (Array.isArray(dexes)) {
      for (const dex of dexes) {
        if (!dex || !dex.name) continue;
        await this.sleep(200);
        const orders = await this.getFrontendOpenOrders(walletAddress, dex.name);
        if (orders) {
          for (const o of orders) {
            if (o.isTrigger && o.isPositionTpsl) allOrders.push({ ...o, dex: dex.name, dexDisplay: this.getDexDisplayName(dex.name) });
          }
        }
      }
    }

    return allOrders;
  }

  // Get display name for a DEX (full name or short name fallback)
  getDexDisplayName(dexName) {
    if (!dexName || dexName === 'Native') return 'Native';
    return this.dexNameMap[dexName] || dexName;
  }

  sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  getBalanceInfo(state) {
    if (!state || !state.marginSummary) return null;
    const ms = state.marginSummary;
    return {
      accountValue: parseFloat(ms.accountValue || 0),
      totalMarginUsed: parseFloat(ms.totalMarginUsed || 0),
      totalNtlPos: parseFloat(ms.totalNtlPos || 0),
      withdrawable: parseFloat(state.withdrawable || 0),
    };
  }

  getPositions(state) {
    if (!state || !state.assetPositions) return [];
    return state.assetPositions
      .map(ap => {
        const p = ap.position;
        return {
          coin: p.coin,
          size: parseFloat(p.szi),
          entryPrice: parseFloat(p.entryPx),
          markPrice: parseFloat(p.positionValue) / Math.abs(parseFloat(p.szi)) || 0,
          unrealizedPnl: parseFloat(p.unrealizedPnl),
          returnOnEquity: parseFloat(p.returnOnEquity),
          leverage: p.leverage ? parseFloat(p.leverage.value) : null,
          liquidationPrice: p.liquidationPx ? parseFloat(p.liquidationPx) : null,
          side: parseFloat(p.szi) > 0 ? 'LONG' : 'SHORT',
        };
      })
      .filter(p => p.size !== 0);
  }

  // Aggregate balance across native + all HIP-3 dexes
  aggregateBalances(allStates) {
    let totalAccountValue = 0;
    let totalMarginUsed = 0;
    let totalWithdrawable = 0;
    const perDex = [];

    for (const { dex, state } of allStates) {
      const bal = this.getBalanceInfo(state);
      if (!bal) continue;
      totalAccountValue += bal.accountValue;
      totalMarginUsed += bal.totalMarginUsed;
      totalWithdrawable += bal.withdrawable;
      if (bal.accountValue > 0.01 || bal.totalMarginUsed > 0.01) {
        perDex.push({ dex: dex || 'Native', dexDisplay: this.getDexDisplayName(dex), ...bal });
      }
    }

    return { totalAccountValue, totalMarginUsed, totalWithdrawable, perDex };
  }

  // Aggregate positions across native + all HIP-3 dexes
  aggregatePositions(allStates) {
    const all = [];
    for (const { dex, state } of allStates) {
      const positions = this.getPositions(state);
      for (const pos of positions) {
        all.push({ ...pos, dex: dex || 'Native', dexDisplay: this.getDexDisplayName(dex) });
      }
    }
    return all;
  }
}

module.exports = HyperliquidApi;
