"""R-FUNDFIX (1 may 2026) — Cosmetic basket label inference.

Pure metadata: maps the active SHORT-coin set to a human-readable basket
id (v4 / v5 / v6). NOT consumed by the LLM — only used for Telegram
message labels (e.g. /reporte "POSICIONES" block, /flywheel header).

Why a separate module?
----------------------
The LLM analyzer must NOT see the historical basket schedule. If it
does, it ends up correlating "the bot says v6 = DYDX/OP/ARB/PYTH/ENA"
with stale legacy strings ("v5 pending capital") and asks BCD to
confirm. Keep this strictly cosmetic and never inject it into the
prompt context.

Public API
----------
- BASKET_LABELS: dict[frozenset[str], str]
- infer_basket_label(positions: list[dict] | list[str]) -> str
- infer_basket_label_from_coins(coins: Iterable[str]) -> str
"""
from __future__ import annotations

from typing import Iterable

# Historical basket compositions (SHORT side). The frozenset ensures
# order-independent matching. Add new entries as new baskets deploy.
BASKET_LABELS: dict[frozenset[str], str] = {
    # v6 — deployed 29 abr 2026 21:45 UTC after TWAP completion
    frozenset({"DYDX", "OP", "ARB", "PYTH", "ENA"}): "v6",
    # v5 — never deployed in production; left here for completeness so a
    # future replay of the v5 universe is labelled correctly. Composition
    # was speculative; if v5 ever ships we'll update this entry.
    frozenset({"WLD", "STRK", "ZRO", "AVAX", "ENA"}): "v4/v5",
    frozenset({"BLUR", "CRV", "LDO", "DYDX", "ARB", "OP"}): "v5",
}


def _to_coin_set(positions) -> frozenset[str]:
    """Normalize either a list of position dicts or a list of strings."""
    out: set[str] = set()
    for p in positions or []:
        if isinstance(p, dict):
            coin = p.get("coin")
        else:
            coin = p
        if not coin:
            continue
        out.add(str(coin).strip().upper())
    return frozenset(out)


def infer_basket_label_from_coins(coins: Iterable[str]) -> str:
    """Return a basket label for an explicit coin iterable."""
    coin_set = frozenset(str(c).strip().upper() for c in coins if c)
    if not coin_set:
        return "unknown"

    # Exact match wins
    label = BASKET_LABELS.get(coin_set)
    if label:
        return label

    # Subset match: if every coin in the active set belongs to a known
    # basket universe (and at least 80% of that basket is present), call
    # it that basket. Handles the case where 1 leg of v6 was closed
    # individually but the rest is alive.
    best_label: str | None = None
    best_overlap = 0.0
    for known_set, known_label in BASKET_LABELS.items():
        if not coin_set.issubset(known_set | coin_set):
            continue
        overlap_count = len(coin_set & known_set)
        if not known_set:
            continue
        overlap_ratio = overlap_count / len(known_set)
        if overlap_ratio >= 0.8 and overlap_count > best_overlap:
            best_overlap = overlap_count
            best_label = known_label
    if best_label:
        return best_label

    return "unknown"


def infer_basket_label(positions) -> str:
    """Infer a basket label from a list of positions (dicts or coin strs).

    Uses SHORT-side coins only when positions carry a ``side`` field. Falls
    back to all coins if no side info is available.
    """
    shorts: list[str] = []
    fallback: list[str] = []
    for p in positions or []:
        if isinstance(p, dict):
            coin = (p.get("coin") or "").strip().upper()
            side = (p.get("side") or "").strip().upper()
            if not coin:
                continue
            fallback.append(coin)
            if side == "SHORT":
                shorts.append(coin)
        else:
            fallback.append(str(p).strip().upper())
    coins = shorts or fallback
    return infer_basket_label_from_coins(coins)
