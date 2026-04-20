"""Bounce Tech — query leveraged token positions on HyperEVM.

Bounce Tech deploys ERC-20 leveraged tokens (up to 10x, no liquidation risk)
that auto-rebalance via Hyperliquid perps. We call the LeveragedTokenHelper
contract to fetch all tokens a wallet holds plus their exchange rates.

Env / config:
    HYPEREVM_RPC — HyperEVM JSON-RPC (default https://rpc.hyperliquid.xyz/evm)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from web3 import Web3

from config import DATA_DIR, FUND_WALLETS, HYPEREVM_RPC

log = logging.getLogger(__name__)

# ─── Bounce Tech contract addresses (HyperEVM, chain 999) ──────────────────
LT_HELPER = Web3.to_checksum_address("0x31205dc06Ce1c0b3D30Fe0C0006D5A4Cb486b2FB")

# Minimal ABI — only the function we need
LT_HELPER_ABI: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "getLeveragedTokens",
        "inputs": [
            {"name": "user_", "type": "address"},
            {"name": "onlyHeld_", "type": "bool"},
        ],
        "outputs": [
            {
                "name": "",
                "type": "tuple[]",
                "components": [
                    {"name": "leveragedToken", "type": "address"},
                    {"name": "marketId", "type": "uint32"},
                    {"name": "targetAsset", "type": "string"},
                    {"name": "targetLeverage", "type": "uint256"},
                    {"name": "isLong", "type": "bool"},
                    {"name": "exchangeRate", "type": "uint256"},
                    {"name": "baseAssetBalance", "type": "uint256"},
                    {"name": "totalAssets", "type": "uint256"},
                    {"name": "userCredit", "type": "uint256"},
                    {"name": "credit", "type": "uint256"},
                    {
                        "name": "agentData",
                        "type": "tuple[3]",
                        "components": [
                            {"name": "slot", "type": "uint8"},
                            {"name": "agent", "type": "address"},
                            {"name": "createdAt", "type": "uint256"},
                        ],
                    },
                    {"name": "balanceOf", "type": "uint256"},
                    {"name": "mintPaused", "type": "bool"},
                    {"name": "isStandbyMode", "type": "bool"},
                ],
            }
        ],
        "stateMutability": "view",
    }
]


# ─── BT state persistence for close detection ──────────────────────────────
BT_STATE_FILE = os.path.join(DATA_DIR, "bt_state.json")


def _load_bt_state() -> dict[str, Any]:
    """Load previous Bounce Tech state from JSON."""
    if not os.path.isfile(BT_STATE_FILE):
        return {}
    try:
        with open(BT_STATE_FILE) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_bt_state(state: dict[str, Any]) -> None:
    """Save current Bounce Tech state to JSON."""
    try:
        os.makedirs(os.path.dirname(BT_STATE_FILE), exist_ok=True)
        with open(BT_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception:  # noqa: BLE001
        log.exception("Could not save BT state")


def _query_wallet_sync(wallet: str) -> dict[str, Any]:
    """Synchronous call to LeveragedTokenHelper.getLeveragedTokens(wallet, true)."""
    try:
        w3 = Web3(Web3.HTTPProvider(HYPEREVM_RPC, request_kwargs={"timeout": 15}))
        helper = w3.eth.contract(address=LT_HELPER, abi=LT_HELPER_ABI)
        raw = helper.functions.getLeveragedTokens(
            Web3.to_checksum_address(wallet), True
        ).call()

        positions: list[dict[str, Any]] = []
        for tok in raw:
            # tok is a tuple matching LeveragedTokenData components
            leverage = tok[3]       # targetLeverage (uint256, 1e18 scaled)
            exchange_rate = tok[5]  # exchangeRate (uint256, 1e18 scaled)
            balance = tok[11]       # balanceOf (uint256, 1e18 scaled)

            if balance == 0:
                continue

            # Convert from 1e18
            bal_f = balance / 1e18
            rate_f = exchange_rate / 1e18
            lev_f = leverage / 1e18
            value_usd = bal_f * rate_f

            positions.append({
                "token_address": tok[0],  # leveragedToken address
                "asset": tok[2],          # targetAsset (e.g. "HYPE", "BTC")
                "leverage": f"{lev_f:.0f}x",
                "is_long": tok[4],        # bool
                "direction": "LONG" if tok[4] else "SHORT",
                "balance": bal_f,
                "exchange_rate": rate_f,
                "value_usd": round(value_usd, 2),
                "mint_paused": tok[12],
                "standby": tok[13],
            })

        return {
            "status": "ok",
            "wallet": wallet,
            "positions": positions,
            "count": len(positions),
        }
    except Exception as exc:  # noqa: BLE001
        log.exception("Bounce Tech query failed for %s", wallet)
        return {"status": "error", "wallet": wallet, "error": str(exc)}


async def fetch_bounce_tech(wallet: str | None = None) -> list[dict[str, Any]]:
    """Query Bounce Tech leveraged token positions for fund wallets.

    If *wallet* is given, query only that address.
    Otherwise query all FUND_WALLETS.
    Returns a list of result dicts (one per wallet that has positions).
    """
    wallets = [wallet] if wallet else list(FUND_WALLETS.keys())
    if not wallets:
        return [{"status": "error", "error": "No wallets configured"}]

    results = await asyncio.gather(
        *(asyncio.to_thread(_query_wallet_sync, w) for w in wallets)
    )
    # Filter to wallets that actually have positions
    return [r for r in results if r.get("count", 0) > 0 or r.get("status") == "error"]


def detect_closes(current_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compare current BT positions with previous state, detect closes.

    Returns list of close event dicts for positions that disappeared.
    Always saves current state for next comparison.
    """
    prev = _load_bt_state()
    prev_positions = prev.get("positions", {})

    # Build current position map
    current_positions: dict[str, dict[str, Any]] = {}
    for r in current_results:
        if r.get("status") != "ok":
            continue
        for p in r.get("positions", []):
            addr = p.get("token_address", "")
            if addr:
                current_positions[addr] = {
                    "asset": p.get("asset", "?"),
                    "direction": p.get("direction", "?"),
                    "leverage": p.get("leverage", "?"),
                    "value_usd": p.get("value_usd", 0),
                    "balance": p.get("balance", 0),
                }

    # Detect closes (positions in prev but not in current)
    closes: list[dict[str, Any]] = []
    for addr, prev_p in prev_positions.items():
        if addr not in current_positions:
            closes.append({
                "token_address": addr,
                "asset": prev_p.get("asset", "?"),
                "direction": prev_p.get("direction", "?"),
                "leverage": prev_p.get("leverage", "?"),
                "last_value_usd": prev_p.get("value_usd", 0),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

    # Save current state
    _save_bt_state({
        "positions": current_positions,
        "last_update": datetime.now(timezone.utc).isoformat(),
    })

    return closes
