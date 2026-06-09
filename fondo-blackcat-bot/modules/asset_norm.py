"""R-SLTP-NATIVE-DETECT (2026-06-09) — canonical asset-identity normalisation.

WHY THIS EXISTS
    HyperLiquid HIP-3 / builder-deployed perps surface the SAME asset under
    several string forms depending on the endpoint and dex context:

        "xyz:HOOD"   — dex-qualified form (clearinghouseState dex=xyz,
                       frontendOpenOrders dex=xyz)
        "HOOD"       — bare ticker (some UI surfaces / future endpoints)
        "XYZ:HOOD"   — case variants
        "builder:xyz:HOOD" — hypothetical deeper-qualified forms

    The 2026-06-09 live bug: the position classifier matched open orders to
    positions by EXACT coin string equality. The orders for the xyz: basket
    legs were never fetched (frontendOpenOrders without ``dex`` only returns
    the MAIN dex) and, even when present, any form mismatch would silently
    fail the match → every xyz: leg showed SL/TP=no and the FULL ANALYSIS
    escalated a false "SIN SL / ACCIÓN URGENTE" on legs that carried 100%
    native stop-loss + take-profit triggers.

    ``normalize_asset()`` is the SINGLE helper both sides of any
    position↔order match must go through. It is deliberately generic — it
    never hardcodes tickers, so any future HIP-3 leg (abcd:FOO, km:BAR…)
    matches automatically.
"""
from __future__ import annotations

from typing import Any

__all__ = ["normalize_asset", "same_asset"]


def normalize_asset(coin: Any) -> str:
    """Canonical UPPER ticker for any HL asset-identity string form.

    Rules (generic, never per-ticker):
      * None / non-string → "" (never raises).
      * strip whitespace, uppercase.
      * dex/builder-qualified colon forms keep ONLY the last segment:
        "xyz:HOOD" → "HOOD"; "builder:xyz:HOOD" → "HOOD".
      * spot-index forms ("@107") are returned as-is (upper) — resolution of
        @N → ticker is spot_index's job, not ours; we must not corrupt it.
    """
    if coin is None:
        return ""
    s = str(coin).strip().upper()
    if not s:
        return ""
    if s.startswith("@"):
        return s
    if ":" in s:
        tail = s.rsplit(":", 1)[-1].strip()
        return tail or s
    return s


def same_asset(a: Any, b: Any) -> bool:
    """True when two asset-identity strings refer to the same asset."""
    na, nb = normalize_asset(a), normalize_asset(b)
    return bool(na) and na == nb
