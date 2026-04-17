"""Liquidation calculator — 2D matrix HYPE × DEBT-ASSET for the flywheel.

Given the current HyperLend position (collateral = kHYPE, debt = UETH/USDH/etc)
and live prices, this module generates a Health Factor matrix so the user can
see at a glance where liquidation risk lives.

Rows = HYPE price change (-20% … +20%)
Cols = debt-asset price change (-10% … +20%)
Cell = projected HF (💀 if < 1.0)

The borrowed asset is detected DYNAMICALLY from hyperlend.HyperLend — no
hardcoded assumption of USDH. If debt is stable (symbol_to_ticker returns
"USD"), the column dimension is collapsed.
"""
from __future__ import annotations

import logging
import math
from typing import Any

from modules.hyperlend import (
    fetch_all_hyperlend,
    project_health_factor,
    symbol_to_ticker,
)
from modules.market import coingecko_prices

log = logging.getLogger(__name__)

# Default matrix axes — HYPE always shown (collateral), debt shown only when
# the borrowed asset isn't USD-pegged.
HYPE_DELTAS = [-0.30, -0.20, -0.10, 0.0, 0.10, 0.20]
DEBT_DELTAS = [-0.20, -0.10, 0.0, 0.10, 0.20]
STABLE_DELTAS = [0.0]  # if debt is USD-pegged, only a single column

# Rough 7-day adverse scenario used as the "worst 7-day" callout. Not a
# prediction — just a plausible stress-test envelope for HYPE vs ETH.
WORST_7D_HYPE_DELTA = -0.25
WORST_7D_DEBT_DELTA = 0.15   # used if debt is ETH-like
WORST_7D_STABLE_DELTA = 0.0  # stables don't move in the worst scenario


def _fmt_pct(v: float) -> str:
    return f"{int(round(v * 100)):+d}%"


def _fmt_hf_cell(hf: float) -> str:
    if math.isinf(hf):
        return "  ∞  "
    if hf < 1.0:
        return f"{hf:.2f}💀"
    if hf < 1.1:
        return f"{hf:.2f}⚠"
    return f"{hf:.2f} "


def _resolve_debt_price(debt_symbol: str | None, prices: dict[str, Any]) -> float:
    """Return current USD price for the debt asset.  Returns 1.0 for USD stables."""
    if not debt_symbol:
        return 1.0
    ticker = symbol_to_ticker(debt_symbol)
    if ticker == "USD":
        return 1.0
    entry = prices.get(ticker) or {}
    px = entry.get("price_usd") if isinstance(entry, dict) else None
    if px:
        return float(px)
    log.warning("No price for debt ticker %s (symbol=%s) — defaulting to 1.0", ticker, debt_symbol)
    return 1.0


def _resolve_collateral_price(collateral_symbol: str | None, prices: dict[str, Any]) -> float:
    if not collateral_symbol:
        return 0.0
    ticker = symbol_to_ticker(collateral_symbol)
    entry = prices.get(ticker) or {}
    px = entry.get("price_usd") if isinstance(entry, dict) else None
    return float(px) if px else 0.0


