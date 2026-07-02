"""P1.5 — Portfolio Margin committed-in-resting-orders correctness.

Loaded limit orders reserve capital in HL, so the borrow head-room is NOT
freely deployable. The PM state must surface the notional committed to
resting BUY limit orders, excluding SL/TP and other reduce-only / trigger
orders (those do not commit new capital).
"""
from __future__ import annotations

from modules.portfolio_margin import (
    compute_pm_state,
    format_pm_state_telegram,
    _committed_resting_notional,
)

SPOT = [{"coin": "HYPE", "total": 1000.0}, {"coin": "USDC", "total": 5000.0}]


def test_committed_sums_resting_buys_only():
    orders = [
        {"coin": "HYPE", "side": "BUY", "limit_px": 46.0, "size": 100.0,
         "is_trigger": False, "reduce_only": False, "is_sl_tp": False},
        {"coin": "BTC", "side": "BUY", "limit_px": 60000.0, "size": 0.1,
         "is_trigger": False, "reduce_only": False, "is_sl_tp": False},
        # SL/TP reduce-only trigger — must NOT count as committed capital.
        {"coin": "BTC", "side": "SELL", "limit_px": 130000.0, "size": 0.5,
         "is_trigger": True, "reduce_only": True, "is_sl_tp": True},
    ]
    total, count = _committed_resting_notional(orders)
    assert count == 2
    assert abs(total - (46.0 * 100.0 + 60000.0 * 0.1)) < 1e-6


def test_committed_handles_hl_string_shapes_and_none():
    # Raw HL shape (limitPx/sz strings) and a malformed entry.
    orders = [
        {"coin": "@107", "side": "B", "limitPx": "50.0", "sz": "125.0"},
        None,
        {"coin": "X", "side": "B"},  # no px/sz → notional 0, skipped
    ]
    total, count = _committed_resting_notional(orders)
    assert count == 1
    assert abs(total - 50.0 * 125.0) < 1e-6
    assert _committed_resting_notional(None) == (0.0, 0)


def test_pm_state_exposes_committed_fields_and_line():
    orders = [{"coin": "HYPE", "side": "BUY", "limit_px": 46.0, "size": 100.0,
               "is_trigger": False, "reduce_only": False, "is_sl_tp": False}]
    pm = compute_pm_state(SPOT, [], {"HYPE": 40.0}, open_orders=orders)
    assert pm.committed_orders_count == 1
    assert abs(pm.committed_orders_usd - 4600.0) < 1e-6
    block = format_pm_state_telegram(pm)
    assert "comprometido en órdenes resting" in block
    # Head-room must NOT be labelled as freely deployable "free capital".
    assert "head-room borrow" in block


def test_no_orders_means_no_committed_line():
    pm = compute_pm_state(SPOT, [], {"HYPE": 40.0}, open_orders=[])
    assert pm.committed_orders_usd == 0.0
    assert "comprometido en órdenes resting" not in format_pm_state_telegram(pm)


def test_naked_long_guard_still_fires():
    # Debt drawn, no shorts → naked_long flag still True regardless of
    # committed orders. R-BOT-DEFINITIVE-2 T7: the panel line is now a NEUTRAL
    # owner-decision note, but it must still render whenever the flag is set.
    spot = [{"coin": "HYPE", "total": 1000.0}, {"coin": "USDC", "total": -5000.0}]
    pm = compute_pm_state(spot, [], {"HYPE": 40.0}, open_orders=[])
    assert pm.naked_long is True
    assert "sin hedge activo" in format_pm_state_telegram(pm).lower()
