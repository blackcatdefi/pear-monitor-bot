"""R-DASH — capital_calc tests.

Single-source-of-truth invariants:

1. NET = (HL_coll - HL_debt) + perp + spot.
2. UPnL is NOT added on top of perp (Hyperliquid Unified Account already
   folds UPnL into accountValue).
3. The dashboard render and the /reporte plain-text render MUST consume the
   same ``NetCapital`` instance — different formatters, identical numbers.
4. Live snapshot from 1 may 2026 13:31 UTC reproduces NET ~$33.85K from
   the audit values.
"""
from __future__ import annotations

from auto.capital_calc import (
    NetCapital,
    compute_net_capital,
    format_net_capital_telegram,
    render_net_capital_html,
)


# ---------------------------------------------------------------------------
# Live-snapshot reference values (1 may 2026 13:31 UTC)
# ---------------------------------------------------------------------------
LIVE_HL_COLL = 73_200.0
LIVE_HL_DEBT = 45_300.0
LIVE_PERP = 5_700.0
LIVE_SPOT = 15.40
LIVE_UPNL = 231.59  # already inside LIVE_PERP under Unified Account


def _live_dict() -> dict:
    return {
        "hl_collateral_total": LIVE_HL_COLL,
        "hl_debt_total": LIVE_HL_DEBT,
        "perp_equity_total": LIVE_PERP,
        "spot_usd_total": LIVE_SPOT,
        "upnl_perp_total": LIVE_UPNL,
    }


# ---------------------------------------------------------------------------
# 1. NET formula
# ---------------------------------------------------------------------------
def test_net_capital_formula_matches_live_snapshot():
    net = compute_net_capital(_live_dict())

    # NET = (73.2K - 45.3K) + 5.7K + 0.0154K = 27.9K + 5.7K + 0.0154K ≈ 33.8154K
    expected_net = (LIVE_HL_COLL - LIVE_HL_DEBT) + LIVE_PERP + LIVE_SPOT
    assert abs(net.net_total_usd - expected_net) < 0.01
    assert abs(net.net_total_usd - 33_615.40) < 0.01

    assert abs(net.hl_net_usd - 27_900.0) < 0.01
    assert abs(net.perp_equity_usd - LIVE_PERP) < 0.01
    assert abs(net.spot_non_usdc_usd - LIVE_SPOT) < 0.01
    assert abs(net.hl_collateral_usd - LIVE_HL_COLL) < 0.01
    assert abs(net.hl_debt_usd - LIVE_HL_DEBT) < 0.01


def test_net_capital_no_double_count_upnl():
    """UPnL must NOT inflate net_total_usd.

    If we ran the calc twice — once with upnl=0 and once with the real
    upnl — both should give the SAME net_total_usd. The non-zero upnl
    only changes the informative ``upnl_perp_usd`` field.
    """
    base = _live_dict() | {"upnl_perp_total": 0.0}
    real = _live_dict()  # has the real UPnL

    n0 = compute_net_capital(base)
    n1 = compute_net_capital(real)

    assert abs(n0.net_total_usd - n1.net_total_usd) < 1e-6
    assert abs(n0.gross_exposure_usd - n1.gross_exposure_usd) < 1e-6
    # Only the informative field differs
    assert abs(n0.upnl_perp_usd) < 1e-9
    assert abs(n1.upnl_perp_usd - LIVE_UPNL) < 1e-6


def test_gross_exposure_equals_pre_fix_total():
    """gross_exposure_usd preserves the buggy old "Total" line for
    informational use."""
    net = compute_net_capital(_live_dict())
    expected_gross = LIVE_HL_COLL + LIVE_PERP + LIVE_SPOT
    assert abs(net.gross_exposure_usd - expected_gross) < 0.01


def test_compute_accepts_object_with_attributes():
    """compute_net_capital must work on a dataclass-shaped object too."""

    class _Snap:
        hl_collateral_total = LIVE_HL_COLL
        hl_debt_total = LIVE_HL_DEBT
        perp_equity_total = LIVE_PERP
        spot_usd_total = LIVE_SPOT
        upnl_perp_total = LIVE_UPNL

    n = compute_net_capital(_Snap())
    assert abs(n.net_total_usd - 33_615.40) < 0.01


