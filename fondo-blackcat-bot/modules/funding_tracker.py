"""P1.6 / R-AUDIT2-P0.1 — per-position funding tracking + expensive-carry flag.

For each open position the bot surfaces:
  • the live 8h funding rate (HL per-asset hourly funding × 8, in bps),
  • whether the position is PAYING or RECEIVING funding (direction-aware), and
  • cumulative funding since entry, shown in the FAVORABLE display convention
    (+ = net received / favorable / green, − = net paid / cost / red).

R-AUDIT2-P0.1 — funding-direction model (the 2026-06-05 fix).
The previous model flagged a LONG whenever the 8h rate sat at/below a NEGATIVE
floor — but a LONG with a negative HL funding rate is RECEIVING funding
(favorable income), not paying. That is backwards. The direction model is now
explicit, per the HL convention:

  HL convention: funding rate > 0  ⇒ LONGS pay shorts.
                 funding rate < 0  ⇒ SHORTS pay longs.

  • LONG  → PAYING when rate > 0, RECEIVING when rate < 0.
  • SHORT → PAYING when rate < 0, RECEIVING when rate > 0.

The "carry caro" (expensive-carry) flag fires ONLY when the position is
PAYING beyond a magnitude threshold (``FUNDING_EXPENSIVE_BPS_8H``, default
**1.5 bp/8h**, interpreted as a POSITIVE paid-per-8h magnitude), computed per
side — never on the rate sign alone. A position that is RECEIVING funding is
NEVER flagged expensive and NEVER shows a monitoring eye.

This module SCORES and FLAGS only — it never proposes a close/reduce. The flag
is MANUAL-REVIEW input for BCD (see P1.7), never an auto-action.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# Default expensive-carry threshold: PAID magnitude per 8h, in bps. A LONG
# paying ≥ +1.5 bp/8h (rate > 0) or a SHORT paying ≥ 1.5 bp/8h (rate < 0)
# raises the flag. Receiving positions never flag.
_DEFAULT_EXPENSIVE_BPS = 1.5


def _expensive_threshold_bps() -> float:
    """Positive paid-per-8h magnitude (bps) above which carry is 'expensive'.

    Defensive: a non-positive configured value (e.g. a stale ``-2.0`` left
    over from the pre-fix NEGATIVE-floor semantics) is meaningless under the
    new model and would flag every paying position — so it is clamped back to
    the safe default. The threshold is always a positive magnitude.
    """
    try:
        val = float(os.getenv("FUNDING_EXPENSIVE_BPS_8H", str(_DEFAULT_EXPENSIVE_BPS)))
    except (TypeError, ValueError):
        return _DEFAULT_EXPENSIVE_BPS
    if val <= 0:
        return _DEFAULT_EXPENSIVE_BPS
    return val


# Direction labels.
PAYING = "PAYING"
RECEIVING = "RECEIVING"
FLAT = "FLAT"
NA = "N/A"


@dataclass(frozen=True)
class PositionFunding:
    coin: str
    side: str                 # LONG | SHORT
    funding_8h_bps: float | None   # signed HL convention; None when no rate
    cum_funding_usd: float    # FAVORABLE convention: + = received, − = paid
    direction: str            # PAYING | RECEIVING | FLAT | N/A
    paying_bps: float | None  # magnitude PAID per 8h (≥0); 0 when receiving; None if N/A
    zone: str                 # OK | RECV | MONITOR | FLAG | N/A
    expensive_carry: bool     # True only when PAYING ≥ threshold


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
    """Convert an HL hourly funding rate to 8h basis points (signed)."""
    if hourly_rate is None:
        return None
    try:
        return float(hourly_rate) * 8.0 * 10_000.0
    except (TypeError, ValueError):
        return None


def funding_direction(side: str, bps_8h: float | None) -> tuple[str, float | None]:
    """Resolve PAYING / RECEIVING / FLAT and the PAID magnitude (bps/8h).

    Returns ``(direction, paying_bps)`` where ``paying_bps`` is the positive
    amount the position PAYS per 8h (0.0 when receiving / flat, ``None`` when
    the rate is unknown). Direction is derived from side + rate sign per the HL
    convention — NEVER from the rate sign alone.
    """
    if bps_8h is None:
        return NA, None
    s = (side or "").upper()
    if abs(bps_8h) < 1e-9:
        return FLAT, 0.0
    if s == "LONG":
        # rate > 0 ⇒ long pays; rate < 0 ⇒ long receives.
        return (PAYING, bps_8h) if bps_8h > 0 else (RECEIVING, 0.0)
    if s == "SHORT":
        # rate < 0 ⇒ short pays; rate > 0 ⇒ short receives.
        return (PAYING, -bps_8h) if bps_8h < 0 else (RECEIVING, 0.0)
    return NA, None


def evaluate_position_funding(
    position: dict[str, Any],
    hourly_rate: float | None,
    *,
    is_cycle_long: bool = False,  # accepted for back-compat; NOT used to gate
    threshold_bps: float | None = None,
) -> PositionFunding:
    """Build the direction-aware funding read for one position. NEVER raises.

    The expensive-carry flag fires for ANY position (long or short) that is
    PAYING beyond ``threshold_bps`` (default ``FUNDING_EXPENSIVE_BPS_8H`` =
    1.5 bp/8h). ``is_cycle_long`` is accepted only for backward-compatibility
    with older callers — the flag is gated on PAYING-magnitude, not on the
    cycle tag (per R-AUDIT2-P0.1).
    """
    coin = str(position.get("coin") or "?")
    side = str(position.get("side") or "").upper() or "?"
    # Raw HL cumFunding.sinceOpen: positive = PAID, negative = received.
    # Display convention is FAVORABLE (+ = received): negate the raw sign.
    try:
        raw_cum = float(position.get("cum_funding_since_open") or 0.0)
    except (TypeError, ValueError):
        raw_cum = 0.0
    cum_favorable = -raw_cum

    bps = funding_8h_bps(hourly_rate)
    threshold = threshold_bps if threshold_bps is not None else _expensive_threshold_bps()

    if bps is None:
        return PositionFunding(coin, side, None, cum_favorable, NA, None, "N/A", False)

    direction, paying_bps = funding_direction(side, bps)

    if direction == RECEIVING:
        # Favorable income — never flagged, never a monitoring eye.
        return PositionFunding(coin, side, bps, cum_favorable, RECEIVING, 0.0, "RECV", False)
    if direction == FLAT:
        return PositionFunding(coin, side, bps, cum_favorable, FLAT, 0.0, "OK", False)
    if direction == PAYING:
        pay = paying_bps or 0.0
        if pay >= threshold - 1e-9:  # boundary-inclusive (float-safe)
            return PositionFunding(coin, side, bps, cum_favorable, PAYING, pay, "FLAG", True)
        # Paying, but below the expensive threshold → MONITOR (a real, small cost).
        return PositionFunding(coin, side, bps, cum_favorable, PAYING, pay, "MONITOR", False)
    # Unknown side with a known rate → surface rate, no flag.
    return PositionFunding(coin, side, bps, cum_favorable, NA, None, "N/A", False)


def format_funding_line(pf: PositionFunding) -> str:
    """One compact line per position for the report. NEVER raises."""
    if pf.funding_8h_bps is None:
        return (
            f"  • {pf.coin} {pf.side}: funding n/d | "
            f"carry acum {pf.cum_funding_usd:+.2f} USD"
        )
    if pf.direction == RECEIVING:
        dir_txt = "recibiendo funding (favorable)"
    elif pf.direction == PAYING:
        dir_txt = "pagando funding"
    else:
        dir_txt = "funding neutro"
    tag = ""
    if pf.zone == "FLAG":
        tag = "  🚩 carry caro — MANUAL REVIEW (reconsiderar)"
    elif pf.zone == "MONITOR":
        tag = "  👁 monitoreo"
    return (
        f"  • {pf.coin} {pf.side}: {pf.funding_8h_bps:+.2f} bp/8h · {dir_txt} | "
        f"carry acum {pf.cum_funding_usd:+.2f} USD{tag}"
    )


def build_funding_block(
    positions: list[dict[str, Any]] | None,
    rates: dict[str, float] | None,
    cycle_long_coins: set[str] | None = None,  # back-compat; no longer gates
) -> str:
    """Render the per-position funding block. Empty string if no positions.

    ``cycle_long_coins`` is accepted for backward-compatibility but no longer
    gates the expensive-carry flag (R-AUDIT2-P0.1): the flag is direction-aware
    and fires on PAYING-beyond-threshold for any side. NEVER raises.
    """
    positions = positions or []
    rates = rates or {}
    rows: list[str] = []
    flagged = 0
    for p in positions:
        if not isinstance(p, dict):
            continue
        coin = str(p.get("coin") or "?")
        rate = rates.get(coin)
        pf = evaluate_position_funding(p, rate)
        rows.append(format_funding_line(pf))
        if pf.expensive_carry:
            flagged += 1
    if not rows:
        return ""
    head = "💸 FUNDING POR POSICIÓN (8h rate · dirección · carry acumulado desde entry)"
    if flagged:
        head += f" — {flagged} pagando carry caro (MANUAL REVIEW)"
    # R-BOT-DEFINITIVE WI-4: when the shared HL client had to serve the funding
    # source from an expired cache, label it instead of pretending it's live.
    try:
        from modules.hl_client import stale_note
        note = stale_note("metaAndAssetCtxs")
        if note:
            head += f" ({note})"
    except Exception:  # noqa: BLE001
        pass
    return head + "\n" + "\n".join(rows)
