"""R-PM-MARGIN-MODE-FIX (2026-06-07) — per-leg margin-mode awareness.

HyperLiquid HIP-3 lets each perp leg run CROSS or ISOLATED independently. Two
of the fund's equity-basket markets (``xyz:MRVL`` / ``xyz:HOOD``) can ONLY be
traded ISOLATED — HL refuses to switch them to cross — so the live basket is a
MIXED-MARGIN portfolio.

The correct mental model
------------------------
* CROSS legs share the Portfolio-Margin account margin pool with the HYPE spot
  collateral. Their losses consume shared margin and therefore DO move the
  borrow utilisation, the aave-HF, and the HYPE cross liquidation price.
* ISOLATED legs are walled off: each posts its own dedicated margin and carries
  its OWN liquidation price. A loss on an isolated leg can only burn its posted
  isolated margin — it does NOT draw down the shared cross pool, does NOT move
  the HYPE collateral liquidation price, and does NOT change
  ``portfolio_margin_ratio``.

Consequently the cross-pool risk math (cross maintenance margin, borrow
utilisation, head-room, aave-HF, HYPE liq price) must include ONLY the cross
legs. The isolated legs are reported in a SEPARATE subsection, each with its
own isolated margin + isolated liq price + distance-to-liq.

Margin mode is READ LIVE from the HL clearinghouse state (``leverage.type``),
NEVER hardcoded by ticker — HL may flip a market between cross/isolated in the
future, or other legs may become isolated. NEVER raises.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "IsolatedLeg",
    "position_margin_mode",
    "is_isolated",
    "is_cross",
    "position_maint_margin",
    "position_isolated_margin",
    "split_legs",
    "cross_perp_maint_margin",
    "build_isolated_legs",
    "shorts_notional_split",
]


def _f(v: Any) -> float:
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def position_margin_mode(p: dict[str, Any] | None) -> str:
    """Return ``'isolated'`` | ``'cross'`` | ``'unknown'`` for one position.

    Mirrors ``position_classifier`` — reads ``leverage_type`` (HL
    ``leverage.type``) then ``margin_mode``. Substring match so HL variants
    like ``"isolated"``/``"cross"`` (any casing) resolve. NEVER raises.
    """
    if not isinstance(p, dict):
        return "unknown"
    mode = str(p.get("leverage_type") or p.get("margin_mode") or "").lower()
    if "iso" in mode:
        return "isolated"
    if "cross" in mode:
        return "cross"
    return "unknown"


def is_isolated(p: dict[str, Any] | None) -> bool:
    """True only when the leg is explicitly ISOLATED. NEVER raises."""
    return position_margin_mode(p) == "isolated"


def is_cross(p: dict[str, Any] | None) -> bool:
    """True when the leg shares the cross pool.

    Unknown margin mode is treated as CROSS: excluding an unclassified leg from
    the shared-pool maintenance requirement would UNDER-state pool risk, so the
    conservative default folds it into the cross math. NEVER raises.
    """
    return position_margin_mode(p) != "isolated"


def position_maint_margin(p: dict[str, Any] | None) -> float:
    """Maintenance margin a leg contributes to ITS margin pool.

    Priority (most to least authoritative):
      1. explicit ``maint_margin`` / ``maintenance_margin`` /
         ``maintenance_margin_used`` field,
      2. notional × (0.5 / max_leverage)  — HL maintenance-margin rate is
         ``0.5 / maxLeverage``,
      3. ``margin_used`` / ``marginUsed`` (INITIAL margin — a conservative
         over-estimate of maintenance),
      4. 0.0.
    NEVER raises.
    """
    if not isinstance(p, dict):
        return 0.0
    for k in ("maint_margin", "maintenance_margin", "maintenance_margin_used"):
        v = _f(p.get(k))
        if v > 0:
            return v
    ntl = abs(_f(p.get("notional_usd") or p.get("positionValue")))
    maxlev = _f(p.get("max_leverage") or p.get("maxLeverage"))
    if ntl > 0 and maxlev > 0:
        return ntl * (0.5 / maxlev)
    mu = _f(p.get("margin_used") or p.get("marginUsed"))
    if mu > 0:
        return mu
    return 0.0


def position_isolated_margin(p: dict[str, Any] | None) -> float:
    """Posted isolated margin for an isolated leg.

    HL exposes this as ``marginUsed`` on an isolated position (the walled-off
    collateral). Prefer the explicit ``isolated_margin`` field, then
    ``margin_used``/``marginUsed``, then the maintenance estimate. NEVER raises.
    """
    if not isinstance(p, dict):
        return 0.0
    for k in ("isolated_margin", "margin_used", "marginUsed"):
        v = _f(p.get(k))
        if v > 0:
            return v
    return position_maint_margin(p)


def _mark_px(p: dict[str, Any], prices: dict[str, float] | None) -> float:
    """Best-effort mark price for a leg. NEVER raises."""
    mk = _f(p.get("mark_px") or p.get("markPx"))
    if mk > 0:
        return mk
    size = abs(_f(p.get("size") or p.get("szi")))
    ntl = abs(_f(p.get("notional_usd") or p.get("positionValue")))
    if size > 0 and ntl > 0:
        return ntl / size
    coin = str(p.get("coin") or "").upper()
    if prices:
        px = prices.get(coin) or prices.get(coin.split(":")[-1])
        if px:
            return _f(px)
    return _f(p.get("entry_px") or p.get("entryPx"))


def split_legs(
    positions: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Partition positions into ``(cross_legs, isolated_legs)``.

    A flat (size 0) leg is dropped. Unknown margin mode → cross (conservative).
    NEVER raises.
    """
    cross: list[dict[str, Any]] = []
    isolated: list[dict[str, Any]] = []
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        if abs(_f(p.get("size") or p.get("szi"))) <= 0 and \
                abs(_f(p.get("notional_usd") or p.get("positionValue"))) <= 0:
            continue
        (isolated if is_isolated(p) else cross).append(p)
    return cross, isolated


