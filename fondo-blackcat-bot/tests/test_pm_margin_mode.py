"""R-PM-MARGIN-MODE-FIX (2026-06-07) — per-leg margin-mode awareness.

The fund opened a 6-leg short equities basket on HyperLiquid HIP-3 intending
all legs CROSS, but HL only allows xyz:MRVL and xyz:HOOD as ISOLATED, so the
live basket is MIXED MARGIN:
  • CROSS  (share the PM pool w/ HYPE collateral): SP500, XYZ100, NVDA, MU
  • ISOLATED (walled off, own margin + own liq price): MRVL, HOOD

Correct model:
  * Cross-pool math (borrow utilisation, head-room, aave-HF, HYPE liq price)
    must include ONLY the cross legs. Isolated PnL must NOT move those numbers.
  * Isolated legs are reported in a SEPARATE subsection w/ own margin + liq.
  * The hedge framing still totals ALL short notional but annotates the split.
  * The naked-long guard counts both cross & isolated shorts as "hedged".
  * Margin mode is READ LIVE (leverage.type), never hardcoded by ticker.

T1-T7 below. These FAIL on a460907 (pre-fix) and PASS after the fix.
"""
from __future__ import annotations

import pytest

from modules.portfolio_margin import compute_pm_state, format_pm_state_telegram
from modules.margin_mode import (
    position_margin_mode,
    cross_perp_maint_margin,
    build_isolated_legs,
)

# ── Live-style HYPE collateral (wallet 0xc7ae, 2026-06-07) ───────────────────
HYPE_QTY = 1317.0
DEBT = 39_800.0
PX = 59.77


def _spot(debt=DEBT, hype=HYPE_QTY):
    return [
        {"coin": "USDC", "total": -(debt * 0.27), "borrowed": debt},
        {"coin": "HYPE", "total": hype, "supplied": hype, "ltv": 0.5},
    ]


def _cross_leg(coin, notional, *, maxlev=10.0):
    """A CROSS short leg (shares the PM pool)."""
    return {
        "coin": coin,
        "size": -100.0,
        "side": "SHORT",
        "notional_usd": notional,
        "entry_px": notional / 100.0,
        "unrealized_pnl": 0.0,
        "leverage_type": "cross",
        "max_leverage": maxlev,
    }


def _iso_leg(coin, notional, *, margin, liq_px, entry, upnl, maxlev=10.0):
    """An ISOLATED short leg (walled off, own margin + own liq price)."""
    return {
        "coin": coin,
        "size": -100.0,
        "side": "SHORT",
        "notional_usd": notional,
        "entry_px": entry,
        "unrealized_pnl": upnl,
        "leverage_type": "isolated",
        "margin_used": margin,
        "liq_px": liq_px,
        "max_leverage": maxlev,
    }


def _cross_basket():
    return [
        _cross_leg("xyz:SP500", 6_000.0),
        _cross_leg("xyz:XYZ100", 5_000.0),
        _cross_leg("xyz:NVDA", 4_000.0),
        _cross_leg("xyz:MU", 3_000.0),
    ]


def _iso_basket(*, mrvl_upnl=0.0, mrvl_notional=2_500.0, mrvl_liq=130.0):
    return [
        _iso_leg("xyz:MRVL", mrvl_notional, margin=500.0, liq_px=mrvl_liq,
                 entry=100.0, upnl=mrvl_upnl),
        _iso_leg("xyz:HOOD", 2_000.0, margin=400.0, liq_px=95.0,
                 entry=70.0, upnl=120.0),
    ]


# ── T1. mixed-margin pool EXCLUSION: isolated PnL must not move cross math ────
def test_t1_mixed_margin_pool_exclusion():
    prices = {"HYPE": PX}
    winning = _cross_basket() + _iso_basket(mrvl_upnl=+4_000.0, mrvl_notional=2_500.0)
    losing = _cross_basket() + _iso_basket(mrvl_upnl=-4_000.0, mrvl_notional=9_000.0,
                                           mrvl_liq=105.0)

    pm_win = compute_pm_state(_spot(), winning, prices)
    pm_lose = compute_pm_state(_spot(), losing, prices)

    # The CROSS-pool numbers are IDENTICAL whether the isolated legs win or lose.
    assert pm_win.ratio == pytest.approx(pm_lose.ratio)
    assert pm_win.available_usd == pytest.approx(pm_lose.available_usd)
    assert pm_win.aave_hf == pytest.approx(pm_lose.aave_hf)
    assert pm_win.health_factor == pytest.approx(pm_lose.health_factor)
    assert pm_win.liq_price == pytest.approx(pm_lose.liq_price)
    assert pm_win.perp_cross_mm == pytest.approx(pm_lose.perp_cross_mm)
    # And the cross MM reflects ONLY the 4 cross legs (isolated excluded).
    expected_cross_mm = cross_perp_maint_margin(_cross_basket())
    assert pm_win.perp_cross_mm == pytest.approx(expected_cross_mm)
    assert expected_cross_mm > 0  # the cross legs DO contribute


