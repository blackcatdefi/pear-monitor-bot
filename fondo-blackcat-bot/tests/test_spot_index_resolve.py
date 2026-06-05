"""P0.1 — spot-index → ticker resolution + regression guard.

Guards against the "coin no identificada / @107" bug: the 4 HYPE spot limit
buys (46/50/55/60) were rendered as the opaque pair index ``@107`` instead
of ``HYPE``. The resolver must map every spot pair index to its base ticker
and leave canonical pairs / plain perp tickers untouched.
"""
from __future__ import annotations

from modules import spot_index


# Minimal real-shape spotMeta slice (mirrors the live HL payload).
SPOT_META = {
    "tokens": [
        {"name": "USDC", "index": 0},
        {"name": "PURR", "index": 1},
        {"name": "HYPE", "index": 150},
        {"name": "FARTCOIN", "index": 42},
    ],
    "universe": [
        {"name": "PURR/USDC", "tokens": [1, 0], "index": 0, "isCanonical": True},
        {"name": "@107", "tokens": [150, 0], "index": 107, "isCanonical": False},
        {"name": "@42", "tokens": [42, 0], "index": 42, "isCanonical": False},
    ],
}


def test_build_map_resolves_at107_to_hype():
    m = spot_index.build_spot_index_map(SPOT_META)
    assert m["@107"] == "HYPE"
    assert m["@42"] == "FARTCOIN"
    assert m["PURR/USDC"] == "PURR"


def test_resolve_spot_coin_with_explicit_map():
    m = spot_index.build_spot_index_map(SPOT_META)
    assert spot_index.resolve_spot_coin("@107", m) == "HYPE"
    # Plain perp ticker is unchanged.
    assert spot_index.resolve_spot_coin("BTC", m) == "BTC"
    # Canonical pair resolves to base ticker.
    assert spot_index.resolve_spot_coin("PURR/USDC", m) == "PURR"


def test_unknown_index_passes_through_not_crash():
    m = spot_index.build_spot_index_map(SPOT_META)
    # An index we have no mapping for must NOT raise and must not invent.
    assert spot_index.resolve_spot_coin("@9999", m) == "@9999"
    assert spot_index.resolve_spot_coin(None, m) == "?"


def test_malformed_spotmeta_is_safe():
    assert spot_index.build_spot_index_map(None) == {}
    assert spot_index.build_spot_index_map({}) == {}
    assert spot_index.build_spot_index_map({"tokens": "x", "universe": 5}) == {}


def test_regression_no_unidentified_spot_coin_in_normalized_orders():
    """The 4 HYPE spot buys must normalise to HYPE, never an @-index string."""
    # Prime the module cache as the live warm-up would.
    spot_index._cache["map"] = spot_index.build_spot_index_map(SPOT_META)
    spot_index._cache["ts"] = 10**12  # far future → never considered stale
    from modules import portfolio

    raw_orders = [
        {"coin": "@107", "side": "B", "limitPx": "46.0", "sz": "108.69", "isTrigger": False},
        {"coin": "@107", "side": "B", "limitPx": "50.0", "sz": "125.0", "isTrigger": False},
        {"coin": "@107", "side": "B", "limitPx": "55.0", "sz": "90.9", "isTrigger": False},
        {"coin": "@107", "side": "B", "limitPx": "60.0", "sz": "62.5", "isTrigger": False},
    ]
    norm = portfolio._normalize_open_orders(raw_orders)
    coins = {o["coin"] for o in norm}
    assert coins == {"HYPE"}, coins
    # GUARD: zero unidentified @-index strings anywhere in the output.
    assert not any(str(o["coin"]).startswith("@") for o in norm)
    # USDC notional sanity (limit_px * size) for the 46-buy.
    o46 = next(o for o in norm if o["limit_px"] == 46.0)
    assert abs(o46["limit_px"] * o46["size"] - 46.0 * 108.69) < 1e-6
