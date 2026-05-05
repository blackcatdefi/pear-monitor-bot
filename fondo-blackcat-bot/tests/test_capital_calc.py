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
    # R-DASHBOARD-SPOT-FIX: label renamed Spot non-USDC → Spot non-stable.
    assert "Spot non-stable" in tg
    assert "Spot non-USDC" not in tg
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


# ---------------------------------------------------------------------------
# R-DASHBOARD-SPOT-FIX (2026-05-05) — stablecoin exclusion regression suite
# ---------------------------------------------------------------------------
import pytest

# Live snapshot 5 may 2026 12:13 UTC (BCD audit):
#   USDC 1,478.81 + USDT0 1,245.17 + USDH 360.28 + USOL 26.67 + HYPE 16.92
# Real non-stable bag = USOL + HYPE = $43.59 (dust). Pre-fix the dashboard
# rendered "Spot non-USDC: $1.7K" because USDT0+USDH were lumped in.
RDASHBOARD_LIVE_USDC = 1_478.81
RDASHBOARD_LIVE_USDT0 = 1_245.17
RDASHBOARD_LIVE_USDH = 360.28
RDASHBOARD_LIVE_USOL = 26.67
RDASHBOARD_LIVE_HYPE = 16.92
RDASHBOARD_REAL_NON_STABLE = RDASHBOARD_LIVE_USOL + RDASHBOARD_LIVE_HYPE  # $43.59
RDASHBOARD_REAL_STABLES_IDLE = (
    RDASHBOARD_LIVE_USDC + RDASHBOARD_LIVE_USDT0 + RDASHBOARD_LIVE_USDH
)


def test_spot_non_stable_excludes_usdt0_usdh():
    """The split helper must not lump USDT0/USDH into the non-stable bucket.

    Verifies the root-cause fix: pre-fix the function returned $1,648.86 for
    these balances; post-fix it must return $43.59 (dust only)."""
    from modules.portfolio_snapshot import _spot_split_value

    # No active perp → USDC also goes to stables (idle wallet).
    spot_balances = [
        {"coin": "USDC", "total": RDASHBOARD_LIVE_USDC, "entry_ntl": 0},
        {"coin": "USDT0", "total": RDASHBOARD_LIVE_USDT0, "entry_ntl": 0},
        {"coin": "USDH", "total": RDASHBOARD_LIVE_USDH, "entry_ntl": 0},
        # Non-stable: only entry_ntl is used (cost basis) — match snapshot.
        {"coin": "USOL", "total": 0.3162, "entry_ntl": RDASHBOARD_LIVE_USOL},
        {"coin": "HYPE", "total": 0.3863, "entry_ntl": RDASHBOARD_LIVE_HYPE},
    ]
    non_stable, stables = _spot_split_value(
        spot_balances, prices={}, perp_account_value=0.0,
    )
    assert non_stable == pytest.approx(RDASHBOARD_REAL_NON_STABLE, abs=0.01)
    assert non_stable < 100.0, (
        "Non-stable bag must be dust-only ($43.59), not the inflated $1,648 "
        "that included USDT0+USDH+USDC."
    )
    assert stables == pytest.approx(RDASHBOARD_REAL_STABLES_IDLE, abs=0.01)


def test_spot_non_stable_with_active_perp_drops_usdc_only():
    """When perp is active, USDC is skipped (Unified Account) but USDT0/USDH
    still go to stables — they are independent on-chain spots."""
    from modules.portfolio_snapshot import _spot_split_value

    spot_balances = [
        {"coin": "USDC", "total": RDASHBOARD_LIVE_USDC, "entry_ntl": 0},
        {"coin": "USDT0", "total": RDASHBOARD_LIVE_USDT0, "entry_ntl": 0},
        {"coin": "USDH", "total": RDASHBOARD_LIVE_USDH, "entry_ntl": 0},
        {"coin": "HYPE", "total": 0.3863, "entry_ntl": RDASHBOARD_LIVE_HYPE},
    ]
    non_stable, stables = _spot_split_value(
        spot_balances, prices={}, perp_account_value=2_900.0,
    )
    # Non-stable still excludes ALL stablecoins.
    assert non_stable == pytest.approx(RDASHBOARD_LIVE_HYPE, abs=0.01)
    # Stables exclude USDC (already in marginSummary.accountValue) but keep
    # USDT0 + USDH — those are independent on-chain spot tokens.
    assert stables == pytest.approx(
        RDASHBOARD_LIVE_USDT0 + RDASHBOARD_LIVE_USDH, abs=0.01,
    )