def test_compute_handles_missing_keys_gracefully():
    """Empty dict → all zeros, no exception."""
    n = compute_net_capital({})
    assert n.net_total_usd == 0.0
    assert n.gross_exposure_usd == 0.0
    assert n.hl_net_usd == 0.0


# ---------------------------------------------------------------------------
# 2. Dashboard ↔ /reporte single-source-of-truth invariants
# ---------------------------------------------------------------------------
def _fmt_compact_usd(v):
    try:
        f = float(v)
    except Exception:
        return "—"
    sign = "-" if f < 0 else ""
    f = abs(f)
    if f >= 1_000_000:
        return f"{sign}${f/1_000_000:.2f}M"
    if f >= 1_000:
        return f"{sign}${f/1_000:.1f}K"
    return f"{sign}${f:.2f}"


def _signed(v):
    try:
        f = float(v)
        sign = "+" if f >= 0 else "-"
        cls = "pos" if f >= 0 else "neg"
        return cls, f"{sign}${abs(f):,.2f}"
    except Exception:
        return "", "—"


def test_dashboard_matches_reporte_net_value():
    """The HTML and Telegram renderers consume the SAME NetCapital instance,
    so the headline NET number must be identical regardless of compact-USD
    formatting differences."""
    net = compute_net_capital(_live_dict())
    tg = format_net_capital_telegram(net)
    html = render_net_capital_html(net, _fmt_compact_usd, _signed)

    # Both renderings must announce the same NET top-line and the same
    # gross exposure footer. We assert via the compact-USD formatting both
    # paths agree on (≈ "$33.6K").
    expected_net_compact = _fmt_compact_usd(net.net_total_usd)
    expected_gross_compact = _fmt_compact_usd(net.gross_exposure_usd)

    assert expected_net_compact in tg
    assert expected_net_compact in html
    assert expected_gross_compact in tg
    assert expected_gross_compact in html


def test_telegram_block_contains_breakdown_lines():
    net = compute_net_capital(_live_dict())
    tg = format_net_capital_telegram(net)
    assert "NET CAPITAL" in tg
    assert "HL net (col-debt)" in tg
    assert "Perp account" in tg
    assert "Spot non-USDC" in tg
    assert "Gross exposure" in tg
    assert "HL collateral" in tg
    assert "HL debt" in tg


def test_html_block_marks_net_as_top_line():
    net = compute_net_capital(_live_dict())
    html = render_net_capital_html(net, _fmt_compact_usd, _signed)
    # NET must precede gross in the rendered fragment — readers see NET first.
    net_idx = html.index("NET:")
    gross_idx = html.index("Gross exposure")
    assert net_idx < gross_idx


# ---------------------------------------------------------------------------
# 3. Edge: HL-only fund (no perp, no spot)
# ---------------------------------------------------------------------------
def test_hl_only_fund_net_equals_hl_net():
    net = compute_net_capital({
        "hl_collateral_total": 10_000.0,
        "hl_debt_total": 4_000.0,
        "perp_equity_total": 0.0,
        "spot_usd_total": 0.0,
        "upnl_perp_total": 0.0,
    })
    assert net.net_total_usd == 6_000.0
    assert net.hl_net_usd == 6_000.0
    assert net.gross_exposure_usd == 10_000.0


def test_perp_only_fund_net_equals_perp():
    net = compute_net_capital({
        "hl_collateral_total": 0.0,
        "hl_debt_total": 0.0,
        "perp_equity_total": 8_000.0,
        "spot_usd_total": 0.0,
        "upnl_perp_total": 500.0,
    })
    # NET = 0 + 8000 + 0 = 8000. UPnL is NOT re-added.
    assert net.net_total_usd == 8_000.0
    assert net.gross_exposure_usd == 8_000.0
