"""R-BOT-DEFINITIVE WI-7 — PM threshold prices (outputs only, panel parity)."""
from __future__ import annotations

import pytest

from modules.portfolio_margin import (
    PMState,
    compute_pm_risk_metrics,
    compute_pm_state,
    format_pm_state_telegram,
)


def _state(hype_qty=800.0, hype_px=53.0, debt=20_000.0, cross_mm=800.0):
    spot = [
        {"coin": "HYPE", "total": hype_qty},
        {"coin": "USDC", "total": -1000.0, "borrowed": debt},
    ]
    return compute_pm_state(
        spot, [], {"HYPE": hype_px},
        ltv_map={"HYPE": 0.50}, perp_cross_mm=cross_mm,
    )


def test_threshold_prices_solve_the_hf_equation():
    """px_at_hf must reproduce the target aave-HF when re-fed to the metrics."""
    pm = _state()
    for target, px in (
        (1.30, pm.hype_price_at_hf_130),
        (1.20, pm.hype_price_at_hf_120),
        (1.10, pm.hype_price_at_hf_110),
    ):
        assert px > 0
        m = compute_pm_risk_metrics(
            {"HYPE": pm.hype_qty * px}, pm.debt_usd, pm.hype_qty, px,
            ltv_map={"HYPE": 0.50}, perp_cross_mm=pm.perp_cross_mm,
        )
        assert m["aave_hf"] == pytest.approx(target, abs=0.005)


def test_threshold_ordering_and_liq_below_action():
    pm = _state()
    assert pm.hype_price_at_hf_130 > pm.hype_price_at_hf_120 > pm.hype_price_at_hf_110
    # The maintenance liq price sits below the HF-1.10 action price.
    assert pm.liq_price < pm.hype_price_at_hf_110


def test_same_basis_as_aave_hf_includes_cross_mm():
    """The thresholds use debt + perp cross maint margin (the aave-HF basis)."""
    no_mm = _state(cross_mm=0.0)
    with_mm = _state(cross_mm=2000.0)
    assert with_mm.hype_price_at_hf_120 > no_mm.hype_price_at_hf_120


def test_no_debt_no_thresholds():
    spot = [{"coin": "HYPE", "total": 800.0}]
    pm = compute_pm_state(spot, [], {"HYPE": 53.0}, ltv_map={"HYPE": 0.50},
                          perp_cross_mm=0.0)
    assert pm.hype_price_at_hf_120 == 0.0
    assert pm.hype_price_at_hf_110 == 0.0


def test_panel_prints_threshold_line():
    pm = _state()
    panel = format_pm_state_telegram(pm)
    assert "Umbrales HYPE:" in panel
    assert f"HF1.20 ${pm.hype_price_at_hf_120:,.2f} (observación)" in panel
    assert f"HF1.10 ${pm.hype_price_at_hf_110:,.2f} (acción)" in panel
    assert f"liq ${pm.liq_price:,.2f}" in panel


def test_pm_context_injects_identical_values_and_forbids_derivation():
    """WI-7 acceptance: panel and narrative carry IDENTICAL threshold prices."""
    from modules.pm_context import build_pm_llm_block
    pm = _state()
    block = build_pm_llm_block(pm)
    panel = format_pm_state_telegram(pm)
    for px in (pm.hype_price_at_hf_120, pm.hype_price_at_hf_110):
        s = f"${px:,.2f}"
        assert s in block and s in panel
    assert "PROHIBIDO derivar otras zonas" in block


def test_existing_math_untouched():
    """OUTPUTS ONLY: aave_hf / liq_price / ratio math is byte-identical."""
    pm = _state()
    m = compute_pm_risk_metrics(
        {"HYPE": pm.hype_qty * pm.hype_px}, pm.debt_usd, pm.hype_qty, pm.hype_px,
        ltv_map={"HYPE": 0.50}, perp_cross_mm=pm.perp_cross_mm,
    )
    assert pm.aave_hf == pytest.approx(m["aave_hf"])
    assert pm.liq_price == pytest.approx(m["liq_price"])
