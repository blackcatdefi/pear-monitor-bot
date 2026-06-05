"""P1.6 — per-position funding tracking + expensive-carry flag.

For each open position the bot surfaces:
  • the live 8h funding rate (HL per-asset hourly funding × 8, in bps), and
  • cumulative funding paid since entry (HL ``cumFunding.sinceOpen``, USD;
    HL convention: positive = the position PAID funding, negative = received).

For LONG cycle-accumulation positions a "carry caro / reconsiderar" flag is
raised when the 8h funding rate sits at or below a configurable floor
(``FUNDING_EXPENSIVE_BPS_8H``, default −2.0 bp). Between the floor and 0 bp
the position is in a MONITOR zone (surfaced, not flagged). Example: ZEC at
−0.83 bp/8h is MONITOR (−0.83 > −2.0); at −5.5 bp/8h it FLAGS.

This module SCORES and FLAGS only — it never proposes a close/reduce. The
flag is MANUAL-REVIEW input for BCD (see P1.7), never an auto-action.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


def _expensive_floor_bps() -> float:
    try:
        return float(os.getenv("FUNDING_EXPENSIVE_BPS_8H", "-2.0"))
    except (TypeError, ValueError):
        return -2.0


@dataclass(frozen=True)
class PositionFunding:
    coin: str
    side: str                 # LONG | SHORT
    funding_8h_bps: float | None   # None when no live rate available
    cum_funding_usd: float    # paid since open (HL sign: + = paid, − = recv)
    zone: str                 # OK | MONITOR | FLAG | N/A
    expensive_carry: bool     # True only for LONG cycle positions past floor


async def fetch_funding_rates() -> dict[str, float]:
    """Return ``{coin: hourly_funding_rate}`` from HL metaAndAssetCtxs.

    Keyless public endpoint. Returns ``{}`` on any failure (the caller then
    renders funding as N/A rather than crashing). NEVER raises.
    """
    try:
        from modules.portfolio import meta_and_asset_ctxs
        data = await meta_and_asset_ctxs()
        if not isinstance(data, list) or len(data) < 2:
            return {}
        meta, ctxs = data[0], data[1]
        universe = (meta or {}).get("universe") or []
        out: dict[str, float] = {}
        for i, ctx in enumerate(ctxs or []):
            if i >= len(universe):
                break
            name = (universe[i] or {}).get("name")
            if not name:
                continue
            try:
                out[name] = float((ctx or {}).get("funding") or 0.0)
            except (TypeError, ValueError):
                continue
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch_funding_rates failed: %s", exc)
        return {}


def funding_8h_bps(hourly_rate: float | None) -> float | None:
    """Convert an HL hourly funding rate to 8h basis points."""
    if hourly_rate is None:
        return None
    try:
        return float(hourly_rate) * 8.0 * 10_000.0
    except (TypeError, ValueError):
        return None


def evaluate_position_funding(
    position: dict[str, Any],
    hourly_rate: float | None,
    *,
    is_cycle_long: bool = False,
    floor_bps: float | None = None,
) -> PositionFunding:
    """Build the funding read for one position. NEVER raises.

    ``is_cycle_long`` should be True only when the position is an on-chain
    LONG tagged ACUMULACIÓN CICLO — only those raise the expensive-carry
    flag. ``floor_bps`` overrides the configured floor (testing).
    """
    coin = str(position.get("coin") or "?")
    side = str(position.get("side") or "").upper() or "?"
    try:
        cum = float(position.get("cum_funding_since_open") or 0.0)
    except (TypeError, ValueError):
        cum = 0.0
    bps = funding_8h_bps(hourly_rate)
    floor = floor_bps if floor_bps is not None else _expensive_floor_bps()

    if bps is None:
        return PositionFunding(coin, side, None, cum, "N/A", False)

    # Zone is informational for every position; the FLAG (and only the FLAG)
    # is gated on LONG cycle-accumulation per the fund rule.
    if bps <= floor:
        zone = "FLAG" if is_cycle_long else "MONITOR"
        expensive = bool(is_cycle_long)
    elif bps < 0:
        zone = "MONITOR"
        expensive = False
    else:
        zone = "OK"
        expensive = False
    return PositionFunding(coin, side, bps, cum, zone, expensive)


def format_funding_line(pf: PositionFunding) -> str:
    """One compact line per position for the report. NEVER raises."""
    if pf.funding_8h_bps is None:
        return f"  • {pf.coin} {pf.side}: funding n/d | carry acum {pf.cum_funding_usd:+.2f} USD"
    tag = ""
    if pf.zone == "FLAG":
        tag = "  🚩 carry caro — MANUAL REVIEW (reconsiderar)"
    elif pf.zone == "MONITOR":
        tag = "  👁 monitoreo"
    return (
        f"  • {pf.coin} {pf.side}: {pf.funding_8h_bps:+.2f} bp/8h | "
        f"carry acum {pf.cum_funding_usd:+.2f} USD{tag}"
    )


def build_funding_block(
    positions: list[dict[str, Any]] | None,
    rates: dict[str, float] | None,
    cycle_long_coins: set[str] | None = None,
) -> str:
    """Render the per-position funding block. Empty string if no positions.

    ``cycle_long_coins`` = coins classified as LONG ACUMULACIÓN CICLO (only
    these can raise the expensive-carry flag). NEVER raises.
    """
    positions = positions or []
    rates = rates or {}
    cycle = {c.upper() for c in (cycle_long_coins or set())}
    rows: list[str] = []
    flagged = 0
    for p in positions:
        if not isinstance(p, dict):
            continue
        coin = str(p.get("coin") or "?")
        rate = rates.get(coin)
        is_cycle_long = (
            coin.upper() in cycle and str(p.get("side") or "").upper() == "LONG"
        )
        pf = evaluate_position_funding(p, rate, is_cycle_long=is_cycle_long)
        rows.append(format_funding_line(pf))
        if pf.expensive_carry:
            flagged += 1
    if not rows:
        return ""
    head = "💸 FUNDING POR POSICIÓN (8h rate · carry acumulado desde entry)"
    if flagged:
        head += f" — {flagged} en carry caro (MANUAL REVIEW)"
    return head + "\n" + "\n".join(rows)
