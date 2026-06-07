"""R-PM-LIQ (2026-06-06) — real Portfolio Margin liquidation price + aave-HF.

The bug
-------
``compute_pm_state`` used the BORROW LTV (0.50) as if it were the maintenance
threshold, so:
  * the liquidation price was ~50% too HIGH ($60.45 vs the real ~$40.32), and
  * the only "health" number was the borrow utilisation (capacity/debt ≈ 0.94),
    which reads as "🔴 over the cap" even when the position is FAR from
    liquidation.

The fix derives the maintenance threshold ``liq_threshold = 0.5 + 0.5×ltv``
(0.75 for a 0.5-LTV asset like HYPE), exposes the aave-style health factor
(Σ value×liq_threshold / debt) as the RISK metric, and computes the REAL
liquidation price ``(debt + offset)/(liq_threshold × tokens)``. The borrow
utilisation is kept as ``health_factor`` (HF_app), clearly relabelled.

Ground truth (6-Jun-2026, Rabby): 1317.0252 HYPE, USDC borrowed $39,807.72.
  * HF_app (capacity/debt)      ≈ 0.96   (borrow utilisation, NOT liq)
  * aave_HF (liq-threshold)     ≈ 1.44   (the real risk → 🟢 SALUDABLE)
  * real liq price (HYPE)       ≈ $40.32 (NOT $60.45)
"""
from __future__ import annotations

import pytest

from modules.portfolio_margin import (
    compute_pm_state,
    compute_pm_risk_metrics,
    format_pm_state_telegram,
    risk_tier,
    _liq_threshold_for_ltv,
)

HYPE_QTY = 1317.0252
BORROWED = 39_807.72
PX = 58.07


# ─── 1. Maintenance threshold formula ───────────────────────────────────────
def test_liq_threshold_is_half_plus_half_ltv():
    assert _liq_threshold_for_ltv(0.50) == pytest.approx(0.75)
    assert _liq_threshold_for_ltv(0.70) == pytest.approx(0.85)
    assert _liq_threshold_for_ltv(0.0) == pytest.approx(0.75)   # bad → default
    assert _liq_threshold_for_ltv("x") == pytest.approx(0.75)   # garbage → default


# ─── 2. Real liq price uses 0.75, NOT the 0.50 borrow LTV ───────────────────
def test_liq_price_uses_maintenance_threshold_not_borrow_ltv():
    spot = [
        {"coin": "USDC", "total": -10_740.32, "borrowed": BORROWED},
        {"coin": "HYPE", "total": HYPE_QTY, "supplied": HYPE_QTY, "ltv": 0.5},
    ]
    pm = compute_pm_state(spot, [], {"HYPE": PX})
    # The OLD wrong value was debt/(qty×0.5) ≈ $60.45 — must NOT reappear.
    wrong_old = BORROWED / (HYPE_QTY * 0.5)
    assert abs(wrong_old - 60.45) < 0.5
    assert abs(pm.liq_price - wrong_old) > 15.0
    # The REAL value: (debt + 20) / (0.75 × qty) ≈ $40.32.
    expected = (BORROWED + 20.0) / (0.75 * HYPE_QTY)
    assert pm.liq_price == pytest.approx(expected, abs=0.05)
    assert 39.0 < pm.liq_price < 42.0


