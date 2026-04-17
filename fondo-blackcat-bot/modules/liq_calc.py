"""HyperLend liquidation calculator — /liqcalc command.

Computes HF scenarios for different HYPE price drops, plus
recovery options (repay debt / deposit collateral) to reach target HFs.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from modules.hyperlend import fetch_all_hyperlend
from modules.market import coingecko_prices

log = logging.getLogger(__name__)

# Known liquidation thresholds per wallet label (from HyperLend config)
_LT_OVERRIDES: dict[str, float] = {
    "Reserva": 0.74,
    "HyperLend Reserva": 0.74,
    "Principal": 0.655,
    "HyperLend Principal": 0.655,
}

# Price-drop scenarios to evaluate (fraction, e.g. 0.05 = −5%)
SCENARIOS = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30]

# Target HFs for recovery suggestions
TARGET_HFS = [1.25, 1.30]


def _hf_icon(hf: float) -> str:
    if hf >= 1.30:
        return "🟢"
    if hf >= 1.20:
        return "🟡"
    if hf >= 1.10:
        return "⚠️"
    if hf >= 1.00:
        return "🔴"
    return "💀"


def _fmt_k(v: float) -> str:
    """Format large numbers with K suffix."""
    if abs(v) >= 1_000:
        return f"{v/1_000:.1f}K"
    return f"{v:.1f}"


def _compute_scenarios(
    hype_price: float,
    collateral_usd: float,
    debt_usd: float,
    lt: float,
    label: str,
    wallet_short: str,
) -> str:
    """Build the full liquidation-calculator text block for one wallet."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Current HF
    hf_current = (collateral_usd * lt) / debt_usd if debt_usd > 0 else float("inf")

    # Estimate collateral in HYPE tokens (assumes ~100% HYPE collateral)
    hype_tokens = collateral_usd / hype_price if hype_price > 0 else 0

    lines = [
        f"🧮 LIQ CALCULATOR — {label} ({wallet_short})",
        f"⏱ {now}",
        "",
        "Estado actual:",
        f"  HYPE: ${hype_price:,.2f}",
        f"  Colateral: ~{_fmt_k(hype_tokens)} HYPE (${_fmt_k(collateral_usd)})",
        f"  Deuda: ${_fmt_k(debt_usd)} USDH",
        f"  LT: {lt}",
        f"  HF actual: {hf_current:.3f} {_hf_icon(hf_current)}",
        "",
        "Escenarios (caída HYPE):",
    ]

    for drop in SCENARIOS:
        new_price = hype_price * (1 - drop)
        # Collateral scales linearly with HYPE price
        new_collateral = hype_tokens * new_price
        new_hf = (new_collateral * lt) / debt_usd if debt_usd > 0 else float("inf")
        icon = _hf_icon(new_hf)
        pct_label = f"-{int(drop*100)}%"
        tag = ""
        if new_hf < 1.0:
            tag = " LIQUIDADO"
        elif new_hf < 1.01:
            tag = " LIQUIDACIÓN"
        elif new_hf < 1.10:
            tag = " EMERGENCIA"
        elif new_hf < 1.20:
            tag = " CRÍTICO"
        lines.append(
            f"  HYPE {pct_label:>4} (${new_price:,.2f}) → HF {new_hf:.3f}  {icon}{tag}"
        )

    # Recovery suggestions
    for target_hf in TARGET_HFS:
        lines.append("")
        lines.append(f"Para llevar HF a {target_hf:.2f}:")

        # Option A: repay debt (keep collateral constant)
        # target_hf = (collateral_usd * lt) / new_debt  ->  new_debt = (collateral_usd * lt) / target_hf
        needed_debt = (collateral_usd * lt) / target_hf
        repay_amount = debt_usd - needed_debt
        if repay_amount > 0:
            lines.append(f"  Opción A: Repagar ${_fmt_k(repay_amount)} USDH")
        else:
            lines.append("  Opción A: ✅ Ya cumplido con deuda actual")

        # Option B: deposit more collateral (keep debt constant)
        # target_hf = (new_collateral * lt) / debt_usd  ->  new_collateral = (target_hf * debt_usd) / lt
        needed_collateral = (target_hf * debt_usd) / lt if lt > 0 else 0
        extra_collateral_usd = needed_collateral - collateral_usd
        if extra_collateral_usd > 0:
            extra_hype = extra_collateral_usd / hype_price if hype_price > 0 else 0
            lines.append(
                f"  Opción B: Depositar {_fmt_k(extra_hype)} HYPE (${_fmt_k(extra_collateral_usd)})"
            )
        else:
            lines.append("  Opción B: ✅ Ya cumplido con colateral actual")

    return "\n".join(lines)


async def liq_calc() -> str:
    """Main entry point for /liqcalc command.

    Fetches current HyperLend positions + HYPE price and returns
    a formatted liquidation-scenario report for each active wallet.
    """
    hl_data, prices = await asyncio.gather(
        fetch_all_hyperlend(),
        coingecko_prices(),
    )

    # Get HYPE price
    hype_price = (prices.get("HYPE") or {}).get("price_usd")
    if not hype_price:
        return "❌ No se pudo obtener precio de HYPE desde CoinGecko."

    blocks: list[str] = []

    for entry in hl_data:
        if entry.get("status") != "ok":
            continue
        d = entry["data"]
        collateral = d.get("total_collateral_usd", 0.0)
        debt = d.get("total_debt_usd", 0.0)
        if collateral < 1 or debt < 1:
            continue  # skip wallets without active lending positions

        label = d.get("label", entry.get("label", "?"))
        wallet = d.get("wallet", "")
        wallet_short = wallet[:6] + "…" + wallet[-4:] if len(wallet) > 10 else wallet

        # Determine LT: use known override or on-chain value
        lt = d.get("current_liquidation_threshold", 0.0)
        for key, override_lt in _LT_OVERRIDES.items():
            if key.lower() in label.lower():
                lt = override_lt
                break

        block = _compute_scenarios(
            hype_price=hype_price,
            collateral_usd=collateral,
            debt_usd=debt,
            lt=lt,
            label=label,
            wallet_short=wallet_short,
        )
        blocks.append(block)

    if not blocks:
        return "ℹ️ No hay posiciones activas en HyperLend para calcular liquidación."

    return "\n\n" + ("═" * 35) + "\n\n".join(blocks)
