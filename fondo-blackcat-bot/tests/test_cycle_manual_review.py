"""P1.7 — cycle-accumulation MANUAL-REVIEW flag (never auto-close).

A position tagged ACUMULACIÓN CICLO is NEVER auto-suggested for close/reduce.
A MANUAL-REVIEW flag (input for BCD, not an action) is raised when a cycle
token has any of: (a) confirmed protocol/supply exploit, (b) funding past the
expensive APR threshold, (c) liq distance compressing below the floor (8%).
The output must say "MANUAL REVIEW", never an auto-close.
"""
from __future__ import annotations

import os

os.environ.setdefault("POSITION_CLASSIFIER_ENABLED", "true")

from modules.position_classifier import (  # noqa: E402
    classify_position,
    classify_portfolio,
    build_classification_block,
    manual_review_coins,
    CYCLE,
)


def _cycle_position(coin="BTC", liq_px=20000.0):
    # Isolated LONG, no SL/TP, with a DCA ladder below price → CYCLE.
    return {
        "coin": coin, "size": 0.5, "side": "LONG", "entry_px": 60000.0,
        "notional_usd": 30000.0, "leverage_type": "isolated", "liq_px": liq_px,
    }


def _ladder_orders(coin="BTC"):
    return [
        {"coin": coin, "side": "BUY", "limit_px": 58000.0, "size": 0.1,
         "is_trigger": False, "reduce_only": False, "is_sl_tp": False},
    ]


def test_clean_cycle_has_no_manual_review():
    tag = classify_position(_cycle_position(), _ladder_orders(), 60000.0)
    assert tag.bucket == CYCLE
    assert tag.manual_review is False
    assert not any("MANUAL REVIEW" in f for f in tag.flags)


def test_exploit_raises_manual_review_not_autoclose():
    tag = classify_position(
        _cycle_position(), _ladder_orders(), 60000.0, exploit_flagged=True
    )
    assert tag.bucket == CYCLE
    assert tag.manual_review is True
    joined = " ".join(tag.flags)
    assert "MANUAL REVIEW" in joined
    assert "exploit" in joined.lower()
    # GUARD: never an auto-close/reduce instruction.
    low = joined.lower()
    assert "cerrar" not in low or "no auto-cierre" in low
    assert "auto-cierre" in low  # explicit "NO auto-cierre"


def test_compressed_liq_distance_flags_manual_review():
    # liq at 57000 vs mark 60000 → ~5% distance (< 8% floor).
    tag = classify_position(
        _cycle_position(liq_px=57000.0), _ladder_orders(), 60000.0
    )
    assert tag.manual_review is True
    assert any("liq" in f.lower() and "MANUAL REVIEW" in f for f in tag.flags)


def test_expensive_funding_flags_manual_review():
    tag = classify_position(
        _cycle_position(), _ladder_orders(), 60000.0, funding_apr=80.0
    )
    assert tag.manual_review is True
    assert any("funding" in f.lower() and "MANUAL REVIEW" in f for f in tag.flags)


def test_classify_portfolio_threads_exploit_set():
    portfolio = [{
        "status": "ok",
        "data": {"open_orders": _ladder_orders(), "positions": [_cycle_position()]},
    }]
    tags = classify_portfolio(portfolio, {"BTC": {"price_usd": 60000.0}},
                              exploit_coins={"BTC"})
    assert manual_review_coins(tags) == {"BTC"}
    block = build_classification_block(tags)
    assert "MANUAL REVIEW" in block
    # The hard rule against bearish auto-close must remain in the block.
    assert "NUNCA" in block


def test_block_never_emits_autoclose_for_cycle():
    portfolio = [{
        "status": "ok",
        "data": {"open_orders": _ladder_orders(), "positions": [_cycle_position()]},
    }]
    tags = classify_portfolio(portfolio, {"BTC": {"price_usd": 60000.0}},
                              exploit_coins={"BTC"})
    block = build_classification_block(tags).lower()
    # No standalone "sugerir cerrar" / auto-close directive for the cycle leg.
    assert "auto-cierre" in block  # only ever as "NO auto-cierre"
