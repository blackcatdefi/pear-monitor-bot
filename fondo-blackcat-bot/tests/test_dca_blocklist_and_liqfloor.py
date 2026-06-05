"""R-AUDIT2-P1.4 / P1.5 — ZEC cycle/DCA blocklist + structural liq floor.

P1.4: ZEC (and any CYCLE_DCA_BLOCKLIST ticker) is NEVER tagged cycle/DCA in
the position classifier or treated as a LONG candidate in the screener.
P1.5: a laddered isolated position surfaces the post-fill structural liq floor.
"""
from __future__ import annotations

import dataclasses

from modules.position_classifier import (
    classify_position,
    build_classification_block,
    CYCLE,
    TACTICAL,
)


def _ladder_orders(coin, pxs, side="BUY"):
    return [
        {"coin": coin, "side": side, "limit_px": px, "is_trigger": False,
         "reduce_only": False, "is_sl_tp": False}
        for px in pxs
    ]


# ── P1.4: ZEC is forced TACTICAL even with a perfect cycle structure ─────────
def test_zec_never_cycle_even_with_ladder():
    pos = {"coin": "ZEC", "side": "LONG", "size": 10, "entry_px": 40.0,
           "notional_usd": 400, "leverage_type": "isolated", "liq_px": 30.0}
    orders = _ladder_orders("ZEC", [38, 36, 34])
    tag = classify_position(pos, orders, mark_px=39.0, dca_blocked=True)
    assert tag.bucket == TACTICAL
    assert "blocklist" in tag.tag_es.lower()
    assert any("blocklist" in f.lower() for f in tag.flags)


def test_non_blocklisted_isolated_ladder_is_cycle():
    pos = {"coin": "BTC", "side": "LONG", "size": 1, "entry_px": 100000.0,
           "notional_usd": 100000, "leverage_type": "isolated", "liq_px": 80000.0}
    orders = _ladder_orders("BTC", [98000, 96000])
    tag = classify_position(pos, orders, mark_px=99000.0, dca_blocked=False)
    assert tag.bucket == CYCLE


def test_classify_portfolio_blocks_zec(monkeypatch):
    from modules import position_classifier as pc
    portfolio = [{
        "status": "ok",
        "data": {
            "open_orders": _ladder_orders("ZEC", [38, 36]),
            "positions": [{"coin": "ZEC", "side": "LONG", "size": 10,
                           "entry_px": 40.0, "notional_usd": 400,
                           "leverage_type": "isolated", "liq_px": 30.0}],
        },
    }]
    tags = pc.classify_portfolio(portfolio, market={"data": {"prices": {"ZEC": 39.0}}})
    assert len(tags) == 1
    assert tags[0].bucket == TACTICAL  # never CYCLE
    assert "ZEC" not in pc.cycle_coins(tags)


# ── P1.5: structural liq floor on a laddered isolated position ───────────────
def test_structural_liq_floor_present_and_below_current_liq():
    pos = {"coin": "BTC", "side": "LONG", "size": 1, "entry_px": 100000.0,
           "notional_usd": 100000, "leverage_type": "isolated", "liq_px": 80000.0}
    orders = _ladder_orders("BTC", [96000, 92000])
    tag = classify_position(pos, orders, mark_px=99000.0)
    assert tag.structural_liq_px is not None
    # blended entry = (100000+96000+92000)/3 = 96000; ratio = 0.8 → 76800
    assert abs(tag.structural_liq_px - 76800.0) < 1.0
    # the ride-or-liq floor sits below the current liq (more rungs → lower)
    assert tag.structural_liq_px < tag.liq_px


def test_structural_liq_floor_rendered_for_tactical_btc():
    # BTC TÁCTICA (has SL/TP) but laddered → floor still surfaces.
    orders = _ladder_orders("BTC", [96000, 92000]) + [
        {"coin": "BTC", "side": "SELL", "limit_px": 110000, "is_trigger": True,
         "reduce_only": True, "is_sl_tp": True}
    ]
    pos = {"coin": "BTC", "side": "LONG", "size": 1, "entry_px": 100000.0,
           "notional_usd": 100000, "leverage_type": "isolated", "liq_px": 80000.0}
    tag = classify_position(pos, orders, mark_px=99000.0)
    assert tag.bucket == TACTICAL  # SL/TP present
    assert tag.structural_liq_px is not None
    block = build_classification_block([tag])
    assert "piso liq estructural" in block


def test_no_ladder_no_structural_liq():
    pos = {"coin": "SOL", "side": "LONG", "size": 10, "entry_px": 150.0,
           "notional_usd": 1500, "leverage_type": "isolated", "liq_px": 120.0}
    tag = classify_position(pos, [], mark_px=149.0)
    assert tag.structural_liq_px is None


# ── P1.4 screener: ZEC long-context forced non-viable ────────────────────────
def test_screener_blocklist_helper_contains_zec():
    from modules.universal_screener import _dca_blocklist
    assert "ZEC" in _dca_blocklist()


# ── Plan alignment: system prompt encodes the post-ZEC plan + hard rules ─────
def test_system_prompt_encodes_plan_and_hard_rules():
    from templates.system_prompt import SYSTEM_PROMPT as sp
    low = sp.lower()
    # ZEC permanently out of cycle/DCA
    assert "zec" in low and ("fuera para siempre" in low or "liquidado" in low)
    # current plan assets present
    assert "hype" in low and "btc" in low and "sol" in low and "pear" in low
    # funding-direction rule
    assert "recibe funding" in low or "recibe" in low
    assert "carry caro" in low
    # integrity-halt rule
    assert "integrity-halt" in low or "integrity" in low
    assert "cuchillo" in low  # "nunca atrapar un cuchillo cayendo"
