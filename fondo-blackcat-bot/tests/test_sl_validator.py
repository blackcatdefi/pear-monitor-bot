"""R-BOT-DEFINITIVE WI-5 — SL/TP structural reachability tests (HOOD case)."""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def sv(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import config
    importlib.reload(config)
    import modules.sl_validator as sl_validator
    importlib.reload(sl_validator)
    return sl_validator


def test_hood_short_sl_beyond_liq_is_unreachable(sv):
    # Live case 2026-06-10: HOOD short, SL 99.58, isolated liq 94.90.
    assert sv.sl_unreachable("SHORT", 99.58, 94.90) is True


def test_short_sl_inside_liq_is_reachable(sv):
    assert sv.sl_unreachable("SHORT", 90.0, 94.90) is False


def test_long_cases(sv):
    assert sv.sl_unreachable("LONG", 40.0, 45.0) is True   # SL below liq
    assert sv.sl_unreachable("LONG", 50.0, 45.0) is False  # SL above liq


def test_missing_data_never_flags(sv):
    assert sv.sl_unreachable("SHORT", None, 94.9) is False
    assert sv.sl_unreachable("SHORT", 99.0, None) is False
    assert sv.sl_unreachable("SHORT", 0.0, 94.9) is False
    assert sv.sl_unreachable("?", 99.0, 94.9) is False


def test_classifier_inline_flag_only_on_hood(sv):
    """Replay the live book shape: HOOD flagged, the other legs clean."""
    from modules.position_classifier import classify_position

    def short(coin, sl, liq):
        pos = {"coin": coin, "size": -1.0, "side": "SHORT",
               "leverage_type": "isolated", "liquidationPx": liq,
               "entry_px": 100.0, "notional_usd": 1000.0}
        orders = [{
            "coin": coin, "side": "BUY", "limit_px": 0.0, "trigger_px": sl,
            "size": 1.0, "is_trigger": True, "reduce_only": True,
            "tpsl": "sl", "order_type": "Stop Market", "is_sl_tp": True,
        }]
        return classify_position(pos, orders, 90.0, orders_available=True)

    hood = short("xyz:HOOD", 99.58, 94.90)
    assert any("SL UNREACHABLE" in f for f in hood.flags)
    assert "99.58" in next(f for f in hood.flags if "SL UNREACHABLE" in f)
    assert "94.90" in next(f for f in hood.flags if "SL UNREACHABLE" in f)

    # Seven healthy variants → zero false positives.
    for coin, sl, liq in [
        ("xyz:SP500", 92.0, 94.9), ("xyz:NVDA", 93.0, 96.0),
        ("xyz:MU", 91.0, 95.0), ("xyz:MRVL", 92.5, 94.0),
        ("xyz:XYZ100", 91.5, 97.0), ("BTC", 93.0, 99.0), ("SOL", 92.0, 98.0),
    ]:
        tag = short(coin, sl, liq)
        assert not any("SL UNREACHABLE" in f for f in tag.flags), coin


def test_find_unreachable_flags_only_hood(sv):
    wallets = [{
        "status": "ok",
        "data": {
            "wallet": "0xc7ae23316b47f7e75f455f53ad37873a18351505",
            "label": "Trading",
            "positions": [
                {"coin": "xyz:HOOD", "size": -10.0, "side": "SHORT",
                 "leverage_type": "isolated", "liquidationPx": 94.90,
                 "entry_px": 100.0, "notional_usd": 900.0},
                {"coin": "xyz:NVDA", "size": -1.0, "side": "SHORT",
                 "leverage_type": "isolated", "liquidationPx": 200.0,
                 "entry_px": 180.0, "notional_usd": 180.0},
            ],
            "open_orders": [
                {"coin": "xyz:HOOD", "side": "BUY", "limit_px": 0.0,
                 "trigger_px": 99.58, "size": 10.0, "is_trigger": True,
                 "reduce_only": True, "tpsl": "sl",
                 "order_type": "Stop Market", "is_sl_tp": True},
                {"coin": "xyz:NVDA", "side": "BUY", "limit_px": 0.0,
                 "trigger_px": 195.0, "size": 1.0, "is_trigger": True,
                 "reduce_only": True, "tpsl": "sl",
                 "order_type": "Stop Market", "is_sl_tp": True},
            ],
        },
    }]
    findings = sv.find_unreachable(wallets, None)
    assert len(findings) == 1
    assert findings[0]["coin"] == "xyz:HOOD"
    assert findings[0]["sl_px"] == pytest.approx(99.58)


def test_alert_is_one_time_until_condition_changes(sv):
    assert sv.should_alert("xyz:HOOD", 99.58, 94.90) is True
    assert sv.should_alert("xyz:HOOD", 99.58, 94.90) is False  # unchanged
    assert sv.should_alert("xyz:HOOD", 101.00, 94.90) is True  # SL moved >0.5%
    sv.clear_condition("xyz:HOOD")
    assert sv.should_alert("xyz:HOOD", 101.00, 94.90) is True  # re-armed