# ─── 3. aave_HF is the risk metric; HF_app is borrow utilisation ────────────
def test_aave_hf_and_hf_app_separated():
    spot = [
        {"coin": "USDC", "total": -10_740.32, "borrowed": BORROWED},
        {"coin": "HYPE", "total": HYPE_QTY, "supplied": HYPE_QTY, "ltv": 0.5},
    ]
    pm = compute_pm_state(spot, [], {"HYPE": PX})
    collateral = HYPE_QTY * PX
    # HF_app = capacity/debt = (collateral×0.5)/debt ≈ 0.96.
    assert pm.health_factor == pytest.approx(collateral * 0.5 / BORROWED, abs=1e-3)
    assert 0.92 <= pm.health_factor <= 1.00
    # aave_HF = (collateral×0.75)/debt ≈ 1.44 → 🟢 SALUDABLE risk band.
    assert pm.aave_hf == pytest.approx(collateral * 0.75 / BORROWED, abs=1e-3)
    assert pm.aave_hf > 1.30
    assert pm.risk_emoji == "🟢"
    assert pm.risk_label == "SALUDABLE"
    # current LTV ≈ debt/collateral ≈ 0.52, buffer > 0.
    assert pm.current_ltv == pytest.approx(BORROWED / collateral, abs=1e-3)
    assert pm.price_buffer_pct > 0
    assert pm.liq_threshold == pytest.approx(0.75)
    assert pm.max_ltv == pytest.approx(0.50)


# ─── 4. Risk band is aave_HF-driven, NOT the borrow ratio ───────────────────
def test_risk_tier_bands():
    assert risk_tier(0.0, has_debt=False) == ("🟢", "SIN DEUDA")
    assert risk_tier(2.0, has_debt=True)[0] == "🟢"     # SALUDABLE
    assert risk_tier(1.30, has_debt=True)[0] == "🟢"
    assert risk_tier(1.29, has_debt=True) == ("🟡", "WATCH")
    assert risk_tier(1.15, has_debt=True) == ("🟡", "WATCH")
    assert risk_tier(1.14, has_debt=True) == ("🟠", "ALERTA")
    assert risk_tier(1.05, has_debt=True) == ("🟠", "ALERTA")
    assert risk_tier(1.04, has_debt=True) == ("🔴", "CRÍTICO")
    assert risk_tier(1.00, has_debt=True) == ("🔴", "CRÍTICO")
    assert risk_tier(0.99, has_debt=True) == ("⛔", "LIQUIDABLE")


def test_status_ratio_invariant_but_risk_is_green():
    """R-PMCORE: ``status``/``ratio`` (borrow utilisation) are UNCHANGED — the
    live case is still LIQ on the ratio band — yet the aave risk is 🟢 because
    the position is far from the maintenance liquidation point."""
    spot = [
        {"coin": "USDC", "total": -10_740.32, "borrowed": BORROWED},
        {"coin": "HYPE", "total": HYPE_QTY, "supplied": HYPE_QTY, "ltv": 0.5},
    ]
    pm = compute_pm_state(spot, [], {"HYPE": PX})
    assert pm.status == "LIQ"          # ratio 1.04 → R-PMCORE classifier
    assert pm.ratio > 0.95
    assert pm.risk_emoji == "🟢"        # but the REAL liq risk is healthy


# ─── 5. Cross perp maintenance margin raises the liq price ──────────────────
def test_cross_perp_mm_raises_liq_and_lowers_aave_hf():
    spot = [
        {"coin": "USDC", "total": -10_740.32, "borrowed": BORROWED},
        {"coin": "HYPE", "total": HYPE_QTY, "supplied": HYPE_QTY, "ltv": 0.5},
    ]
    base = compute_pm_state(spot, [], {"HYPE": PX})
    crossed = compute_pm_state(spot, [], {"HYPE": PX}, perp_cross_mm=5_000.0)
    # Folding $5K of cross perp maintenance margin into the liability pushes the
    # liquidation price UP and the aave HF DOWN.
    assert crossed.liq_price > base.liq_price
    assert crossed.aave_hf < base.aave_hf
    assert crossed.perp_cross_mm == pytest.approx(5_000.0)