# ── T2. isolated leg liquidation INDEPENDENCE ────────────────────────────────
def test_t2_isolated_liq_independence():
    prices = {"HYPE": PX}
    base = _cross_basket() + _iso_basket(mrvl_upnl=0.0, mrvl_liq=130.0)
    stressed = _cross_basket() + _iso_basket(
        mrvl_upnl=-6_000.0, mrvl_notional=9_000.0, mrvl_liq=104.0
    )

    pm_base = compute_pm_state(_spot(), base, prices)
    pm_stress = compute_pm_state(_spot(), stressed, prices)

    # The HYPE collateral liq price and the PM ratio do NOT change when an
    # isolated leg is driven toward its own liquidation.
    assert pm_stress.liq_price == pytest.approx(pm_base.liq_price)
    assert pm_stress.ratio == pytest.approx(pm_base.ratio)
    assert pm_stress.aave_hf == pytest.approx(pm_base.aave_hf)

    # The isolated leg's OWN liq price + distance is reported in the subsection.
    legs = {l.coin: l for l in pm_stress.isolated_positions}
    assert "xyz:MRVL" in legs
    assert legs["xyz:MRVL"].liq_px == pytest.approx(104.0)
    assert legs["xyz:MRVL"].distance_to_liq_pct > 0


# ── T3. ISOLATED subsection rendering + NO double-count in cross MM ───────────
def test_t3_isolated_subsection_rendering():
    prices = {"HYPE": PX}
    positions = _cross_basket() + _iso_basket()
    pm = compute_pm_state(_spot(), positions, prices)
    block = format_pm_state_telegram(pm)

    assert "ISOLATED POSITIONS" in block
    for coin in ("MRVL", "HOOD"):
        assert coin in block
    # Each isolated leg shows notional, posted isolated margin, its liq price,
    # distance-to-liq, and UPnL.
    assert "iso-margin" in block
    assert "liq $" in block
    assert "% away" in block
    assert "UPnL" in block

    # The isolated legs are NOT folded into the cross maintenance margin.
    cross_only_mm = cross_perp_maint_margin(_cross_basket())
    assert pm.perp_cross_mm == pytest.approx(cross_only_mm)
    # Sanity: adding the isolated legs back does NOT change the cross MM.
    assert cross_perp_maint_margin(positions) == pytest.approx(cross_only_mm)


# ── T4. hedge annotation + naked-long guard reports HEDGED ────────────────────
def test_t4_hedge_annotation_and_not_naked():
    prices = {"HYPE": PX}
    positions = _cross_basket() + _iso_basket()
    pm = compute_pm_state(_spot(), positions, prices)
    block = format_pm_state_telegram(pm)

    # Total hedge notional = ALL 6 legs (cross + isolated).
    total = sum(abs(p["notional_usd"]) for p in positions)
    assert pm.shorts_notional == pytest.approx(total)
    assert pm.cross_shorts_notional + pm.isolated_shorts_notional == pytest.approx(total)
    assert pm.cross_shorts_notional > 0 and pm.isolated_shorts_notional > 0

    # The hedge line annotates the cross vs isolated split.
    assert "Hedge (shorts basket)" in block
    assert "cross" in block and "isolated" in block and "walled-off" in block

    # Naked-long guard: debt is hedged (shorts present) → NOT naked.
    assert pm.naked_long is False
    assert "naked leveraged long" not in block


