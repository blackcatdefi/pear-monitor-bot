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

# ── R-FUNDING-TRUTH (2026-06-15) — single source of truth for the LLM ─────────
# Canonical verdict direction labels (the strings the FULL ANALYSIS narrative
# and the funding_por_posicion block BOTH consume). Distinct from the internal
# PAYING/RECEIVING/FLAT zone labels above so callers can't accidentally cross
# the two vocabularies.
PAYS = "PAYS"
RECEIVES = "RECEIVES"
NEUTRAL = "NEUTRAL"
NA_VERDICT = "NA"


@dataclass(frozen=True)
class FundingVerdict:
    """Authoritative funding read for ONE position — the single source of truth.

    Both ``evaluate_position_funding`` (which drives the funding_por_posicion
    block) and ``build_funding_llm_block`` (which drives the FULL ANALYSIS LLM
    context) derive their direction + expensive-carry decision from this exact
    struct, so the two surfaces can never disagree again (the 2026-06-15 bug:
    funding_por_posicion said "pagando funding" while the LLM narrative
    re-derived direction itself and said "COBRA / no es carry caro").

    ``display_string`` is the EXACT phrase the LLM must verbalize. It always
    contains the literal token "PAGA" when the position PAYS and "RECIBE" when
    it RECEIVES, so the model never has to (and is forbidden to) infer direction
    from the rate sign on its own.
    """
    direction: str            # PAYS | RECEIVES | NEUTRAL | NA
    is_expensive_carry: bool
    display_string: str
    paying_bps: float | None  # magnitude PAID per 8h (≥0); 0 when receiving/flat; None if unknown


def funding_verdict(
    side: str,
    funding_rate: float | None,
    carry_accrued: float | None = None,
    *,
    threshold_bps: float | None = None,
    coin: str | None = None,
) -> FundingVerdict:
    """Deterministic single-source-of-truth funding verdict. NEVER raises.

    Ground truth (perpetual swaps, HL convention):
        funding rate > 0  ⇒ LONGS pay shorts.
        funding rate < 0  ⇒ SHORTS pay longs.
    Combined with the position side:
        LONG  + rate > 0 → PAYS     · LONG  + rate < 0 → RECEIVES
        SHORT + rate > 0 → RECEIVES · SHORT + rate < 0 → PAYS
    A rate of EXACTLY 0 → NEUTRAL (no cashflow, never expensive).

    Args:
        side: "LONG" | "SHORT".
        funding_rate: signed funding rate in **bp/8h** (HL convention). ``None``
            when no live rate is available — direction then falls back to the
            realized-carry sign (ground truth).
        carry_accrued: realized cumulative funding in the FAVORABLE display
            convention (``+`` = net received, ``−`` = net paid). Used both as the
            fallback when the rate is missing AND as a sanity check on the
            rate-derived direction.
        threshold_bps: expensive-carry threshold (positive bp/8h magnitude);
            defaults to ``FUNDING_EXPENSIVE_BPS_8H`` (1.5).
        coin: optional, for log context only.

    Sanity check (realized cashflow is ground truth over the instantaneous rate):
        if the rate-derived direction is PAYS/RECEIVES but the realized carry
        sign says the opposite, TRUST THE CARRY SIGN and log a warning. A
        negative carry means net PAID ⇒ direction PAYS.
    """
    s = (side or "").upper()
    threshold = threshold_bps if threshold_bps is not None else _expensive_threshold_bps()

    # ── rate-implied direction + paid magnitude ──
    rate_dir: str | None = None
    paying_bps: float | None = None
    r: float | None = None
    if funding_rate is not None:
        try:
            r = float(funding_rate)
        except (TypeError, ValueError):
            r = None
    if r is not None:
        if abs(r) < 1e-9:
            rate_dir, paying_bps = NEUTRAL, 0.0
        elif s == "LONG":
            rate_dir, paying_bps = (PAYS, r) if r > 0 else (RECEIVES, 0.0)
        elif s == "SHORT":
            rate_dir, paying_bps = (PAYS, -r) if r < 0 else (RECEIVES, 0.0)

    # ── carry-implied direction (favorable convention: + received, − paid) ──
    carry_dir: str | None = None
    c: float | None = None
    if carry_accrued is not None:
        try:
            c = float(carry_accrued)
        except (TypeError, ValueError):
            c = None
    if c is not None and abs(c) >= 0.01:  # ≥ 1¢ realized → a clear sign
        carry_dir = RECEIVES if c > 0 else PAYS

    # ── reconcile (realized cashflow wins over instantaneous rate) ──
    direction = rate_dir
    if direction is None:
        # No live rate → fall back to realized carry sign (ground truth).
        direction = carry_dir if carry_dir is not None else NA_VERDICT
    elif direction in (PAYS, RECEIVES) and carry_dir is not None and carry_dir != direction:
        log.warning(
            "funding_verdict sign disagreement coin=%s side=%s rate→%s carry=%.4f→%s "
            "— trusting realized carry sign (ground truth)",
            coin, s, direction, c, carry_dir,
        )
        direction = carry_dir
        paying_bps = None  # rate magnitude no longer trustworthy

    # ── expensive-carry: ONLY when PAYS and the paid magnitude clears threshold ──
    is_expensive = bool(
        direction == PAYS
        and paying_bps is not None
        and paying_bps >= threshold - 1e-9
    )

    # ── display string (the LLM must verbalize this VERBATIM) ──
    if direction == PAYS:
        mag = f" ({paying_bps:+.2f} bp/8h pagados)" if paying_bps is not None else ""
        flag = " 🚩 CARRY CARO — MANUAL REVIEW" if is_expensive else ""
        display = f"PAGA funding (costo){mag}{flag}"
    elif direction == RECEIVES:
        display = "RECIBE funding (favorable)"
    elif direction == NEUTRAL:
        display = "funding NEUTRO (~0, sin cashflow)"
    else:
        display = "funding n/d (sin dato)"

    return FundingVerdict(direction, is_expensive, display, paying_bps)