def build_matrix_text(
    label: str,
    wallet_short: str,
    collateral_symbol: str,
    collateral_balance: float,
    collateral_price: float,
    debt_symbol: str,
    debt_balance: float,
    debt_price: float,
    liquidation_threshold: float,
) -> str:
    """Render one 2D HF matrix for a single wallet."""
    debt_ticker = symbol_to_ticker(debt_symbol)
    is_stable = debt_ticker == "USD"
    col_deltas = STABLE_DELTAS if is_stable else DEBT_DELTAS

    lines: list[str] = []
    lines.append(f"[{label}] {wallet_short}")
    lines.append(
        f"  Colateral: {collateral_balance:.4f} {collateral_symbol} @ ${collateral_price:,.2f}"
    )
    lines.append(
        f"  Deuda:     {debt_balance:.4f} {debt_symbol} @ ${debt_price:,.2f}"
        + ("  (stable)" if is_stable else "")
    )
    lines.append(f"  LT = {liquidation_threshold:.2%}")
    lines.append("")

    # Header row
    col_hdr = "  HYPE \\ " + debt_ticker
    header_cells = [_fmt_pct(d).rjust(6) for d in col_deltas]
    lines.append(col_hdr.ljust(14) + " | " + " | ".join(header_cells))
    lines.append("─" * (14 + 3 + sum(len(c) + 3 for c in header_cells)))

    for hype_d in HYPE_DELTAS:
        row_cells: list[str] = []
        for debt_d in col_deltas:
            c_px = collateral_price * (1 + hype_d)
            d_px = debt_price * (1 + debt_d)
            hf = project_health_factor(
                collateral_balance=collateral_balance,
                collateral_price=c_px,
                liquidation_threshold=liquidation_threshold,
                debt_balance=debt_balance,
                debt_price=d_px,
            )
            row_cells.append(_fmt_hf_cell(hf).rjust(6))
        label_col = f"  {_fmt_pct(hype_d).rjust(6)}"
        lines.append(label_col.ljust(14) + " | " + " | ".join(row_cells))

    # Worst-7d callout
    worst_hype = WORST_7D_HYPE_DELTA
    worst_debt = WORST_7D_STABLE_DELTA if is_stable else WORST_7D_DEBT_DELTA
    c_px = collateral_price * (1 + worst_hype)
    d_px = debt_price * (1 + worst_debt)
    worst_hf = project_health_factor(
        collateral_balance, c_px, liquidation_threshold, debt_balance, d_px
    )
    worst_icon = "💀" if worst_hf < 1.0 else ("⚠" if worst_hf < 1.1 else "")
    lines.append("")
    lines.append(
        f"  Worst-7d ({_fmt_pct(worst_hype)} HYPE / {_fmt_pct(worst_debt)} "
        f"{debt_ticker}): HF = {worst_hf:.3f} {worst_icon}".rstrip()
    )

    # Recovery options — compute what it takes to restore HF=1.20 under worst-7d
    target_hf = 1.20
    # Repay debt needed: x such that (coll * c_px * LT) / ((debt-x) * d_px) >= target_hf
    # => x >= debt - (coll * c_px * LT) / (target_hf * d_px)
    try:
        repay_needed_raw = debt_balance - (
            collateral_balance * c_px * liquidation_threshold
        ) / (target_hf * d_px)
        repay_needed = max(0.0, repay_needed_raw)
    except ZeroDivisionError:
        repay_needed = 0.0
    # Additional collateral needed (same LT, in units of collateral asset)
    try:
        add_coll_needed_raw = (
            (target_hf * debt_balance * d_px) / (liquidation_threshold * c_px)
            - collateral_balance
        )
        add_coll_needed = max(0.0, add_coll_needed_raw)
    except ZeroDivisionError:
        add_coll_needed = 0.0
    # Required debt-asset price drop to auto-recover (holding everything else equal)
    # HF>=target => d_px_needed <= (coll * c_px * LT) / (debt * target_hf)
    try:
        d_px_needed = (collateral_balance * c_px * liquidation_threshold) / (
            debt_balance * target_hf
        )
        price_drop_needed_pct = max(0.0, 1 - d_px_needed / d_px)
    except ZeroDivisionError:
        price_drop_needed_pct = 0.0

    lines.append("")
    lines.append(f"  Recovery options para volver a HF={target_hf:.2f}:")
    lines.append(
        f"    • Repay {repay_needed:.4f} {debt_symbol} "
        f"(~{_fmt_dollars(repay_needed * d_px)})"
    )
    lines.append(
        f"    • Deposit {add_coll_needed:.4f} {collateral_symbol} extra "
        f"(~{_fmt_dollars(add_coll_needed * c_px)})"
    )
    if not is_stable:
        lines.append(
            f"    • O esperar a que {debt_ticker} baje ≥ {price_drop_needed_pct * 100:.1f}%"
        )
    return "\n".join(lines)


def _fmt_dollars(v: float) -> str:
    if v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v/1_000:.1f}K"
    return f"${v:.2f}"


async def compute_liq_matrix() -> str:
    """Full /liqcalc output: one matrix per wallet with debt."""
    hl_list = await fetch_all_hyperlend()
    prices = await coingecko_prices()

    blocks: list[str] = []
    blocks.append("💀 LIQ CALC — MATRIZ HYPE × DEUDA")
    blocks.append("─" * 40)
    blocks.append(
        "HF proyectado bajo escenarios de precio. 💀 = liquidación, ⚠ = zona crítica."
    )
    blocks.append("")

    any_debt = False
    for r in hl_list:
        if r.get("status") != "ok":
            continue
        d = r["data"]
        coll_sym = d.get("collateral_symbol")
        coll_bal = d.get("collateral_balance") or 0.0
        debt_sym = d.get("debt_symbol")
        debt_bal = d.get("debt_balance") or 0.0
        if not debt_sym or debt_bal <= 0 or not coll_sym or coll_bal <= 0:
            continue
        any_debt = True
        coll_px = _resolve_collateral_price(coll_sym, prices)
        debt_px = _resolve_debt_price(debt_sym, prices)
        if coll_px <= 0:
            blocks.append(
                f"[{d.get('label','—')}] Sin precio para {coll_sym} — skip matriz."
            )
            blocks.append("")
            continue
        lt = d.get("current_liquidation_threshold") or 0.74
        wallet = d.get("wallet") or ""
        wshort = (wallet[:6] + "…" + wallet[-4:]) if wallet else ""
        blocks.append(
            build_matrix_text(
                label=d.get("label", "—"),
                wallet_short=wshort,
                collateral_symbol=coll_sym,
                collateral_balance=coll_bal,
                collateral_price=coll_px,
                debt_symbol=debt_sym,
                debt_balance=debt_bal,
                debt_price=debt_px,
                liquidation_threshold=lt,
            )
        )
        blocks.append("")

    if not any_debt:
        blocks.append("— Sin deuda activa en ningún wallet.")

    return "\n".join(blocks)