# ── T5. margin-mode is READ, not hardcoded (MRVL as CROSS) ────────────────────
def test_t5_margin_mode_read_not_hardcoded():
    prices = {"HYPE": PX}
    # Synthetic clearinghouse state where MRVL is CROSS (HL could allow it later).
    mrvl_cross = dict(_iso_leg("xyz:MRVL", 2_500.0, margin=500.0, liq_px=130.0,
                               entry=100.0, upnl=0.0))
    mrvl_cross["leverage_type"] = "cross"
    positions = _cross_basket() + [mrvl_cross,
                                   _iso_leg("xyz:HOOD", 2_000.0, margin=400.0,
                                            liq_px=95.0, entry=70.0, upnl=120.0)]

    assert position_margin_mode(mrvl_cross) == "cross"
    pm = compute_pm_state(_spot(), positions, prices)

    # MRVL is now treated as a CROSS leg: NOT in the isolated subsection.
    iso_coins = {l.coin for l in pm.isolated_positions}
    assert "xyz:MRVL" not in iso_coins
    assert "xyz:HOOD" in iso_coins
    assert pm.isolated_perp_count == 1
    assert pm.cross_perp_count == 5

    # Its maintenance margin now folds into the cross pool (proves no hardcoded
    # isolated list): cross MM is higher than the 4-leg-only baseline.
    base_cross_mm = cross_perp_maint_margin(_cross_basket())
    assert pm.perp_cross_mm > base_cross_mm


# ── T6. FULL ANALYSIS paragraph relabel (build_fund_state_block) ──────────────
def test_t6_full_analysis_relabel():
    """The LLM-facing PM context (which drives the FULL ANALYSIS "REPORTE
    DIARIO" PORTFOLIO MARGIN paragraph) must no longer frame borrow utilization
    as liquidation. NOTE: no PRE-EXISTING assertion locked the old "CRÍTICO /
    pre-liquidación" borrow-ratio text — test_reporte_llm_context_clean only
    locks "PM MARGIN-RATIO THRESHOLDS" / "0.85" / "naked-long", all preserved —
    so no existing assertion needed updating for this fix.
    """
    import os
    os.environ["FLYWHEEL_DEPRECATED"] = "true"
    import importlib
    import config
    importlib.reload(config)
    from templates import system_prompt
    importlib.reload(system_prompt)
    block = system_prompt.build_fund_state_block()
    low = block.lower()

    # Borrow-utilization-as-liquidation framing is GONE.
    assert "pre-liquidación" not in low
    assert "→ crítico" not in low  # the old "ratio ≥ 0.85 → CRÍTICO" line
    assert "liquidación inminente" not in low

    # The corrected framing is present.
    assert "borrow utilization" in low
    assert "max-borrow" in low
    assert "over max-borrow" in low
    assert "aave-hf" in low
    assert "liq price" in low
    # Real liquidation language is reserved for aave-HF approaching 1.0 / 0.95.
    assert "portfolio_margin_ratio" in low


# ── T7. all-cross sanity: reduces to the pure cross-pool math ─────────────────
def test_t7_all_cross_sanity():
    prices = {"HYPE": PX}
    # All 6 legs CROSS.
    all_cross = _cross_basket() + [
        _cross_leg("xyz:MRVL", 2_500.0),
        _cross_leg("xyz:HOOD", 2_000.0),
    ]
    pm = compute_pm_state(_spot(), all_cross, prices)

    # No leg is excluded: empty isolated subsection.
    assert pm.isolated_positions == ()
    assert pm.isolated_perp_count == 0
    assert pm.cross_perp_count == 6
    assert pm.isolated_shorts_notional == pytest.approx(0.0)
    # The hedge is entirely in the cross pool.
    total = sum(abs(p["notional_usd"]) for p in all_cross)
    assert pm.cross_shorts_notional == pytest.approx(total)
    assert pm.shorts_notional == pytest.approx(total)

    # The result equals computing with perp_cross_mm explicitly set to the sum
    # of ALL legs' maintenance margin (the pure cross-pool baseline).
    explicit_mm = cross_perp_maint_margin(all_cross)
    pm_explicit = compute_pm_state(_spot(), [], prices, perp_cross_mm=explicit_mm)
    assert pm.perp_cross_mm == pytest.approx(pm_explicit.perp_cross_mm)
    assert pm.aave_hf == pytest.approx(pm_explicit.aave_hf)
    assert pm.liq_price == pytest.approx(pm_explicit.liq_price)
    assert pm.ratio == pytest.approx(pm_explicit.ratio)
