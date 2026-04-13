const { ethers } = require('ethers');

// HyperLend is an Aave v3 fork deployed on HyperEVM (chainId 999)
// Docs: https://docs.hyperlend.finance
// Pool address is the main entrypoint for all lending/borrow operations
const DEFAULT_RPC = 'https://rpc.hyperliquid.xyz/evm';
const DEFAULT_POOL = '0x00A89d7a5A02160f20150EbEA7a2b5E4879A1A8b';

// Aave v3 getUserAccountData returns amounts in the oracle's base currency.
// On HyperLend (Aave v3 fork), the base currency is USD with 8 decimals.
const BASE_DECIMALS = 8;

const POOL_ABI = [
  'function getUserAccountData(address user) view returns (uint256 totalCollateralBase, uint256 totalDebtBase, uint256 availableBorrowsBase, uint256 currentLiquidationThreshold, uint256 ltv, uint256 healthFactor)'
];

class HyperLendApi {
  constructor({ rpcUrl = DEFAULT_RPC, poolAddress = DEFAULT_POOL } = {}) {
    this.rpcUrl = rpcUrl;
    this.poolAddress = poolAddress;
    this.provider = new ethers.JsonRpcProvider(rpcUrl, { name: 'hyperevm', chainId: 999 });
    this.pool = new ethers.Contract(poolAddress, POOL_ABI, this.provider);
  }

  async getAccountData(address) {
    const r = await this.pool.getUserAccountData(address);
    const toUsd = (v) => Number(ethers.formatUnits(v, BASE_DECIMALS));
    // healthFactor uses 18 decimals (ray/wad). When there's no debt it returns
    // 2^256-1 (infinity), so cap it for display.
    const hfRaw = r[5];
    const MAX = (1n << 255n);
    const healthFactor = hfRaw >= MAX ? Infinity : Number(ethers.formatUnits(hfRaw, 18));
    return {
      totalCollateralUsd: toUsd(r[0]),
      totalDebtUsd: toUsd(r[1]),
      availableBorrowsUsd: toUsd(r[2]),
      currentLiquidationThreshold: Number(r[3]) / 10000,
      ltv: Number(r[4]) / 10000,
      healthFactor,
    };
  }
}

module.exports = HyperLendApi;
