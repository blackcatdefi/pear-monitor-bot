"""HyperLend (Aave v3 fork on HyperEVM) — on-chain reader.

Reads user account data via the Pool contract's `getUserAccountData(address)`.
Aave v3 base currency is USD with 8 decimals.

Also attempts supply/borrow APY by reading `getReserveData` for configured
reserves (kHYPE collateral, USDH debt). APYs are approximated from liquidity
rates (ray, 1e27) using Aave's formula:

    apy = ((1 + rate / SECONDS_PER_YEAR) ** SECONDS_PER_YEAR) - 1

Graceful fallback: if on-chain calls fail, returns best-effort dict with
`error` populated so the report keeps building.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from web3 import Web3
from web3.exceptions import Web3Exception

from config import HYPEREVM_RPC, HYPERLEND_POOL_ADDRESS, HYPERLEND_WALLET

log = logging.getLogger(__name__)

SECONDS_PER_YEAR = 31_536_000
RAY = 10**27

# Aave v3 Pool ABI — only the methods we need
POOL_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "user", "type": "address"}],
        "name": "getUserAccountData",
        "outputs": [
            {"internalType": "uint256", "name": "totalCollateralBase", "type": "uint256"},
            {"internalType": "uint256", "name": "totalDebtBase", "type": "uint256"},
            {"internalType": "uint256", "name": "availableBorrowsBase", "type": "uint256"},
            {"internalType": "uint256", "name": "currentLiquidationThreshold", "type": "uint256"},
            {"internalType": "uint256", "name": "ltv", "type": "uint256"},
            {"internalType": "uint256", "name": "healthFactor", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "asset", "type": "address"}],
        "name": "getReserveData",
        "outputs": [
            {
                "components": [
                    {"internalType": "uint256", "name": "data", "type": "uint256"}
                ],
                "internalType": "struct DataTypes.ReserveConfigurationMap",
                "name": "configuration",
                "type": "tuple",
            },
            {"internalType": "uint128", "name": "liquidityIndex", "type": "uint128"},
            {"internalType": "uint128", "name": "currentLiquidityRate", "type": "uint128"},
            {"internalType": "uint128", "name": "variableBorrowIndex", "type": "uint128"},
            {"internalType": "uint128", "name": "currentVariableBorrowRate", "type": "uint128"},
            {"internalType": "uint128", "name": "currentStableBorrowRate", "type": "uint128"},
            {"internalType": "uint40", "name": "lastUpdateTimestamp", "type": "uint40"},
            {"internalType": "uint16", "name": "id", "type": "uint16"},
            {"internalType": "address", "name": "aTokenAddress", "type": "address"},
            {"internalType": "address", "name": "stableDebtTokenAddress", "type": "address"},
            {"internalType": "address", "name": "variableDebtTokenAddress", "type": "address"},
            {"internalType": "address", "name": "interestRateStrategyAddress", "type": "address"},
            {"internalType": "uint128", "name": "accruedToTreasury", "type": "uint128"},
            {"internalType": "uint128", "name": "unbacked", "type": "uint128"},
            {"internalType": "uint128", "name": "isolationModeTotalDebt", "type": "uint128"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]


def _w3() -> Web3:
    return Web3(Web3.HTTPProvider(HYPEREVM_RPC, request_kwargs={"timeout": 15}))


def _apy_from_ray(rate_ray: int) -> float:
    if rate_ray <= 0:
        return 0.0
    rate = rate_ray / RAY  # per-second rate
    # compound continuously — (1 + r/N)^N ≈ e^r for per-second rates
    try:
        return ((1 + rate / SECONDS_PER_YEAR) ** SECONDS_PER_YEAR) - 1
    except OverflowError:
        return 0.0


def _sync_fetch(wallet: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "wallet": wallet,
        "hf": None,
        "total_collateral_usd": None,
        "total_debt_usd": None,
        "available_borrow_usd": None,
        "ltv": None,
        "liquidation_threshold": None,
        "supply_apy": None,
        "borrow_apy": None,
        "error": None,
    }
    try:
        w3 = _w3()
        pool = w3.eth.contract(
            address=Web3.to_checksum_address(HYPERLEND_POOL_ADDRESS),
            abi=POOL_ABI,
        )
        data = pool.functions.getUserAccountData(
            Web3.to_checksum_address(wallet)
        ).call()
        (total_coll_base, total_debt_base, avail_borrow_base, liq_thr, ltv, hf_raw) = data
        # Aave v3: base currency = USD, 8 decimals
        out["total_collateral_usd"] = total_coll_base / 1e8
        out["total_debt_usd"] = total_debt_base / 1e8
        out["available_borrow_usd"] = avail_borrow_base / 1e8
        # liquidation threshold + ltv are basis points (bps / 1e4)
        out["liquidation_threshold"] = liq_thr / 1e4
        out["ltv"] = ltv / 1e4
        # healthFactor is 1e18 scaled; if no debt, Aave returns uint256 max
        if hf_raw >= 2**255:
            out["hf"] = float("inf")
        else:
            out["hf"] = hf_raw / 1e18
    except (Web3Exception, ValueError, OSError, TimeoutError) as e:
        out["error"] = f"rpc error: {e}"
        log.warning("HyperLend RPC read failed: %s", e)
    except Exception as e:  # noqa: BLE001 - last resort
        out["error"] = f"unexpected: {e}"
        log.exception("HyperLend unexpected error")
    return out


async def get_account_data(wallet: str | None = None) -> dict[str, Any]:
    target = wallet or HYPERLEND_WALLET
    return await asyncio.to_thread(_sync_fetch, target)


async def get_health_factor(wallet: str | None = None) -> float | None:
    data = await get_account_data(wallet)
    hf = data.get("hf")
    if hf is None:
        return None
    return float(hf) if hf != float("inf") else float("inf")


def format_hyperlend(data: dict[str, Any]) -> str:
    if data.get("error"):
        return f"HyperLend: error ({data['error']})"
    hf = data.get("hf")
    if hf == float("inf"):
        hf_str = "∞ (sin deuda)"
    elif hf is None:
        hf_str = "—"
    else:
        emoji = "🟢" if hf >= 1.20 else ("🟡" if hf >= 1.10 else "🔴")
        hf_str = f"{emoji} {hf:.3f}"
    coll = data.get("total_collateral_usd") or 0
    debt = data.get("total_debt_usd") or 0
    avail = data.get("available_borrow_usd") or 0
    return (
        f"HyperLend — HF: {hf_str}\n"
        f"  Colateral: ${coll:,.0f}\n"
        f"  Borrowed:  ${debt:,.0f}\n"
        f"  Borrow disponible: ${avail:,.0f}\n"
        f"  LTV {(data.get('ltv') or 0)*100:.0f}% / Liq {(data.get('liquidation_threshold') or 0)*100:.0f}%"
    )
