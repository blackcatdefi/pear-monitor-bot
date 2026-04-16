"""HyperLend on-chain reader (Aave v3 fork on HyperEVM).

Reads getUserAccountData() from the Pool contract via web3.py to obtain:
- totalCollateralBase (USD, 8 decimals)
- totalDebtBase (USD, 8 decimals)
- availableBorrowsBase (USD, 8 decimals)
- currentLiquidationThreshold (basis points / 1e4)
- ltv (basis points / 1e4)
- healthFactor (1e18 wad; 2^256-1 means infinity / no debt)

Mirrors the proven Node implementation in src/hyperLendApi.js.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from web3 import Web3

from config import (
    FUND_WALLETS,
    HYPEREVM_CHAIN_ID,
    HYPEREVM_RPC,
    HYPERLEND_POOL_ADDRESS,
    HYPERLEND_WALLET,
)

log = logging.getLogger(__name__)

BASE_DECIMALS = 8
HF_DECIMALS = 18
MAX_UINT_THRESHOLD = 1 << 255  # values >= here are treated as "infinity" (no debt)

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
    }
]


class HyperLend:
    def __init__(
        self,
        rpc_url: str = HYPEREVM_RPC,
        pool_address: str = HYPERLEND_POOL_ADDRESS,
    ) -> None:
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 20}))
        self.pool = self.w3.eth.contract(
            address=Web3.to_checksum_address(pool_address),
            abi=POOL_ABI,
        )

    def _get_account_data_sync(self, address: str) -> dict[str, Any]:
        addr = Web3.to_checksum_address(address)
        r = self.pool.functions.getUserAccountData(addr).call()
        total_collateral_base, total_debt_base, available_borrows_base, liq_threshold_bps, ltv_bps, hf_raw = r

        def to_usd(v: int) -> float:
            return v / (10 ** BASE_DECIMALS)

        if hf_raw >= MAX_UINT_THRESHOLD:
            health_factor: float = float("inf")
        else:
            health_factor = hf_raw / (10 ** HF_DECIMALS)

        return {
            "wallet": addr,
            "total_collateral_usd": to_usd(total_collateral_base),
            "total_debt_usd": to_usd(total_debt_base),
            "available_borrows_usd": to_usd(available_borrows_base),
            "current_liquidation_threshold": liq_threshold_bps / 10000,
            "ltv": ltv_bps / 10000,
            "health_factor": health_factor,
        }

    async def get_account_data(self, address: str = HYPERLEND_WALLET) -> dict[str, Any]:
        # web3.py is sync — run in a thread to avoid blocking the event loop
        return await asyncio.to_thread(self._get_account_data_sync, address)


async def fetch_hyperlend(address: str = HYPERLEND_WALLET) -> dict[str, Any]:
    """Fetch HyperLend state for a single wallet with graceful error handling."""
    try:
        client = HyperLend()
        data = await client.get_account_data(address)
        return {"status": "ok", "data": data}
    except Exception as exc:  # noqa: BLE001
        log.exception("HyperLend fetch failed")
        return {"status": "error", "error": str(exc)}


async def fetch_all_hyperlend() -> list[dict[str, Any]]:
    """Fetch HyperLend state for ALL fund wallets + the legacy HYPERLEND_WALLET.

    Returns a list of {status, data|error, label} dicts.
    Only wallets with non-zero collateral are included in the response.
    """
    # Build list of (address, label) pairs, deduplicating
    wallets: dict[str, str] = {}
    # Legacy HYPERLEND_WALLET always included
    if HYPERLEND_WALLET:
        wallets[HYPERLEND_WALLET] = "HyperLend Principal"
    # All fund wallets
    for addr, label in FUND_WALLETS.items():
        if addr not in wallets:
            wallets[addr] = label

    if not wallets:
        return [{"status": "error", "error": "no_wallets_configured", "label": "?"}]

    client = HyperLend()

    async def _query(addr: str, label: str) -> dict[str, Any]:
        try:
            data = await client.get_account_data(addr)
            data["label"] = label
            return {"status": "ok", "data": data, "label": label}
        except Exception as exc:  # noqa: BLE001
            log.warning("HyperLend fetch %s (%s) failed: %s", label, addr[:10], exc)
            return {"status": "error", "error": str(exc), "label": label}

    results = await asyncio.gather(*[_query(a, l) for a, l in wallets.items()])

    # Filter: only return wallets that have non-zero collateral (or errors)
    filtered: list[dict[str, Any]] = []
    for r in results:
        if r["status"] == "error":
            continue  # skip failed / zero wallets silently
        coll = r["data"].get("total_collateral_usd", 0.0) or 0.0
        if coll > 0.01:
            filtered.append(r)

    if not filtered:
        # No wallet has HyperLend — return a single "no positions" entry
        return [{"status": "ok", "data": {
            "wallet": "",
            "label": "—",
            "total_collateral_usd": 0.0,
            "total_debt_usd": 0.0,
            "available_borrows_usd": 0.0,
            "current_liquidation_threshold": 0.0,
            "ltv": 0.0,
            "health_factor": float("inf"),
        }, "label": "—"}]

    return filtered


async def get_health_factor(address: str = HYPERLEND_WALLET) -> float | None:
    res = await fetch_hyperlend(address)
    if res["status"] == "ok":
        return res["data"]["health_factor"]
    return None
