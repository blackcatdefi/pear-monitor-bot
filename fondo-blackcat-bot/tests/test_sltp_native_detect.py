"""R-SLTP-NATIVE-DETECT (2026-06-09) — tests A1-A6.

THE LIVE BUG (2026-06-09 20:43 UTC /reporte): every xyz: basket leg showed
SL/TP=no and the FULL ANALYSIS escalated "SIN SL / ACCIÓN URGENTE" on
xyz:HOOD — while the user had 100% native HL stop-loss + take-profit trigger
orders loaded on ALL legs. Root cause (confirmed live against the HL API):
``frontendOpenOrders`` WITHOUT the ``dex`` param returns ONLY main-dex orders
(BTC/SOL); the 12 xyz: triggers require ``{"dex": "xyz"}``. Plus the
position↔order match was exact-string, fragile to any HIP-3 form variant.

A1 was verified RED against HEAD 911a439 (PositionTag had no ``has_sl`` and
the per-dex fetch didn't exist) and is GREEN after the fix.
"""
from __future__ import annotations

import asyncio

import pytest

from modules.asset_norm import normalize_asset, same_asset
from modules.portfolio import _normalize_open_orders
from modules.position_classifier import (
    build_classification_block,
    classify_position,
    classify_portfolio,
)


# ── Raw HL fixtures (shape captured LIVE from frontendOpenOrders 2026-06-09) ──
def _raw_stop(coin: str, side: str = "B", trig: float = 99.585) -> dict:
    return {
        "coin": coin, "side": side, "limitPx": "0.0", "sz": "95.389",
        "isTrigger": True, "triggerPx": str(trig), "reduceOnly": True,
        "isPositionTpsl": True, "orderType": "Stop Market",
        "triggerCondition": "Price above " + str(trig), "tpsl": "sl",
    }


def _raw_tp(coin: str, side: str = "B", trig: float = 66.39) -> dict:
    return {
        "coin": coin, "side": side, "limitPx": "0.0", "sz": "95.389",
        "isTrigger": True, "triggerPx": str(trig), "reduceOnly": True,
        "isPositionTpsl": True, "orderType": "Take Profit Market",
        "triggerCondition": "Price below " + str(trig), "tpsl": "tp",
    }


def _short_position(coin: str = "xyz:HOOD") -> dict:
    return {
        "coin": coin, "size": -95.389, "side": "SHORT",
        "leverage_type": "cross", "entry_px": 85.0,
        "notional_usd": 8000.0, "liq_px": 150.0,
    }


# ─── A1: trigger order detected → has_sl=True (RED before this round) ────────
def test_a1_short_hood_buy_stop_above_mark_has_sl():
    orders = _normalize_open_orders([_raw_stop("xyz:HOOD", "B", 99.585)])
    tag = classify_position(_short_position("xyz:HOOD"), orders, mark_px=83.0)
    assert tag.has_sl is True
    assert tag.sl_px == pytest.approx(99.585)
    assert tag.has_sl_tp is True


# ─── A2: asset-identity normalization — every coin form matches ───────────────
@pytest.mark.parametrize("order_coin", ["xyz:HOOD", "HOOD", "XYZ:HOOD", " xyz:hood "])
def test_a2_normalize_asset_variants_match(order_coin):
    orders = _normalize_open_orders([_raw_stop(order_coin, "B", 99.585)])
    tag = classify_position(_short_position("xyz:HOOD"), orders, mark_px=83.0)
    assert tag.has_sl is True, f"variant {order_coin!r} must match xyz:HOOD"


def test_a2_normalize_asset_helper_generic():
    assert normalize_asset("xyz:HOOD") == "HOOD"
    assert normalize_asset("HOOD") == "HOOD"
    assert normalize_asset("builder:xyz:HOOD") == "HOOD"
    assert normalize_asset("  abcd:foo ") == "FOO"      # generalizes, no hardcoded tickers
    assert normalize_asset("@107") == "@107"            # spot-index form untouched
    assert normalize_asset(None) == ""
    assert same_asset("xyz:HOOD", "HOOD")
    assert not same_asset("xyz:HOOD", "xyz:MU")


# ─── A3: take-profit trigger detected ─────────────────────────────────────────
def test_a3_take_profit_detected():
    orders = _normalize_open_orders([_raw_tp("xyz:HOOD", "B", 66.39)])
    tag = classify_position(_short_position("xyz:HOOD"), orders, mark_px=83.0)
    assert tag.has_tp is True
    assert tag.tp_px == pytest.approx(66.39)
    assert tag.has_sl is False


# ─── A4: no false positives ───────────────────────────────────────────────────
def test_a4_different_asset_does_not_mark():
    orders = _normalize_open_orders([_raw_stop("xyz:MU", "B", 1106.3)])
    tag = classify_position(_short_position("xyz:HOOD"), orders, mark_px=83.0)
    assert tag.has_sl is False
    assert tag.has_tp is False


def test_a4_non_reduce_only_entry_trigger_not_protective():
    raw = _raw_stop("xyz:HOOD", "B", 99.585)
    raw["reduceOnly"] = False
    raw["isPositionTpsl"] = False
    raw["tpsl"] = ""
    orders = _normalize_open_orders([raw])
    tag = classify_position(_short_position("xyz:HOOD"), orders, mark_px=83.0)
    assert tag.has_sl is False


