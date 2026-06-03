"""R-REPORTE-LIVE hotfix (2026-06-03) — regression tests for the raw-HL
``frontendOpenOrders`` normalization layer (modules.portfolio._normalize_open_orders).

ROOT CAUSE this guards against:
  HL serialises every numeric field as a STRING. Plain limit orders carry
  ``triggerPx == "0.0"`` and ``bool("0.0") is True`` in Python, so the old
  ``is_trigger = bool(o.get("triggerPx"))`` flagged EVERY resting limit as a
  trigger. That single misread cascaded:
    • is_sl_tp = is_trigger or ...  →  has_sl_tp wrongly True
    • classifier ladder loop ``if o.get("is_trigger"): continue``  →  ladder=0
  Result: the BTC cycle-accumulation long was mis-tagged TÁCTICA and the report
  suggested a partial TP — exactly what a CYCLE leg must never receive.

The existing classifier tests feed ALREADY-NORMALIZED orders, so they never
exercised this layer. These tests use RAW HL order shapes (string numbers,
"B"/"A" sides, triggerPx "0.0", orderType "Limit"/"Take Profit Market").
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(__file__)
_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ── Real BTC case fixture: 5 plain DCA limit buys, NO triggers ──────────────
# Prices mirror the live 3-Jun position: $63,500 / $60,500 / $57,000 /
# $52,500 / $48,000, all below the ~$66k mark, all same-side (BUY) as the long.
_BTC_DCA_LADDER_PX = [63500.0, 60500.0, 57000.0, 52500.0, 48000.0]
_BTC_MARK = 66000.0


def _raw_dca_buy(limit_px: float) -> dict:
    """A plain resting limit BUY exactly as HL frontendOpenOrders returns it."""
    return {
        "coin": "BTC",
        "side": "B",                  # bid / buy
        "limitPx": f"{limit_px}",     # HL sends numbers as strings
        "sz": "0.05",
        "oid": int(limit_px),
        "timestamp": 1717400000000,
        "triggerCondition": "N/A",
        "isTrigger": False,
        "triggerPx": "0.0",           # ← the trap: string "0.0" is truthy
        "isPositionTpsl": False,
        "reduceOnly": False,
        "orderType": "Limit",
        "tif": "Gtc",
    }


def _raw_tp_trigger() -> dict:
    """A HL native position take-profit: reduce-only TRIGGER order (sells to
    close a long when price rises above the trigger)."""
    return {
        "coin": "BTC",
        "side": "A",                  # ask / sell (reduce a long)
        "limitPx": "0.0",
        "sz": "0.5",
        "oid": 999,
        "timestamp": 1717400000000,
        "triggerCondition": "Price above 80000.0",
        "isTrigger": True,
        "triggerPx": "80000.0",
        "isPositionTpsl": True,
        "reduceOnly": True,
        "orderType": "Take Profit Market",
        "tif": "Gtc",
    }


def _btc_long_position() -> dict:
    return {
        "coin": "BTC",
        "size": 0.236,
        "side": "LONG",
        "entry_px": 66100.0,
        "notional_usd": 15608.0,
        "liq_px": 41000.0,
        "leverage_type": "isolated",
    }


# ════════════════ normalization layer (the bug's home) ════════════════

def test_plain_limit_buys_are_not_triggers_and_not_sltp() -> None:
    """The core regression: triggerPx '0.0' must NOT make a limit a trigger."""
    from modules.portfolio import _normalize_open_orders

    raw = [_raw_dca_buy(px) for px in _BTC_DCA_LADDER_PX]
    norm = _normalize_open_orders(raw)

    assert len(norm) == 5
    for o in norm:
        assert o["is_trigger"] is False, "plain limit wrongly flagged as trigger"
        assert o["reduce_only"] is False
        assert o["is_sl_tp"] is False, "plain DCA buy wrongly flagged as SL/TP"
        assert o["side"] == "BUY"
        assert o["trigger_px"] is None
    assert sorted(o["limit_px"] for o in norm) == sorted(_BTC_DCA_LADDER_PX)


def test_reduce_only_tp_trigger_is_sltp() -> None:
    """Inverse: a reduce-only take-profit TRIGGER must read as SL/TP."""
    from modules.portfolio import _normalize_open_orders

    norm = _normalize_open_orders([_raw_tp_trigger()])
    assert len(norm) == 1
    o = norm[0]
    assert o["is_trigger"] is True
    assert o["reduce_only"] is True
    assert o["is_sl_tp"] is True
    assert o["side"] == "SELL"
    assert o["trigger_px"] == 80000.0


def test_stop_market_trigger_is_sltp() -> None:
    """A reduce-only stop-loss trigger (orderType 'Stop Market') is SL/TP too."""
    from modules.portfolio import _normalize_open_orders

    sl = _raw_tp_trigger()
    sl["orderType"] = "Stop Market"
    sl["triggerPx"] = "55000.0"
    sl["triggerCondition"] = "Price below 55000.0"
    norm = _normalize_open_orders([sl])
    assert norm[0]["is_sl_tp"] is True


def test_plain_buy_without_triggerpx_field_still_not_trigger() -> None:
    """Defensive: even if HL omits triggerPx entirely, a limit stays a limit."""
    from modules.portfolio import _normalize_open_orders

    o = _raw_dca_buy(60000.0)
    o.pop("triggerPx", None)
    o.pop("triggerCondition", None)
    norm = _normalize_open_orders([o])
    assert norm[0]["is_trigger"] is False
    assert norm[0]["is_sl_tp"] is False


# ════════════════ end-to-end: normalization → classifier ════════════════

def test_btc_dca_classifies_as_cycle_accumulation() -> None:
    """THE bug, end to end: raw HL DCA orders → CYCLE with ladder=5, SL/TP=no."""
    from modules.portfolio import _normalize_open_orders
    from modules.position_classifier import classify_position, CYCLE

    raw = [_raw_dca_buy(px) for px in _BTC_DCA_LADDER_PX]
    norm = _normalize_open_orders(raw)
    tag = classify_position(
        _btc_long_position(), norm, mark_px=_BTC_MARK, orders_available=True
    )

    assert tag.bucket == CYCLE, "BTC long must be ACUMULACIÓN CICLO, not TÁCTICA"
    assert "ACUMULACIÓN CICLO" in tag.tag_es
    assert tag.ladder_count == 5, f"ladder must be 5, got {tag.ladder_count}"
    assert tag.has_sl_tp is False, "BTC long has no SL/TP attached"
    assert tag.lowest_ladder_px == 48000.0
    # A CYCLE leg must never carry a bearish close suggestion.
    assert all("cerrar" not in f.lower() for f in tag.flags)


def test_btc_with_tp_trigger_is_tactical() -> None:
    """Inverse end-to-end: add a reduce-only TP trigger → SL/TP true → TÁCTICA."""
    from modules.portfolio import _normalize_open_orders
    from modules.position_classifier import classify_position, TACTICAL

    raw = [_raw_dca_buy(px) for px in _BTC_DCA_LADDER_PX] + [_raw_tp_trigger()]
    norm = _normalize_open_orders(raw)
    tag = classify_position(
        _btc_long_position(), norm, mark_px=_BTC_MARK, orders_available=True
    )

    assert tag.has_sl_tp is True
    assert tag.bucket == TACTICAL
    # The 5 DCA buys are still counted as ladder; only the TP trigger is excluded.
    assert tag.ladder_count == 5


def test_btc_dca_end_to_end_via_classify_portfolio() -> None:
    """Through the production path: classify_portfolio over a wallet whose
    open_orders were normalized by _normalize_open_orders."""
    from modules.portfolio import _normalize_open_orders
    from modules.position_classifier import classify_portfolio, cycle_coins

    norm = _normalize_open_orders([_raw_dca_buy(px) for px in _BTC_DCA_LADDER_PX])
    portfolio = [{
        "status": "ok",
        "data": {
            "wallet": "0xc7ae",
            "positions": [_btc_long_position()],
            "open_orders": norm,
        },
    }]
    market = {"data": {"BTC": {"price": _BTC_MARK}}}
    tags = classify_portfolio(portfolio, market)
    assert len(tags) == 1
    assert "BTC" in cycle_coins(tags)
    assert tags[0].ladder_count == 5
    assert tags[0].has_sl_tp is False
