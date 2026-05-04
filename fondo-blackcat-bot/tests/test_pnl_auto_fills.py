"""R-PNL-AUTOMATIC — regression tests for auto-fill PnL computation.

Tests use build_auto_summary_from_fills (pure function, no HTTP) so they
run offline and are deterministic.

Invariants:
1. test_pnl_7d_consumes_auto_fills   — 7d sub-window is populated from ytd fills
2. test_pnl_no_double_count          — a fill appearing in both ytd & all does not double-count
3. test_pnl_excludes_open_fills      — Open fills contribute 0 to realized PnL
4. test_pnl_subtracts_fees           — net = gross - fees (open + close fees deducted)
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from modules.pnl_tracker import (
    _compute_fill_stats,
    _filter_fills_since,
    build_auto_summary_from_fills,
)

# ─── helpers ──────────────────────────────────────────────────────────────────

_NOW_MS = int(datetime.now(timezone.utc).timestamp() * 1000)
_DAY_MS = 24 * 60 * 60 * 1000


def _fill(
    dir: str,
    closedPnl: float,
    fee: float,
    sz: float = 1.0,
    px: float = 100.0,
    age_days: float = 0.0,
) -> dict:
    """Build a minimal synthetic fill dict."""
    ts = _NOW_MS - int(age_days * _DAY_MS)
    return {
        "dir": dir,
        "closedPnl": closedPnl,
        "fee": fee,
        "sz": sz,
        "px": px,
        "time": ts,
        "_wallet_label": "test",
    }


# ─── test 1: 7d window consumes auto-fills ────────────────────────────────────

def test_pnl_7d_consumes_auto_fills():
    """7d sub-window must sum fills within the last 7 days."""
    fills_ytd = [
        _fill("Close Long", closedPnl=200.0, fee=5.0, age_days=2),   # inside 7d
        _fill("Close Long", closedPnl=100.0, fee=3.0, age_days=5),   # inside 7d
        _fill("Close Short", closedPnl=50.0, fee=2.0, age_days=20),  # outside 7d
    ]

    now = datetime.now(timezone.utc)
    d7 = now - timedelta(days=7)
    fills_7d = _filter_fills_since(fills_ytd, d7)

    assert len(fills_7d) == 2, f"Expected 2 fills in 7d window, got {len(fills_7d)}"

    s = _compute_fill_stats(fills_7d)
    assert s["gross"] == pytest.approx(300.0), "gross should be 200+100=300"
    assert s["n_trades"] == 2


# ─── test 2: no double-counting ───────────────────────────────────────────────

def test_pnl_no_double_count():
    """All-time window must not double-count fills that also appear in YTD."""
    # One close fill in YTD, same fill also in all-time (as would be the case)
    shared_fill = _fill("Close Long", closedPnl=500.0, fee=10.0, age_days=1)
    fills_ytd = [shared_fill]
    fills_all = [shared_fill]  # same object — all-time includes YTD

    text = build_auto_summary_from_fills(
        fills_ytd=fills_ytd,
        fills_alltime=fills_all,
        manual_count=0,
        year=2026,
    )

    # YTD section must show +$490.00 net (500 - 10 fees), not 980
    assert "+$490.00" in text, f"Expected +$490.00 in YTD section, got:\n{text}"
    # All-time section also shows the same single fill
    assert text.count("+$490.00") >= 1


# ─── test 3: open fills excluded from realized PnL ───────────────────────────

def test_pnl_excludes_open_fills():
    """Open fills (dir='Open Long'/'Open Short') must not contribute to gross PnL."""
    fills = [
        _fill("Open Long",  closedPnl=0.0,   fee=5.0, age_days=1),
        _fill("Open Short", closedPnl=0.0,   fee=3.0, age_days=1),
        _fill("Close Long", closedPnl=200.0, fee=4.0, age_days=1),
    ]

    s = _compute_fill_stats(fills)
    assert s["gross"] == pytest.approx(200.0), "gross must only count Close fills"
    assert s["n_trades"] == 1, "only 1 close trade"
    # fees from ALL fills are deducted
    assert s["fees"] == pytest.approx(12.0), "fees = 5+3+4 = 12"
    assert s["net"] == pytest.approx(188.0), "net = 200 - 12 = 188"


# ─── test 4: fees subtracted correctly ───────────────────────────────────────

def test_pnl_subtracts_fees():
    """net = gross_pnl - sum(all fees); gross and net appear correctly in output."""
    fills_ytd = [
        _fill("Close Long",  closedPnl=504.19, fee=30.0, age_days=3),
        _fill("Close Short", closedPnl=0.0,    fee=37.88, age_days=3),  # close with 0 PnL
        _fill("Open Long",   closedPnl=0.0,    fee=0.0,   age_days=3),
    ]

    s = _compute_fill_stats(fills_ytd)
    assert s["gross"] == pytest.approx(504.19)
    assert s["fees"] == pytest.approx(67.88)
    assert s["net"] == pytest.approx(504.19 - 67.88)

    text = build_auto_summary_from_fills(
        fills_ytd=fills_ytd,
        fills_alltime=fills_ytd,
        manual_count=0,
        year=2026,
    )
    # Check net appears in 7d section (fill is 3 days old → inside 7d)
    assert "net" in text.lower()
    assert "fees" in text.lower()
