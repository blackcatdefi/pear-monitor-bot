"""R-UNIFIED-LIQ Phase A — pinned tests for the official HL PM borrow liq model.

Snapshot Aug-11 2026: debt 91.6K USDC, 3,006.28 HYPE collateral, LTV 0.65.
Mandate pins (pure ladder, no offsets):
  PARTIAL 36.93 · FULL 34.49 · early-warning 38.87 · action 40.62 · observation 44.32
Backward-consistency: LTV 0.50 → partial factor 0.75 (legacy app value).
"""
from __future__ import annotations

import pytest

from modules.portfolio_margin import (
    borrow_full_liq_factor,
    borrow_partial_liq_factor,
    compute_borrow_liq_ladder,
    _liq_threshold_for_ltv,
)

DEBT = 91_600.0
QTY = 3_006.28
LTV = 0.65


# ------------------------------------------------------------------ factors
def test_partial_factor_065():
    assert borrow_partial_liq_factor(0.65) == pytest.approx(0.825, abs=1e-9)


def test_full_factor_065():
    assert borrow_full_liq_factor(0.65) == pytest.approx(0.65 + 0.35 * 2 / 3, abs=1e-9)


def test_legacy_ltv_050_partial_factor_is_075():
    """Backward-consistency: LTV 0.50 → partial factor 0.75 (legacy value)."""
    assert borrow_partial_liq_factor(0.50) == pytest.approx(0.75, abs=1e-9)


def test_threshold_derives_from_formula_by_default():
    assert _liq_threshold_for_ltv(0.65) == pytest.approx(0.825, abs=1e-9)
    assert _liq_threshold_for_ltv(0.50) == pytest.approx(0.75, abs=1e-9)


# ------------------------------------------------------------------ ladder pins
@pytest.fixture()
def ladder():
    return compute_borrow_liq_ladder(DEBT, QTY, LTV)


def test_pin_partial_price(ladder):
    assert ladder["partial_price"] == pytest.approx(36.93, abs=0.01)


def test_pin_full_price(ladder):
    assert ladder["full_price"] == pytest.approx(34.49, abs=0.01)


def test_pin_early_warning(ladder):
    # Conservative early-warning = partial / 0.95 (5% threshold haircut,
    # fires ABOVE the partial price).
    assert ladder["early_price"] == pytest.approx(38.87, abs=0.02)
    assert ladder["early_price"] > ladder["partial_price"]


def test_pin_action(ladder):
    assert ladder["action_price"] == pytest.approx(40.62, abs=0.02)


def test_pin_observation(ladder):
    assert ladder["observation_price"] == pytest.approx(44.32, abs=0.02)


def test_ladder_ordering(ladder):
    assert (
        ladder["observation_price"]
        > ladder["action_price"]
        > ladder["early_price"]
        > ladder["partial_price"]
        > ladder["full_price"]
    )


def test_ladder_factors_reported(ladder):
    assert ladder["partial_factor"] == pytest.approx(0.825, abs=1e-9)
    assert ladder["full_factor"] == pytest.approx(0.65 + 0.35 * 2 / 3, abs=1e-9)


# ------------------------------------------------------------------ integration
def test_compute_pm_risk_metrics_snapshot_pin():
    """Risk-metrics core on the Aug-11 snapshot: partial ≈ 36.94 (incl. +20
    min-borrow offset), full ≈ 34.50, HF at partial start = 1.0."""
    from modules import portfolio_margin as pm

    px = 40.0
    metrics = pm.compute_pm_risk_metrics(
        {"HYPE": QTY * px},
        DEBT,
        QTY,
        px,
        ltv_map={"HYPE": LTV},
    )
    assert metrics["liq_price"] == pytest.approx(36.94, abs=0.05)
    assert metrics["liq_price_real"] == pytest.approx(metrics["liq_price"], abs=1e-9)
    assert metrics["liq_price_full"] == pytest.approx(34.50, abs=0.05)
    assert metrics["hf_at_real_liq"] == pytest.approx(1.0, abs=1e-9)
    assert metrics["liq_early_price"] == pytest.approx(
        metrics["liq_price"] / 0.95, abs=0.01
    )
    assert metrics["liq_action_price"] == pytest.approx(
        metrics["liq_price"] * 1.10, abs=0.01
    )
    assert metrics["liq_obs_price"] == pytest.approx(
        metrics["liq_price"] * 1.20, abs=0.01
    )
