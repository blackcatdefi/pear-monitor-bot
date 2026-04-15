"""
HyperLend on-chain reader.

HyperLend es un fork de Aave v3 corriendo en HyperEVM. Leemos el Pool
contract directamente via web3.py para obtener `getUserAccountData(address)`.

ABI signature (Aave v3 Pool):
    getUserAccountData(address user) returns (
        uint256 totalCollateralBase,
        uint256 totalDebtBase,
        uint256 availableBorrowsBase,
        uint256 currentLiquidationThreshold,
        uint256 ltv,
        uint256 healthFactor
    )

Base currency = USD con 8 decimales (Aave v3 default).
healthFactor viene con 18 decimales (ray/1e9).
"""
from __future__ import annotations

import logging
from typing import Any

from web3 import Web3

from config import HYPEREVM_RPC, HYPERLEND_POOL_ADDRESS, HYPERLEND_WALLET

log = logging.getLogger(__name__)

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


def _w3() -> Web3:
    return Web3(Web3.HTTPProvider(HYPEREVM_RPC, request_kwargs={"timeout": 15}))


def _to_usd(raw: int) -> float:
    """Aave base currency: 8 decimales de USD."""
    return raw / 1e8


def _to_hf(raw: int) -> float:
    """healthFactor: 18 decimales. Si no hay debt retorna un valor enorme."""
    return raw / 1e18


def get_account_data(wallet: str | None = None) -> dict[str, Any] | None:
    """Lee el estado completo del user en HyperLend Pool."""
    wallet = wallet or HYPERLEND_WALLET
    try:
        w3 = _w3()
        pool = w3.eth.contract(
            address=Web3.to_checksum_address(HYPERLEND_POOL_ADDRESS),
            abi=POOL_ABI,
        )
        data = pool.functions.getUserAccountData(
            Web3.to_checksum_address(wallet)
        ).call()
        collateral, debt, borrowable, liq_threshold, ltv, hf = data
        return {
            "wallet": wallet,
            "collateral_usd": _to_usd(collateral),
            "debt_usd": _to_usd(debt),
            "available_borrows_usd": _to_usd(borrowable),
            "liquidation_threshold_bps": liq_threshold,  # bps (0-10000)
            "ltv_bps": ltv,
            "health_factor": _to_hf(hf),
            "raw": data,
        }
    except Exception as e:  # noqa: BLE001
        log.exception("HyperLend getUserAccountData failed: %s", e)
        return None


async def get_health_factor(wallet: str | None = None) -> float:
    """Helper async (aunque la llamada es sync) para uso uniforme."""
    data = get_account_data(wallet)
    if not data:
        return float("inf")
    return data["health_factor"]


def format_hyperlend_summary(data: dict | None, hype_price: float | None = None) -> str:
    if not data:
        return "❌ HyperLend: no disponible (RPC error)"

    hf = data["health_factor"]
    hf_str = f"{hf:.2f}" if hf < 1000 else "∞ (no debt)"

    # Alerta visual según umbral
    if hf < 1.10:
        hf_emoji = "🔴"
    elif hf < 1.20:
        hf_emoji = "⚠️"
    else:
        hf_emoji = "✅"

    lines = [
        "🏦 *HYPERLEND*",
        f"  {hf_emoji} HF: {hf_str}",
        f"  Colateral: ${data['collateral_usd']:,.0f}",
        f"  Borrowed: ${data['debt_usd']:,.0f}",
        f"  Available borrows: ${data['available_borrows_usd']:,.0f}",
        f"  LTV: {data['ltv_bps']/100:.2f}% | Liq threshold: {data['liquidation_threshold_bps']/100:.2f}%",
    ]
    if hype_price:
        lines.append(f"  (HYPE ref: ${hype_price:.2f})")
    return "\n".join(lines)