@dataclass(frozen=True)
class PositionFunding:
    coin: str
    side: str                 # LONG | SHORT
    funding_8h_bps: float | None   # signed HL convention; None when no rate
    cum_funding_usd: float | None  # FAVORABLE convention: + recv, − paid; None = n/d (missing)
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
    # FIX 2 (never fabricate a value): a genuinely-missing carry (``None`` from
    # the portfolio layer when the cumFunding block was absent) stays ``None`` so
    # it renders "n/d" — NOT a confident "+0.00 USD". A real 0.0 stays 0.0.
    _raw = position.get("cum_funding_since_open")
    if _raw is None:
        cum_favorable = None
    else:
        try:
            cum_favorable = -float(_raw) + 0.0  # +0.0 normalizes -0.0
        except (TypeError, ValueError):
            cum_favorable = None

    bps = funding_8h_bps(hourly_rate)
    threshold = threshold_bps if threshold_bps is not None else _expensive_threshold_bps()

    if bps is None:
        return PositionFunding(coin, side, None, cum_favorable, NA, None, "N/A", False)

    # R-FUNDING-TRUTH (2026-06-15): route the direction + expensive-carry
    # decision through the canonical ``funding_verdict`` so this block (which
    # feeds funding_por_posicion) and the FULL ANALYSIS LLM context can never
    # disagree. The internal PAYING/RECEIVING/FLAT zone vocabulary is preserved
    # for the existing renderer; it is a pure mapping off the verdict.
    verdict = funding_verdict(
        side, bps, cum_favorable, threshold_bps=threshold, coin=coin
    )

    if verdict.direction == RECEIVES:
        # Favorable income — never flagged, never a monitoring eye.
        return PositionFunding(coin, side, bps, cum_favorable, RECEIVING, 0.0, "RECV", False)
    if verdict.direction == NEUTRAL:
        return PositionFunding(coin, side, bps, cum_favorable, FLAT, 0.0, "OK", False)
    if verdict.direction == PAYS:
        pay = verdict.paying_bps or 0.0
        if verdict.is_expensive_carry:
            return PositionFunding(coin, side, bps, cum_favorable, PAYING, pay, "FLAG", True)
        # Paying, but below the expensive threshold → MONITOR (a real, small cost).
        return PositionFunding(coin, side, bps, cum_favorable, PAYING, pay, "MONITOR", False)
    # Unknown side with a known rate → surface rate, no flag.
    return PositionFunding(coin, side, bps, cum_favorable, NA, None, "N/A", False)


def _carry_txt(cum: float | None) -> str:
    """FIX 2: render the cumulative carry, or 'n/d' when it is genuinely missing."""
    return "carry acum n/d" if cum is None else f"carry acum {cum:+.2f} USD"


