"""R-PM-RATIO-RELABEL (2026-06-07) — utilization is NOT a liquidation signal.

Reproduces the live 2026-06-07 false alarm: the PM panel printed
``Margin ratio: 101.1% 🔴 LIQ-RISK`` (borrow-cap utilization) right next to a
green aave-HF 1.48 and liq price $40.32. Utilization > 100% means "over the
max-borrow cap / no new draws", NOT "near liquidation". The headline risk must
be the aave-HF; the utilization line must never be red and must never carry
"LIQ-RISK".
"""
from __future__ import annotations

import pytest

from modules.portfolio_margin import (
    compute_pm_state,
    compute_pm_risk_metrics,
    format_pm_state_telegram,
)
from modules.pm_panel import (
    headline_color,
    borrow_utilization_status,
    explainer_line,
)

# Live 2026-06-07 snapshot for wallet 0xc7ae.
HYPE_QTY = 1317.0
DEBT = 39_800.0
PX = 59.77


def _spot(debt=DEBT, hype=HYPE_QTY):
    return [
        {"coin": "USDC", "total": -(debt * 0.27), "borrowed": debt},
        {"coin": "HYPE", "total": hype, "supplied": hype, "ltv": 0.5},
    ]


# ── T1. over-cap, NOT liquidation: green headline, no red, no LIQ-RISK ────────
def test_t1_over_cap_not_liq():
    pm = compute_pm_state(_spot(), [], {"HYPE": PX})
    block = format_pm_state_telegram(pm)
    # Sanity on the live numbers.
    assert pm.ratio * 100 == pytest.approx(101.1, abs=1.0)   # ~101% utilization
    assert pm.aave_hf == pytest.approx(1.48, abs=0.05)        # healthy
    # Headline is the aave-HF, GREEN.
    assert headline_color(pm.aave_hf, has_debt=True) == "🟢"
    assert "SALUDABLE" in block and "🟢" in block
    # Borrow utilization line: OVER MAX-BORROW, NOT red, NO LIQ-RISK.
    assert "Borrow utilization (vs 50% max-borrow)" in block
    assert "OVER MAX-BORROW" in block
    # The false-alarm strings must be GONE from the whole panel.
    assert "LIQ-RISK" not in block
    assert "🔴" not in block
    # The old WARN/STRESS/CRÍTICO/LIQ utilization scale is removed.
    assert "CRÍTICO 85%" not in block
    assert "LIQ 95%" not in block


# ── T2. real-liq-approach: HF drives a RED headline, independent of util ──────
def test_t2_real_liq_approach_red_headline():
    # Drop the oracle until aave_HF < 1.10.
    pm = compute_pm_state(_spot(), [], {"HYPE": 44.0})
    assert pm.aave_hf < 1.10
    assert headline_color(pm.aave_hf, has_debt=True) == "🔴"
    # Utilization is even HIGHER here (>130%) but that is NOT what makes it red.
    assert pm.ratio * 100 > 130
    label, is_red = borrow_utilization_status(pm.ratio * 100)
    assert is_red is False                     # utilization never red
    assert "OVER MAX-BORROW" in label


# ── T3. healthy with headroom: green headline + "borrow headroom OK" ──────────
def test_t3_healthy_with_headroom():
    pm = compute_pm_state(_spot(debt=10_000.0), [], {"HYPE": PX})
    assert pm.ratio * 100 < 90
    block = format_pm_state_telegram(pm)
    assert headline_color(pm.aave_hf, has_debt=True) == "🟢"
    assert "borrow headroom OK" in block
    assert "LIQ-RISK" not in block and "🔴" not in block


# ── T4. liq-price math: debt / (hype × 0.75) ≈ $40.30 ────────────────────────
def test_t4_liq_price_math():
    pm = compute_pm_state(_spot(), [], {"HYPE": PX})
    expected = DEBT / (HYPE_QTY * 0.75)
    assert expected == pytest.approx(40.3, abs=0.2)
    assert pm.liq_price == pytest.approx(expected, abs=0.2)


# ── T5. param-driven thresholds: liq_threshold = 0.5 + 0.5×ltv follows ───────
def test_t5_param_driven_thresholds():
    breakdown = {"HYPE": 100_000.0}
    m50 = compute_pm_risk_metrics(
        breakdown, debt=40_000.0, hype_qty=1000.0, hype_px=100.0,
        ltv_map={"HYPE": 0.50},
    )
    m70 = compute_pm_risk_metrics(
        breakdown, debt=40_000.0, hype_qty=1000.0, hype_px=100.0,
        ltv_map={"HYPE": 0.70},
    )
    assert m50["liq_threshold"] == pytest.approx(0.75)        # 0.5 + 0.5×0.50
    assert m70["liq_threshold"] == pytest.approx(0.85)        # 0.5 + 0.5×0.70
    assert m50["max_ltv"] == pytest.approx(0.50)
    assert m70["max_ltv"] == pytest.approx(0.70)
    # Downstream aave_HF follows the threshold (no hardcoded 0.75/0.50).
    assert m70["aave_hf"] > m50["aave_hf"]
    assert m70["liq_price"] < m50["liq_price"]


# ── T6. label-guard: utilization status never "LIQ" / never red ──────────────
def test_t6_label_guard():
    for util in (95.0, 100.0, 150.0):
        label, is_red = borrow_utilization_status(util)
        assert "LIQ" not in label
        assert is_red is False
    assert borrow_utilization_status(95.0)[0] == "NEAR MAX-BORROW: limited new draws"
    assert borrow_utilization_status(100.0)[0] == "NEAR MAX-BORROW: limited new draws"
    assert borrow_utilization_status(150.0)[0].startswith("OVER MAX-BORROW")
    assert borrow_utilization_status(50.0)[0] == "borrow headroom OK"


def test_explainer_line_mentions_liq_not_util():
    line = explainer_line(40.31)
    assert "40.3" in line
    assert "LIQ-RISK" not in line and "🔴" not in line
