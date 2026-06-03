"""R-REPORTE-LIVE (2026-06-03) — tests for the /reporte analysis-layer rewrite.

Covers:
  FIX 1 — HyperLend/closed-flywheel removed + 6h freshness rule.
  FIX 2 — position classification (CYCLE-ACCUMULATION vs TACTICAL).
  FIX 3 — header/body self-consistency pass.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

_HERE = os.path.dirname(__file__)
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ════════════════════════════ FIX 1 — venue + freshness ════════════════════

def test_compile_raw_data_strips_hyperlend_when_deprecated() -> None:
    """The LLM raw-data blob must NOT carry live HyperLend HF/collateral."""
    from templates.formatters import compile_raw_data

    hyperlend = [{
        "status": "ok",
        "hf_status": "OK",
        "data": {
            "wallet": "0xa44e0000000000000000000000000000000000ae",
            "label": "Flywheel",
            "total_collateral_usd": 71800.0,
            "total_debt_usd": 45000.0,
            "health_factor": 1.2001,
        },
    }]
    out = compile_raw_data(portfolio=[], hyperlend=hyperlend, market={}, unlocks={}, telegram_intel={})
    assert "deprecated_closed" in out
    assert "71800" not in out  # stale collateral must not leak
    assert "1.2001" not in out  # stale HF must not leak
    assert "Portfolio Margin" in out


def test_compile_raw_data_keeps_hyperlend_on_rollback(monkeypatch) -> None:
    import config
    monkeypatch.setattr(config, "FLYWHEEL_DEPRECATED", False, raising=False)
    from templates.formatters import compile_raw_data

    hyperlend = [{"status": "ok", "data": {"wallet": "0xa44e", "total_collateral_usd": 71800.0}}]
    out = compile_raw_data(portfolio=[], hyperlend=hyperlend, market={}, unlocks={}, telegram_intel={})
    assert "71800" in out  # legacy path still feeds HyperLend


def test_format_quick_positions_no_hyperlend_block_when_deprecated() -> None:
    """Default (deprecated) — no HYPERLEND HF block in /reporte positions."""
    from templates.formatters import format_quick_positions

    hl = [{
        "status": "ok",
        "hf_status": "OK",
        "data": {
            "wallet": "0xa44e0000000000000000000000000000000000ae",
            "label": "Flywheel",
            "total_collateral_usd": 71800.0,
            "total_debt_usd": 45000.0,
            "health_factor": 1.2001,
        },
    }]
    block = format_quick_positions(wallets=[], hyperlend=hl)
    # No live HF / HyperLend collateral surfaced.
    assert "1.200" not in block
    assert "HF:" not in block
    # The "HYPERLEND" detail header should not be rendered as a live block.
    assert "Borrowed:" not in block


def test_freshness_marks_stale_wallet() -> None:
    from auto.freshness import annotate_portfolio_freshness, is_stale

    old_iso = (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat()
    portfolio = [{
        "status": "ok",
        "stale": True,
        "stale_reason": "fetch_failed_after_retries",
        "data": {"wallet": "0xabc", "account_value": 100.0, "timestamp_utc": old_iso},
    }]
    out = annotate_portfolio_freshness(portfolio)
    assert out[0]["_stale_data"] is True
    assert "STALE" in out[0]["data"]["_freshness"]
    assert is_stale({"age_seconds": 7 * 3600}) is True
    assert is_stale({"age_seconds": 60}) is False
    assert is_stale({}) is False  # no timestamp → not provably stale


def test_freshness_fresh_wallet_untouched() -> None:
    from auto.freshness import annotate_portfolio_freshness

    fresh_iso = datetime.now(timezone.utc).isoformat()
    portfolio = [{"status": "ok", "data": {"wallet": "0xabc", "timestamp_utc": fresh_iso}}]
    out = annotate_portfolio_freshness(portfolio)
    assert "_freshness" not in out[0]["data"]
    assert "_stale_data" not in out[0]


# ════════════════════════════ FIX 2 — classification ═══════════════════════

def _long_isolated_with_ladder():
    """An isolated LONG with no SL/TP and 2 buy-limit ladders below price."""
    position = {
        "coin": "BTC", "size": 0.5, "side": "LONG", "entry_px": 70000.0,
        "notional_usd": 35000.0, "liq_px": 50000.0, "leverage_type": "isolated",
    }
    orders = [
        {"coin": "BTC", "side": "BUY", "limit_px": 65000.0, "is_trigger": False,
         "reduce_only": False, "is_sl_tp": False},
        {"coin": "BTC", "side": "BUY", "limit_px": 60000.0, "is_trigger": False,
         "reduce_only": False, "is_sl_tp": False},
    ]
    return position, orders


def test_classify_cycle_accumulation_long() -> None:
    from modules.position_classifier import classify_position, CYCLE

    pos, orders = _long_isolated_with_ladder()
    tag = classify_position(pos, orders, mark_px=70000.0, orders_available=True)
    assert tag.bucket == CYCLE
    assert "ACUMULACIÓN CICLO" in tag.tag_es
    assert tag.ladder_count == 2
    assert tag.lowest_ladder_px == 60000.0
    # Liq distance = (70000-50000)/70000 = 28.6% → > 8% → no compress flag.
    assert all("LIQ COMPRIMIDA" not in f for f in tag.flags)


def test_cycle_flags_when_liq_compresses() -> None:
    from modules.position_classifier import classify_position, CYCLE

    pos, orders = _long_isolated_with_ladder()
    pos["liq_px"] = 66000.0  # mark 70000 → distance 5.7% < 8%
    tag = classify_position(pos, orders, mark_px=70000.0, orders_available=True)
    assert tag.bucket == CYCLE
    assert any("LIQ COMPRIMIDA" in f for f in tag.flags)


def test_position_with_sl_tp_is_tactical() -> None:
    from modules.position_classifier import classify_position, TACTICAL

    pos, orders = _long_isolated_with_ladder()
    orders.append({"coin": "BTC", "side": "SELL", "limit_px": 80000.0,
                   "is_trigger": True, "reduce_only": True, "is_sl_tp": True})
    tag = classify_position(pos, orders, mark_px=70000.0, orders_available=True)
    assert tag.bucket == TACTICAL
    assert tag.has_sl_tp is True


def test_cross_margin_basket_short_is_tactical() -> None:
    from modules.position_classifier import classify_position, TACTICAL

    pos = {"coin": "ENA", "size": -1000.0, "side": "SHORT", "entry_px": 0.5,
           "notional_usd": 500.0, "liq_px": 0.9, "leverage_type": "cross"}
    tag = classify_position(pos, [], mark_px=0.5, orders_available=True)
    assert tag.bucket == TACTICAL


def test_orders_unavailable_isolated_long_flagged_not_cycle() -> None:
    """Without order visibility we cannot confirm a ladder → TACTICAL, but the
    flag warns the LLM not to close it blindly."""
    from modules.position_classifier import classify_position, TACTICAL

    pos = {"coin": "BTC", "size": 0.5, "side": "LONG", "entry_px": 70000.0,
           "notional_usd": 35000.0, "liq_px": 50000.0, "leverage_type": "isolated"}
    tag = classify_position(pos, [], mark_px=70000.0, orders_available=False)
    assert tag.bucket == TACTICAL
    assert tag.orders_unavailable is True
    assert any("NO sugerir cierre a ciegas" in f for f in tag.flags)


def test_classification_block_contains_hard_rule() -> None:
    from modules.position_classifier import classify_position, build_classification_block

    pos, orders = _long_isolated_with_ladder()
    tag = classify_position(pos, orders, mark_px=70000.0, orders_available=True)
    block = build_classification_block([tag])
    assert "NUNCA sugerir" in block
    assert "ACUMULACIÓN CICLO" in block
    assert build_classification_block([]) == ""  # empty when no positions


def test_classify_portfolio_end_to_end() -> None:
    from modules.position_classifier import classify_portfolio, cycle_coins

    pos, orders = _long_isolated_with_ladder()
    portfolio = [{
        "status": "ok",
        "data": {"wallet": "0xc7ae", "positions": [pos], "open_orders": orders},
    }]
    market = {"data": {"BTC": {"price": 70000.0}}}
    tags = classify_portfolio(portfolio, market)
    assert len(tags) == 1
    assert "BTC" in cycle_coins(tags)


# ════════════════════════════ FIX 3 — consistency ══════════════════════════

def test_consistency_drops_live_hyperlend_hf_line() -> None:
    from modules.report_consistency import enforce_consistency

    report = (
        "1. PORTFOLIO\n"
        "HyperLend HF: 1.21 — zona cómoda\n"
        "Portfolio Margin ratio 18% CALM\n"
    )
    clean, dropped = enforce_consistency(report, flywheel_deprecated=True)
    assert "HyperLend HF: 1.21" not in clean
    assert "Portfolio Margin ratio 18% CALM" in clean
    assert len(dropped) == 1


def test_consistency_keeps_hyperlend_closed_affirmation() -> None:
    from modules.report_consistency import enforce_consistency

    report = "HyperLend: CERRADO (flywheel migrado a Portfolio Margin), HF n/a"
    clean, dropped = enforce_consistency(report, flywheel_deprecated=True)
    assert "CERRADO" in clean
    assert dropped == []


def test_consistency_drops_bearish_close_on_cycle_coin() -> None:
    from modules.report_consistency import enforce_consistency

    report = (
        "Acción BTC: cerrar el long por entorno bearish y CVD negativo.\n"
        "Acción ENA: mantener short del basket.\n"
    )
    clean, dropped = enforce_consistency(report, flywheel_deprecated=True, cycle_coins={"BTC"})
    assert "cerrar el long" not in clean
    assert "mantener short del basket" in clean
    assert len(dropped) == 1


def test_consistency_keeps_cycle_flag_close_when_not_bearish() -> None:
    """A close suggestion on a cycle coin for a LIQ reason (not bearish) stays."""
    from modules.report_consistency import enforce_consistency

    report = "BTC: reducir si la distancia a liq baja de 8%.\n"
    clean, dropped = enforce_consistency(report, flywheel_deprecated=True, cycle_coins={"BTC"})
    assert "reducir si la distancia a liq" in clean
    assert dropped == []


def test_consistency_never_raises_on_garbage() -> None:
    from modules.report_consistency import enforce_consistency

    clean, dropped = enforce_consistency("", flywheel_deprecated=True, cycle_coins={"BTC"})
    assert clean == ""
    assert dropped == []
