"""R-PMCORE — HyperLiquid Portfolio Margin state for the primary account.

Post-migration the fund holds its core capital as a spot HYPE balance in the
primary wallet (``0xc7ae``). Under Portfolio Margin that spot HYPE IS the
cross collateral — there is no separate "deposit as collateral" step, and the
ONLY borrowable asset is USDC/USDH (UETH borrow no longer exists). This module
derives the live PM state from the wallet's spot balances + perp positions:

* ``collateral_usd``   — value of PM-eligible spot (HYPE + configured assets)
                          at the live HL oracle price.
* ``debt_usd``         — USDC/USDH/USDT0 ACTUALLY borrowed (negative spot
                          balance). This is a real liability, NOT capacity.
* ``capacity_usd``     — borrow capacity = ``PM_HYPE_LTV × collateral`` (0.5×).
* ``available_usd``    — capacity − debt (head-room still borrowable).
* ``ratio``            — debt / capacity (utilisation of borrow capacity).
                          Thresholds: WARN 0.40, STRESS 0.70, LIQ 0.95.
* ``shorts_notional``  — total SHORT perp notional (the basket = the hedge).
* ``naked_long``       — debt drawn but NO shorts open → leveraged long with
                          no hedge (the fund's hard-rule violation).

Robustness: NEVER raises. Missing data → zeros and a CALM status. The HYPE
price is resolved via ``modules.hl_prices`` (keyless oracle) so a CoinGecko
outage never zeroes the collateral.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

try:
    from config import (
        PM_HYPE_LTV,
        PM_WARN_RATIO,
        PM_STRESS_RATIO,
        PM_CRITICAL_RATIO,
        PM_LIQ_RATIO,
        PM_COLLATERAL_ASSETS,
    )
except Exception:  # noqa: BLE001 — importable in isolated tests
    PM_HYPE_LTV = 0.50
    PM_WARN_RATIO = 0.40
    PM_STRESS_RATIO = 0.70
    PM_CRITICAL_RATIO = 0.85
    PM_LIQ_RATIO = 0.95
    PM_COLLATERAL_ASSETS = ["HYPE"]

log = logging.getLogger(__name__)

_STABLES = frozenset({"USDC", "USDH", "USDT", "USDT0", "USDE", "DAI", "USR", "USDHL"})


@dataclass(frozen=True)
class PMState:
    collateral_usd: float
    debt_usd: float
    capacity_usd: float
    available_usd: float
    ratio: float                      # debt / capacity (0..1+)
    status: str                       # CALM | WARN | STRESS | LIQ
    shorts_notional: float
    naked_long: bool
    hype_qty: float
    hype_px: float
    collateral_breakdown: dict[str, float] = field(default_factory=dict)
    has_data: bool = True


def _f(v: Any) -> float:
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _classify(ratio: float) -> str:
    if ratio >= PM_LIQ_RATIO:
        return "LIQ"
    if ratio >= PM_STRESS_RATIO:
        return "STRESS"
    if ratio >= PM_WARN_RATIO:
        return "WARN"
    return "CALM"


def compute_pm_state(
    spot_balances: list[dict[str, Any]] | None,
    positions: list[dict[str, Any]] | None,
    prices: dict[str, float] | None = None,
    *,
    hype_px: float | None = None,
) -> PMState:
    """Derive Portfolio Margin state from one wallet's spot + perp data.

    ``prices`` is an optional ``{COIN: usd}`` oracle map; when absent the HYPE
    price is fetched live via ``modules.hl_prices``. NEVER raises.
    """
    spot_balances = spot_balances or []
    positions = positions or []

    # Resolve prices: prefer the passed oracle map, fall back to live fetch.
    prices = dict(prices or {})
    if "HYPE" not in prices:
        try:
            from modules.hl_prices import get_oracle_prices
            for k, v in (get_oracle_prices() or {}).items():
                prices.setdefault(k, v)
        except Exception:  # noqa: BLE001
            pass
    if hype_px is not None and hype_px > 0:
        prices["HYPE"] = hype_px

    pm_assets = {a.upper() for a in (PM_COLLATERAL_ASSETS or ["HYPE"])}

    collateral = 0.0
    debt = 0.0
    hype_qty = 0.0
    breakdown: dict[str, float] = {}
    for sb in spot_balances:
        coin = (sb.get("coin") or "").upper()
        total = _f(sb.get("total"))
        # Borrowed stable shows as a NEGATIVE spot balance → real debt.
        if coin in _STABLES:
            if total < 0:
                debt += abs(total)  # stables are ~1:1 USD
            continue
        if coin in pm_assets:
            lookup = coin[1:] if coin.startswith("K") and len(coin) > 1 else coin
            px = prices.get(lookup) or prices.get(coin) or 0.0
            val = total * float(px or 0.0)
            if val > 0:
                collateral += val
                breakdown[coin] = breakdown.get(coin, 0.0) + val
            if coin == "HYPE":
                hype_qty += total

    shorts_notional = 0.0
    for p in positions:
        try:
            sz = float(p.get("size") or p.get("szi") or 0.0)
        except (TypeError, ValueError):
            sz = 0.0
        try:
            ntl = abs(float(p.get("notional_usd") or p.get("positionValue") or 0.0))
        except (TypeError, ValueError):
            ntl = 0.0
        if sz < 0:
            shorts_notional += ntl

    capacity = PM_HYPE_LTV * collateral
    available = capacity - debt
    ratio = (debt / capacity) if capacity > 0 else 0.0
    status = _classify(ratio)
    naked_long = debt > 1.0 and shorts_notional < 1.0

    return PMState(
        collateral_usd=collateral,
        debt_usd=debt,
        capacity_usd=capacity,
        available_usd=available,
        ratio=ratio,
        status=status,
        shorts_notional=shorts_notional,
        naked_long=naked_long,
        hype_qty=hype_qty,
        hype_px=float(prices.get("HYPE") or 0.0),
        collateral_breakdown=breakdown,
        has_data=(collateral > 0 or debt > 0 or len(spot_balances) > 0),
    )


def _fmt_usd(v: float) -> str:
    av = abs(v)
    if av >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if av >= 1_000:
        return f"${v / 1_000:.1f}K"
    return f"${v:,.0f}"


_STATUS_EMOJI = {"CALM": "🟢", "WARN": "🟡", "STRESS": "🟠", "LIQ": "🔴"}


def _display_band(ratio: float) -> tuple[str, str]:
    """R-PMALERT 4-level DISPLAY label for the ratio line: CALM/WARN/STRESS/
    LIQ-RISK with the 0.85 pre-liq tier mapped to 🔴. This is the rendering
    label only; ``PMState.status`` (the R-PMCORE classifier) is unchanged and
    still flips to LIQ at 0.95. NEVER raises.
    """
    try:
        r = float(ratio)
    except (TypeError, ValueError):
        return "🟢", "CALM"
    if r >= PM_CRITICAL_RATIO:
        return "🔴", "LIQ-RISK"
    if r >= PM_STRESS_RATIO:
        return "🟠", "STRESS"
    if r >= PM_WARN_RATIO:
        return "🟡", "WARN"
    return "🟢", "CALM"


def format_pm_state_telegram(pm: PMState) -> str:
    """Telegram block for the Portfolio Margin state. NEVER raises."""
    if pm is None or not pm.has_data or pm.collateral_usd <= 0:
        return ""
    emoji, band_label = _display_band(pm.ratio)
    lines = ["⚖️ PORTFOLIO MARGIN (cuenta primaria — HYPE como colateral cross)"]
    hype_line = f"├─ Colateral: {_fmt_usd(pm.collateral_usd)}"
    if pm.hype_qty > 0 and pm.hype_px > 0:
        hype_line += f"  ({pm.hype_qty:,.1f} HYPE × ${pm.hype_px:,.2f})"
    lines.append(hype_line)
    lines.append(f"├─ Deuda (USDC/USDH borrowed): {_fmt_usd(pm.debt_usd)}")
    lines.append(
        f"├─ Capacidad borrow (LTV {PM_HYPE_LTV:.2f}): {_fmt_usd(pm.capacity_usd)}"
        f"  | disponible: {_fmt_usd(pm.available_usd)}"
    )
    lines.append(
        f"├─ Margin ratio: {pm.ratio * 100:.1f}%  {emoji} {band_label}  "
        f"(WARN {PM_WARN_RATIO*100:.0f}% · STRESS {PM_STRESS_RATIO*100:.0f}% · "
        f"CRÍTICO {PM_CRITICAL_RATIO*100:.0f}% · LIQ {PM_LIQ_RATIO*100:.0f}%)"
    )
    if pm.shorts_notional > 0:
        lines.append(
            f"└─ Hedge (shorts basket): {_fmt_usd(pm.shorts_notional)} notional "
            f"vs colateral {_fmt_usd(pm.collateral_usd)}"
        )
    else:
        lines.append("└─ Hedge (shorts basket): sin shorts abiertos")
    if pm.naked_long:
        lines.append(
            "🚨 WARNING: USDC debt vs HYPE with no shorts open — "
            "naked leveraged long, hedge missing."
        )
    return "\n".join(lines)


def pm_alert(pm: PMState) -> tuple[bool, str]:
    """R-SILENT break-silence decision for the PM ratio.

    Returns ``(should_alert, message)``. Stays SILENT (False) below WARN
    (ratio < PM_WARN_RATIO). Breaks silence at WARN, escalates language at
    STRESS/LIQ. The naked-long condition ALSO breaks silence (hedge missing
    is a hard-rule violation regardless of ratio).
    """
    if pm is None or not pm.has_data:
        return False, ""
    if pm.naked_long:
        return True, (
            "🚨 PORTFOLIO MARGIN — HEDGE MISSING\n"
            f"Deuda {_fmt_usd(pm.debt_usd)} contra HYPE {_fmt_usd(pm.collateral_usd)} "
            "SIN shorts abiertos. Naked leveraged long — falta el hedge del basket."
        )
    if pm.status == "CALM":
        return False, ""
    head = {
        "WARN": "🟡 PORTFOLIO MARGIN — WARN",
        "STRESS": "🟠 PORTFOLIO MARGIN — STRESS (reducir deuda)",
        "LIQ": "🔴 PORTFOLIO MARGIN — LIQUIDACIÓN INMINENTE",
    }.get(pm.status, "🟡 PORTFOLIO MARGIN")
    return True, (
        f"{head}\n"
        f"Margin ratio {pm.ratio*100:.1f}% (deuda {_fmt_usd(pm.debt_usd)} / "
        f"capacidad {_fmt_usd(pm.capacity_usd)}). Colateral HYPE "
        f"{_fmt_usd(pm.collateral_usd)}."
    )
