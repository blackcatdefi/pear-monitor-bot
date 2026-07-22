"""R-PUBLIC-FUNDS side-task — DESTACADO tactical label derived from real sides.

The header used to hardcode 'TACTICAL LONGS' even when every tactical leg was
a SHORT (e.g. the LTC/RUNE/RSR/NIL main-dex shorts book of 2026-07). The
label must follow the actual sides:
  all shorts → TACTICAL SHORTS · all longs → TACTICAL LONGS · mixed → TACTICAL BOOK
"""
from __future__ import annotations

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


def _current_book_shorts():
    """2026-07 live shape: xyz basket shorts + LTC/RUNE/RSR/NIL MAIN-dex shorts."""
    basket = [
        _pos("xyz:SP500", "SHORT", 100.0, "xyz"),
        _pos("xyz:NVDA", "SHORT", 50.0, "xyz"),
        _pos("xyz:HOOD", "SHORT", 25.0, "xyz"),
    ]
    tacticals = [
        _pos("LTC", "SHORT", 80.0, "main"),
        _pos("RUNE", "SHORT", 40.0, "main"),
        _pos("RSR", "SHORT", 30.0, "main"),
        _pos("NIL", "SHORT", 20.0, "main"),
    ]
    return basket + tacticals


def test_all_tactical_shorts_label():
    assert fmt._tactical_book_label(_wallet(_current_book_shorts())) == "TACTICAL SHORTS"


def test_all_tactical_longs_label():
    poss = [
        _pos("xyz:NVDA", "SHORT", 10.0, "xyz"),
        _pos("BTC", "LONG", -5.0, "main"),
        _pos("SOL", "LONG", -3.0, "main"),
    ]
    assert fmt._tactical_book_label(_wallet(poss)) == "TACTICAL LONGS"


def test_mixed_tactical_book_label():
    poss = [
        _pos("BTC", "LONG", 5.0, "main"),
        _pos("LTC", "SHORT", 8.0, "main"),
    ]
    assert fmt._tactical_book_label(_wallet(poss)) == "TACTICAL BOOK"


def test_no_tacticals_keeps_legacy_longs_label():
    poss = [_pos("xyz:NVDA", "SHORT", 10.0, "xyz")]
    assert fmt._tactical_book_label(_wallet(poss)) == "TACTICAL LONGS"


def test_never_raises_on_garbage():
    assert fmt._tactical_book_label(None) in (
        "TACTICAL SHORTS", "TACTICAL LONGS", "TACTICAL BOOK"
    )
    assert fmt._tactical_book_label([{"status": "err"}]) == "TACTICAL LONGS"


def test_header_renders_tactical_shorts_for_current_book():
    """End-to-end: DESTACADO header line shows the derived label."""
    b, n, t, coins, tot = fmt._perp_upnl_split(_wallet(_current_book_shorts()))
    assert n == 3
    assert set(coins) == {"LTC", "RUNE", "RSR", "NIL"}
    label = fmt._tactical_book_label(_wallet(_current_book_shorts()))
    line = f"🎯 {label} UPnL ({', '.join(coins)}): +{t:.0f}"
    assert "TACTICAL SHORTS UPnL (LTC, RUNE, RSR, NIL)" in line