# ─── 6. Generic multi-collateral support ────────────────────────────────────
def test_multi_collateral_metrics():
    # HYPE (ltv .50 → maint .75) + a hypothetical asset (ltv .70 → maint .85).
    breakdown = {"HYPE": 50_000.0, "WBTC": 50_000.0}
    m = compute_pm_risk_metrics(
        breakdown, debt=40_000.0, hype_qty=1000.0, hype_px=50.0,
        ltv_map={"HYPE": 0.50, "WBTC": 0.70},
    )
    # capacity = 50k×.5 + 50k×.7 = 60k ; liq_weighted = 50k×.75 + 50k×.85 = 80k.
    assert m["borrow_capacity"] == pytest.approx(60_000.0)
    assert m["liq_weighted"] == pytest.approx(80_000.0)
    assert m["aave_hf"] == pytest.approx(80_000.0 / 40_000.0)   # 2.0
    assert m["hf_app"] == pytest.approx(60_000.0 / 40_000.0)    # 1.5
    # HYPE liq price holds the WBTC leg (50k×.85 = 42.5k) constant:
    #   target = (debt+20) - 42_500 = -2_480  → already covered → liq price ~0.
    assert m["liq_price"] == pytest.approx(0.0)


def test_multi_collateral_liq_price_when_hype_dominant():
    breakdown = {"HYPE": 70_000.0, "WBTC": 10_000.0}
    m = compute_pm_risk_metrics(
        breakdown, debt=50_000.0, hype_qty=1000.0, hype_px=70.0,
        ltv_map={"HYPE": 0.50, "WBTC": 0.70},
    )
    # other_liq (WBTC) = 10_000×0.85 = 8_500.
    # target = (50_000 + 20) - 8_500 = 41_520 ; liq = 41_520/(0.75×1000) = 55.36.
    assert m["liq_price"] == pytest.approx(41_520.0 / (0.75 * 1000.0), abs=0.05)


# ─── 7. Telegram block: real risk legible, naked-long kept distinct ─────────
def test_block_renders_real_liq_and_aave_hf():
    spot = [
        {"coin": "USDC", "total": -10_740.32, "borrowed": BORROWED},
        {"coin": "HYPE", "total": HYPE_QTY, "supplied": HYPE_QTY, "ltv": 0.5},
    ]
    block = format_pm_state_telegram(compute_pm_state(spot, [], {"HYPE": PX}))
    assert "Health factor (aave" in block
    assert "SALUDABLE" in block and "🟢" in block          # aave-driven headline
    assert "Utilización borrow" in block                   # HF_app relabelled
    assert "Liq. price HYPE" in block and "$40.3" in block  # REAL liq price
    assert "buffer" in block
    assert "$60.45" not in block                            # the OLD wrong price
    # Naked-long is a SEPARATE hedge-missing note, NOT conflated with liq risk.
    assert "naked leveraged long" in block.lower()
    # R-PMALERT display scale untouched (regression guard).
    assert "CRÍTICO 85%" in block and "LIQ 95%" in block and "LIQ-RISK" in block


def test_second_oracle_case_hf_app_096():
    """Second snapshot (oracle $58.07) → HF_app ≈ 0.9606 (sanity on the
    utilisation framing remaining a borrow ratio, not the liq metric)."""
    spot = [
        {"coin": "USDC", "total": -10_740.32, "borrowed": BORROWED},
        {"coin": "HYPE", "total": HYPE_QTY, "supplied": HYPE_QTY, "ltv": 0.5},
    ]
    pm = compute_pm_state(spot, [], {"HYPE": 58.07})
    assert pm.health_factor == pytest.approx(0.9606, abs=0.002)
    assert pm.aave_hf == pytest.approx(1.4409, abs=0.003)


# ─── 8. No-debt path stays clean (no false risk band) ───────────────────────
def test_no_debt_no_risk_band():
    spot = [{"coin": "HYPE", "total": 1000.0}, {"coin": "USDC", "total": 0.5}]
    pm = compute_pm_state(spot, [], {"HYPE": 70.0})
    assert pm.debt_usd == 0.0
    assert pm.liq_price == 0.0
    assert pm.aave_hf == 0.0
    assert pm.health_factor == 0.0
    assert pm.risk_label == "SIN DEUDA"
