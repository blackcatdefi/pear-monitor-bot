"""R-BOT-DEFINITIVE WI-6 — trailing-rule monitor tests (MRVL case)."""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def tm(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import config
    importlib.reload(config)
    import modules.trailing_monitor as trailing_monitor
    importlib.reload(trailing_monitor)
    return trailing_monitor


def _mrvl_leg(mark=260.30, entry=294.24, size=-3.0):
    return {
        "coin": "xyz:MRVL", "size": size, "side": "SHORT", "dex": "xyz",
        "entry_px": entry, "notional_usd": abs(size) * mark,
    }


def test_favorable_move_math(tm):
    # MRVL live: entry 294.24, mark ~260 → +11.5% favorable for the short.
    move = tm.favorable_move_pct("SHORT", 294.24, 260.30)
    assert move == pytest.approx(11.53, abs=0.05)
    # LONG mirror.
    assert tm.favorable_move_pct("LONG", 100.0, 112.0) == pytest.approx(12.0)
    # Adverse move is negative.
    assert tm.favorable_move_pct("SHORT", 100.0, 110.0) == pytest.approx(-10.0)


def test_thresholds_ladder(tm):
    assert tm.crossed_thresholds(9.9) == []
    assert tm.crossed_thresholds(11.5) == [10.0]
    assert tm.crossed_thresholds(21.0) == [10.0, 15.0, 20.0]


def test_suggested_sl_and_locked_pnl_mrvl(tm):
    """WI-6 acceptance: MRVL suggested SL near 286 with correct locked math."""
    mark = 260.30
    sl = tm.suggested_sl("SHORT", mark)
    assert sl == pytest.approx(286.33, abs=0.01)  # mark × 1.10
    lock = tm.locked_pnl("SHORT", 294.24, sl, 3.0)
    assert lock == pytest.approx((294.24 - 286.33) * 3.0, abs=0.05)
    assert lock > 0  # the suggestion locks PROFIT


def test_evaluate_leg_fires_once_for_mrvl(tm):
    fire1, msg = tm.evaluate_leg(_mrvl_leg())
    assert fire1 is True
    assert "TRAILING RULE" in msg and "MRVL" in msg
    assert "+11.5" in msg
    assert "286" in msg  # suggested SL near 286
    assert "NUNCA ejecuta" in msg  # the bot never executes
    # Same state again → silent (one alert per threshold per leg).
    fire2, _ = tm.evaluate_leg(_mrvl_leg())
    assert fire2 is False


def test_next_step_refires_at_15(tm):
    tm.evaluate_leg(_mrvl_leg())                       # consumes the 10% step
    deeper = _mrvl_leg(mark=294.24 * (1 - 0.16))       # +16% favorable
    fire, msg = tm.evaluate_leg(deeper)
    assert fire is True and "+16" in msg


def test_below_threshold_never_fires(tm):
    leg = _mrvl_leg(mark=294.24 * (1 - 0.05))  # only +5%
    fire, _ = tm.evaluate_leg(leg)
    assert fire is False


def test_basket_leg_selection(tm):
    wallets = [{
        "status": "ok",
        "data": {"positions": [
            _mrvl_leg(),                                                  # basket
            {"coin": "BTC", "size": 1.0, "side": "LONG", "dex": "main",
             "entry_px": 100000.0, "notional_usd": 100000.0},              # tactical
            {"coin": "ETH", "size": -1.0, "side": "SHORT", "dex": "main",
             "entry_px": 4000.0, "notional_usd": 4000.0},                  # main short → not basket
        ]},
    }]
    legs = tm._basket_legs(wallets)
    assert len(legs) == 1 and legs[0]["coin"] == "xyz:MRVL"
