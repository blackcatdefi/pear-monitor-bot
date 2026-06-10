"""R-BOT-DEFINITIVE WI-2 — DESTACADO UPnL truth-in-labeling tests."""
from __future__ import annotations

import pytest

from templates import formatters as fmt

_W = "0xc7ae23316b47f7e75f455f53ad37873a18351505"


def _wallet(positions):
    return [{
        "status": "ok",
        "data": {
            "wallet": _W,
            "label": "BlackCatDeFi EVM",
            "account_value": 20000.0,
            "positions": positions,
            "spot_balances": [],
        },
    }]


def _pos(coin, side, upnl, dex):
    sz = 1.0 if side == "LONG" else -1.0
    return {"coin": coin, "size": sz, "side": side, "unrealized_pnl": upnl,
            "dex": dex, "notional_usd": 1000.0}


def _live_like_positions():
    """2026-06-10 live shape: 6 xyz shorts +$1,544 / BTC+SOL longs −$1,224."""
    shorts = [
        _pos("xyz:SP500", "SHORT", 400.0, "xyz"),
        _pos("xyz:XYZ100", "SHORT", 300.0, "xyz"),
        _pos("xyz:NVDA", "SHORT", 250.0, "xyz"),
        _pos("xyz:MU", "SHORT", 200.0, "xyz"),
        _pos("xyz:MRVL", "SHORT", 250.0, "xyz"),
        _pos("xyz:HOOD", "SHORT", 144.0, "xyz"),
    ]
    longs = [
        _pos("BTC", "LONG", -800.0, "main"),
        _pos("SOL", "LONG", -424.0, "main"),
    ]
    return shorts + longs


def test_split_classifies_basket_vs_tactical():
    b, n, t, coins, tot = fmt._perp_upnl_split(_wallet(_live_like_positions()))
    assert n == 6
    assert b == pytest.approx(1544.0)
    assert t == pytest.approx(-1224.0)
    assert coins == ["BTC", "SOL"]
    assert tot == pytest.approx(320.0)


def test_total_is_sum_of_parts_always():
    """WI-2 acceptance: Z = X + Y asserted."""
    b, _n, t, _c, tot = fmt._perp_upnl_split(_wallet(_live_like_positions()))
    assert tot == pytest.approx(b + t)
    # Also with odd extras (a main-dex short and a HIP-3 long → tactical).
    extra = _live_like_positions() + [
        _pos("ETH", "SHORT", -50.0, "main"),
        _pos("xyz:TSLA", "LONG", 30.0, "xyz"),
    ]
    b2, _n2, t2, _c2, tot2 = fmt._perp_upnl_split(_wallet(extra))
    assert tot2 == pytest.approx(b2 + t2)


def test_header_renders_three_lines():
    header = fmt.format_report_header(_wallet(_live_like_positions()), [], {"status": "error"})
    assert "BASKET UPnL (6 short legs):" in header
    assert "TACTICAL LONGS UPnL (BTC, SOL):" in header
    assert "PERP ACCOUNT UPnL (total):" in header
    # The mixed wallet-total framing is gone.
    assert "8 legs" not in header


def test_header_idle_basket_unchanged():
    header = fmt.format_report_header(_wallet([]), [], {"status": "error"})
    assert "basket idle" in header


def test_dex_prefix_stripped_from_tactical_labels():
    poss = [_pos("xyz:TSLA", "LONG", 10.0, "xyz")]
    _b, _n, _t, coins, _tot = fmt._perp_upnl_split(_wallet(poss))
    assert coins == ["TSLA"]
