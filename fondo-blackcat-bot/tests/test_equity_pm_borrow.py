"""R-PM-LIQ / P0.2 (2026-06-06) — TOTAL EQUITY nets the PM borrow exactly once.

The bug
-------
The /reporte headline (and the auto-thesis equity) summed HYPE collateral
GROSS (at oracle) + perp account value WITHOUT subtracting the USDC the fund
borrowed against that HYPE under HyperLiquid Portfolio Margin. The borrowed
dollars were ALSO deployed into the perp account (already counted inside
``marginSummary.accountValue``), so the liability was never netted → the
top-line read ~$99–107K when Rabby/DeBank showed ~$59–67K.

The fix
-------
``templates.formatters.format_report_header`` and ``build_fund_state_block``
both accumulate ``spot_borrow_total`` from each wallet's spot balances via
``modules.portfolio_snapshot._spot_native_borrow`` and feed it into
``auto.capital_calc.compute_net_capital`` as ``spot_borrow_total``. There,
``total_equity_usd`` subtracts it EXACTLY ONCE:

    total_equity = (hl_coll-hl_debt) + perp + spot_non_stable
                   + stables + pear + vault − spot_borrow

Ground truth (6-Jun-2026, Rabby): HYPE collateral ≈ $76.5K, perp account
≈ $29.2K, USDC borrowed ≈ $39.81K, Pear staked ≈ $1.2K → TOTAL EQUITY ≈ $67K
(NOT the pre-fix ~$106.9K).
"""
from __future__ import annotations

import pytest

from auto.capital_calc import compute_net_capital
from modules.portfolio_snapshot import _spot_native_borrow

# ── Live-ish ground truth (6-Jun-2026) ──────────────────────────────────────
HYPE_COLLATERAL = 76_500.0
PERP_ACCOUNT = 29_200.0
BORROWED = 39_807.72
PEAR_STAKED = 1_200.0


def _live_dict(borrow: float = BORROWED) -> dict:
    return {
        "hl_collateral_total": 0.0,   # HyperLend flywheel closed
        "hl_debt_total": 0.0,
        "perp_equity_total": PERP_ACCOUNT,
        "spot_usd_total": HYPE_COLLATERAL,   # non-stable HYPE at oracle (gross)
        "spot_stables_total": 0.0,
        "pear_staked_total": PEAR_STAKED,
        "spot_borrow_total": borrow,
    }


# ── 1. The PM borrow is netted out of TOTAL EQUITY ──────────────────────────
def test_total_equity_nets_pm_borrow():
    n = compute_net_capital(_live_dict())
    # Lands in the Rabby/DeBank band, NOT the pre-fix ~$106.9K.
    assert 59_000.0 < n.total_equity_usd < 70_000.0
    assert n.total_equity_usd == pytest.approx(67_092.28, abs=1.0)
    # The pre-fix inflated number must NOT reappear.
    assert n.total_equity_usd < 90_000.0


# ── 2. Debt is netted EXACTLY ONCE (not zero, not twice) ────────────────────
def test_pm_borrow_netted_exactly_once():
    with_debt = compute_net_capital(_live_dict(BORROWED))
    no_debt = compute_net_capital(_live_dict(0.0))
    delta = no_debt.total_equity_usd - with_debt.total_equity_usd
    # Removing the borrow raises equity by EXACTLY the borrow amount.
    assert delta == pytest.approx(BORROWED, abs=0.01)
    # NET (post-leverage exposure) is unaffected by the borrow line — the
    # liability only moves the TOTAL EQUITY headline, never NET.
    assert with_debt.net_total_usd == pytest.approx(no_debt.net_total_usd, abs=1e-6)
    # The borrow is surfaced as its own liability field.
    assert with_debt.spot_borrow_usd == pytest.approx(BORROWED, abs=0.01)


# ── 3. The Telegram block shows the borrow as a netted liability ────────────
def test_telegram_block_shows_netted_borrow():
    from auto.capital_calc import format_net_capital_telegram

    tg = format_net_capital_telegram(compute_net_capital(_live_dict()))
    assert "TOTAL EQUITY" in tg
    assert "Deuda PM (USDC borrowed)" in tg
    assert "restada del TOTAL EQUITY" in tg


# ── 4. _spot_native_borrow reads the authoritative ``borrowed`` field ───────
def test_spot_native_borrow_uses_borrowed_field():
    # HL PM borrow row: negative net total, positive authoritative borrowed.
    bal = [
        {"coin": "USDC", "total": -10_740.32, "borrowed": BORROWED},
        {"coin": "HYPE", "total": 1317.0252},
    ]
    assert _spot_native_borrow(bal) == pytest.approx(BORROWED, abs=0.01)
    # Fallback: a negative stable total with no ``borrowed`` field counts its
    # magnitude (older payloads).
    bal2 = [{"coin": "USDC", "total": -5_000.0}]
    assert _spot_native_borrow(bal2) == pytest.approx(5_000.0, abs=0.01)
    # No borrow → 0.0, never raises.
    assert _spot_native_borrow([{"coin": "HYPE", "total": 100.0}]) == 0.0
    assert _spot_native_borrow([]) == 0.0
    assert _spot_native_borrow(None) == 0.0


# ── 5. No-borrow fund: headline equals NET + stables (no phantom debt) ───────
def test_no_borrow_headline_unchanged():
    n = compute_net_capital(_live_dict(0.0))
    expected = n.net_total_usd + n.spot_stables_usd + n.pear_staked_usd
    assert n.total_equity_usd == pytest.approx(expected, abs=0.01)
    assert n.spot_borrow_usd == 0.0