def cross_perp_maint_margin(positions: list[dict[str, Any]] | None) -> float:
    """Σ maintenance margin of CROSS legs only (isolated walled off).

    This is the perp contribution to the shared PM liability. Returns 0.0 when
    no cross leg carries maintenance data (keeps fieldless synthetic positions
    backward-compatible). NEVER raises.
    """
    cross, _iso = split_legs(positions)
    return sum(position_maint_margin(p) for p in cross)


def build_isolated_legs(
    positions: list[dict[str, Any]] | None,
    prices: dict[str, float] | None = None,
) -> list["IsolatedLeg"]:
    """Build the ISOLATED-leg report structs (walled off from the cross pool).

    Each carries notional, entry, mark, posted isolated margin, the leg's OWN
    isolated liquidation price, distance-to-liq %, and UPnL. NEVER raises.
    """
    _cross, isolated = split_legs(positions)
    out: list[IsolatedLeg] = []
    for p in isolated:
        coin = str(p.get("coin") or "?")
        size = _f(p.get("size") or p.get("szi"))
        side = str(p.get("side") or ("LONG" if size > 0 else "SHORT")).upper()
        ntl = abs(_f(p.get("notional_usd") or p.get("positionValue")))
        entry = _f(p.get("entry_px") or p.get("entryPx"))
        mark = _mark_px(p, prices)
        iso_margin = position_isolated_margin(p)
        liq = _f(p.get("liq_px") or p.get("liquidationPx"))
        upnl = _f(p.get("unrealized_pnl") or p.get("unrealizedPnl"))
        dist = 0.0
        if liq > 0 and mark > 0:
            dist = abs(mark - liq) / mark * 100.0
        out.append(
            IsolatedLeg(
                coin=coin,
                side=side,
                size=size,
                notional_usd=ntl,
                entry_px=entry,
                mark_px=mark,
                isolated_margin=iso_margin,
                liq_px=liq,
                distance_to_liq_pct=dist,
                upnl=upnl,
            )
        )
    return out


def shorts_notional_split(
    positions: list[dict[str, Any]] | None,
) -> tuple[float, float]:
    """Return ``(cross_short_notional, isolated_short_notional)``.

    The basket is the directional hedge; total short notional = the sum of
    both. This split lets the hedge framing annotate which portion touches the
    cross pool (cross) vs which is walled off (isolated). NEVER raises.
    """
    cross_short = 0.0
    iso_short = 0.0
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        size = _f(p.get("size") or p.get("szi"))
        if size >= 0:
            continue
        ntl = abs(_f(p.get("notional_usd") or p.get("positionValue")))
        if is_isolated(p):
            iso_short += ntl
        else:
            cross_short += ntl
    return cross_short, iso_short


@dataclass(frozen=True)
class IsolatedLeg:
    """One ISOLATED perp leg — margin walled off from the cross PM pool."""

    coin: str
    side: str
    size: float
    notional_usd: float
    entry_px: float
    mark_px: float
    isolated_margin: float
    liq_px: float
    distance_to_liq_pct: float
    upnl: float