def format_funding_line(pf: PositionFunding) -> str:
    """One compact line per position for the report. NEVER raises."""
    if pf.funding_8h_bps is None:
        return f"  • {pf.coin} {pf.side}: funding n/d | {_carry_txt(pf.cum_funding_usd)}"
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
        f"{_carry_txt(pf.cum_funding_usd)}{tag}"
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


def build_funding_llm_block(
    positions: list[dict[str, Any]] | None,
    rates: dict[str, float] | None,
) -> str:
    """Authoritative PRE-COMPUTED funding block for the FULL ANALYSIS LLM context.

    R-FUNDING-TRUTH (2026-06-15). The 2026-06-15 bug: the LLM received only raw
    portfolio JSON and re-derived funding DIRECTION on its own — getting it
    backwards (narrating a BTC LONG paying positive funding as "COBRA / no es
    carry caro"). Same failure class as the PM-math bug solved by pm_context.py.

    The fix mirrors pm_context exactly: compute the verdict ONCE via the
    canonical ``funding_verdict`` (the SAME function the funding_por_posicion
    block consumes) and inject the precomputed ``display_string`` per position.
    The model is instructed to verbalize it VERBATIM and is FORBIDDEN from
    inferring cobra/paga from the rate sign itself.

    Returns "" when there are no positions (injects nothing). NEVER raises.
    """
    positions = positions or []
    rates = rates or {}
    rows: list[str] = []
    paying = 0
    for p in positions:
        if not isinstance(p, dict):
            continue
        coin = str(p.get("coin") or "?")
        side = str(p.get("side") or "").upper() or "?"
        rate = rates.get(coin)
        bps = funding_8h_bps(rate)
        # FIX 2: missing carry → None → "n/d", never a fabricated +0.00.
        _raw = p.get("cum_funding_since_open")
        if _raw is None:
            cum_favorable = None
        else:
            try:
                cum_favorable = -float(_raw) + 0.0  # +0.0 normalizes -0.0
            except (TypeError, ValueError):
                cum_favorable = None
        verdict = funding_verdict(side, bps, cum_favorable, coin=coin)
        if verdict.direction == PAYS:
            paying += 1
        if cum_favorable is None:
            carry_txt = "carry acumulado n/d (sin dato)"
        else:
            carry_txt = (
                f"carry acumulado {cum_favorable:+.2f} USD "
                f"({'costo' if cum_favorable < 0 else 'favorable'})"
            )
        rows.append(f"  • {coin} {side}: {verdict.display_string} | {carry_txt}")

    if not rows:
        return ""

    lines: list[str] = []
    lines.append(
        "═══════ FUNDING POR POSICIÓN — VEREDICTO PRE-CALCULADO (AUTORITATIVO) ═══════"
    )
    lines.append(
        "La DIRECCIÓN de funding de cada posición YA está calculada por el motor "
        "del fondo (modules.funding_tracker.funding_verdict, la MISMA fuente que "
        "el bloque funding_por_posición). Verbalizá el veredicto de cada línea "
        "VERBATIM. PROHIBIDO inferir 'cobra'/'paga' a partir del signo del rate "
        "por tu cuenta: la dirección depende del signo del rate Y del lado "
        "(LONG con rate>0 PAGA; SHORT con rate<0 PAGA; LONG con rate<0 RECIBE; "
        "SHORT con rate>0 RECIBE). El cashflow realizado (carry acumulado) es la "
        "verdad sobre el rate instantáneo. La señal '🚩 CARRY CARO' marca review "
        "manual SOLO cuando la posición PAGA por encima del umbral; una posición "
        "que RECIBE NUNCA es carry caro. Si una línea dice 'n/d', escribí 'n/d'."
    )
    if paying:
        lines.append(f"({paying} posición(es) PAGANDO funding)")
    lines.append("")
    lines.extend(rows)
    # WI-4 parity: label stale funding source instead of pretending it's live.
    try:
        from modules.hl_client import stale_note
        note = stale_note("metaAndAssetCtxs")
        if note:
            lines.append(f"(fuente funding: {note})")
    except Exception:  # noqa: BLE001
        pass
    lines.append("═══════ FIN FUNDING PRE-CALCULADO ═══════")
    return "\n".join(lines)