def test_dashboard_capital_block_no_double_count():
    """The Capital block must show "Spot non-stable: $43" (real dust) — NOT
    "Spot non-USDC: $1.7K" (the pre-fix inflated string)."""
    net = compute_net_capital({
        "hl_collateral_total": 76_500.0,  # includes UETH/USDH-borrow flywheel
        "hl_debt_total": 44_900.0,
        "perp_equity_total": 2_900.0,
        # spot_usd_total now means non-stable only — feed dust value.
        "spot_usd_total": RDASHBOARD_REAL_NON_STABLE,
        "spot_stables_total": (
            RDASHBOARD_LIVE_USDT0 + RDASHBOARD_LIVE_USDH
        ),
        "upnl_perp_total": 0.0,
    })

    tg = format_net_capital_telegram(net)
    html = render_net_capital_html(net, _fmt_compact_usd, _signed)

    # Telegram render: real dust value, NOT the inflated $1.7K. The
    # compact-USD formatter rounds $43.59 → "$44" at the integer step,
    # so we anchor the assertion on the leading "$4" prefix and on the
    # ABSENCE of the buggy "$1.7K" string under the non-stable line.
    assert "Spot non-stable:" in tg, tg
    assert "Spot non-stable: $1.7K" not in tg, "Pre-fix bug string still present"
    assert "Spot non-stable: $1,7" not in tg
    # The figure must be a sub-$50 dust number — never the inflated $1.6K
    # USDT0+USDH that used to be lumped in.
    import re
    m = re.search(r"Spot non-stable: \$(\d+(?:\.\d+)?)", tg)
    assert m is not None, f"No Spot non-stable line found:\n{tg}"
    rendered_non_stable = float(m.group(1))
    assert rendered_non_stable < 100.0, (
        f"Spot non-stable rendered as ${rendered_non_stable} — should be "
        f"<$100 dust, indicates stables are still being lumped in."
    )

    # HTML render: same — dust only, no $1.7K under the non-stable line.
    assert "Spot non-stable" in html
    # Stables get their own informative line.
    assert "Spot stables (cash equiv)" in tg
    assert "Spot stables (cash equiv)" in html


def test_net_excludes_stables_per_bcd_directive():
    """BCD directive: stables son cash equivalente, NO exposure. NET must
    equal HL_net + perp + non_stable, with stables surfaced separately."""
    n = compute_net_capital({
        "hl_collateral_total": 76_500.0,
        "hl_debt_total": 44_900.0,
        "perp_equity_total": 2_900.0,
        "spot_usd_total": 43.59,
        "spot_stables_total": 1_605.45,  # USDT0 + USDH idle
        "upnl_perp_total": 0.0,
    })
    expected_net = (76_500.0 - 44_900.0) + 2_900.0 + 43.59  # 34,543.59
    assert n.net_total_usd == pytest.approx(expected_net, abs=0.01)
    # Stables surfaced separately — NOT folded into net_total_usd.
    assert n.spot_stables_usd == pytest.approx(1_605.45, abs=0.01)
    assert n.spot_non_stable_usd == pytest.approx(43.59, abs=0.01)


def test_backward_compat_spot_non_usdc_alias():
    """Legacy callers / tests that still reach for ``spot_non_usdc_usd``
    transparently get the corrected non-stable value."""
    n = compute_net_capital({
        "hl_collateral_total": 0.0,
        "hl_debt_total": 0.0,
        "perp_equity_total": 0.0,
        "spot_usd_total": 99.99,
        "spot_stables_total": 1_000.0,
        "upnl_perp_total": 0.0,
    })
    assert n.spot_non_usdc_usd == n.spot_non_stable_usd
    assert n.spot_non_usdc_usd == pytest.approx(99.99, abs=0.01)


def test_estimate_spot_split_in_formatters():
    """Mirror of the snapshot helper for the /reporte banner builder.
    Bug surface was wider than just portfolio_snapshot — formatters had
    an independent ``_estimate_spot_usd`` with the same bug."""
    from templates.formatters import _estimate_spot_split, _estimate_spot_usd

    spot_balances = [
        {"coin": "USDC", "total": 1_500.0, "entry_ntl": 0},
        {"coin": "USDT0", "total": 1_245.0, "entry_ntl": 0},
        {"coin": "USDH", "total": 360.0, "entry_ntl": 0},
        {"coin": "HYPE", "total": 0.4, "entry_ntl": 16.92},
    ]
    non_stable, stables = _estimate_spot_split(spot_balances, perp_account_value=2_900.0)
    assert non_stable == pytest.approx(16.92, abs=0.01)
    assert stables == pytest.approx(1_245.0 + 360.0, abs=0.01)  # USDC dropped (active perp)
    # Backward-compat wrapper returns non-stable only — bug is closed.
    assert _estimate_spot_usd(spot_balances, perp_account_value=2_900.0) == pytest.approx(
        16.92, abs=0.01,
    )