def test_a4_wrong_direction_stop_not_protective():
    # A SELL stop on a SHORT is not protective (would ADD to the short).
    orders = _normalize_open_orders([_raw_stop("xyz:HOOD", "A", 70.0)])
    tag = classify_position(_short_position("xyz:HOOD"), orders, mark_px=83.0)
    assert tag.has_sl is False


# ─── A5: FULL ANALYSIS context — false-alarm suppressed, genuine warn kept ───
def _basket_wallet(legs_with_sl: bool, extra_naked_leg: bool = False) -> list[dict]:
    coins = ["xyz:HOOD", "xyz:MRVL", "xyz:XYZ100", "xyz:NVDA", "xyz:MU", "xyz:SP500"]
    raw_orders = []
    if legs_with_sl:
        for c in coins:
            raw_orders.append(_raw_stop(c, "B", 999.0))
            raw_orders.append(_raw_tp(c, "B", 1.0))
    positions = [_short_position(c) for c in coins]
    if extra_naked_leg:
        positions.append(_short_position("xyz:NAKED"))
    return [{
        "label": "Trading", "status": "ok",
        "data": {
            "positions": positions,
            "open_orders": _normalize_open_orders(raw_orders),
        },
    }]


def test_a5_no_false_sin_sl_when_all_legs_protected():
    from templates.formatters import compile_raw_data
    portfolio = _basket_wallet(legs_with_sl=True)
    tags = classify_portfolio(portfolio, None)
    assert tags and all(t.has_sl for t in tags)
    block = build_classification_block(tags)
    ctx = compile_raw_data(portfolio, None, None, None, None)
    for forbidden in ("SIN SL", "posición más vulnerable", "ACCIÓN URGENTE"):
        assert forbidden not in block
        assert forbidden not in ctx
    assert "SL/TP=sí" in block


def test_a5_genuine_no_sl_warning_still_fires():
    portfolio = _basket_wallet(legs_with_sl=True, extra_naked_leg=True)
    tags = classify_portfolio(portfolio, None)
    block = build_classification_block(tags)
    assert "SIN SL" in block  # the synthetic naked leg keeps the guard alive
    naked = [t for t in tags if t.coin == "xyz:NAKED"][0]
    assert naked.has_sl is False


def test_a5_system_prompt_prohibits_reinventing_sin_sl():
    from templates.system_prompt import SYSTEM_PROMPT
    assert "R-SLTP-NATIVE-DETECT" in SYSTEM_PROMPT
    assert "PROHIBIDO afirmar \"SIN SL\"" in SYSTEM_PROMPT


# ─── A6: BTC/SOL (main dex) unchanged — SL/TP still detected ─────────────────
def test_a6_btc_sol_main_dex_tp_still_detected():
    # Live main-dex shape 2026-06-09: BTC/SOL carry Take Profit Market triggers.
    raw = [
        {"coin": "BTC", "side": "A", "limitPx": "0.0", "sz": "1.42",
         "isTrigger": True, "triggerPx": "186666.0", "reduceOnly": True,
         "isPositionTpsl": True, "orderType": "Take Profit Market",
         "triggerCondition": "Price above 186666", "tpsl": "tp"},
        {"coin": "SOL", "side": "A", "limitPx": "0.0", "sz": "298.6",
         "isTrigger": True, "triggerPx": "566.0", "reduceOnly": True,
         "isPositionTpsl": True, "orderType": "Take Profit Market",
         "triggerCondition": "Price above 566", "tpsl": "tp"},
    ]
    orders = _normalize_open_orders(raw)
    btc = classify_position(
        {"coin": "BTC", "size": 1.42, "side": "LONG", "leverage_type": "isolated"},
        orders, mark_px=100000.0,
    )
    assert btc.has_sl_tp is True
    assert btc.has_tp is True
    sol = classify_position(
        {"coin": "SOL", "size": 298.6, "side": "LONG", "leverage_type": "isolated"},
        orders, mark_px=200.0,
    )
    assert sol.has_sl_tp is True and sol.has_tp is True


# ─── Root-cause guard: orders are fetched for EVERY dex (main + HIP-3) ───────
def test_fetch_all_open_orders_queries_every_dex(monkeypatch):
    from modules import portfolio as pf
    from config import HIP3_DEXES

    seen_payloads: list[dict] = []

    async def fake_info(payload):
        seen_payloads.append(payload)
        if payload.get("dex") == "xyz":
            return [_raw_stop("xyz:HOOD", "B", 99.585)]
        if "dex" not in payload:
            return [_raw_tp("BTC", "A", 186666.0)]
        return []

    monkeypatch.setattr(pf, "_info", fake_info)
    merged = asyncio.run(
        pf.fetch_all_open_orders("0xc7ae23316b47f7e75f455f53ad37873a18351505")
    )
    dexes_queried = {p.get("dex") for p in seen_payloads}
    # main (no dex) + every HIP-3 dex, xyz included — the missing query that
    # made every xyz: SL/TP invisible before this round.
    assert None in dexes_queried
    for d in HIP3_DEXES:
        assert d in dexes_queried
    coins = {o.get("coin") for o in merged}
    assert {"xyz:HOOD", "BTC"} <= coins
