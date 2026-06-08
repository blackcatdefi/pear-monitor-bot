"""R-FULLANALYSIS-PM-TRUTH (2026-06-08) — regression tests.

The FULL ANALYSIS narrative (the long Spanish "REPORTE DIARIO" the LLM writes)
used to recompute Portfolio Margin health ON ITS OWN, with the WRONG formula,
contradicting the already-correct DESTACADO panel. Live 2026-06-08 13:54 UTC:

  panel  : aave-HF 1.58 SALUDABLE · util 93% · liq HYPE $40.79
  LLM text: "aave-HF estimado: capacidad / deuda = $42,440 / $39,431 = 1.076 …
             ZONA DE RIESGO REAL … liq HYPE = $39,431 / (1,317 × 0.50) = $59.89
             … PRIORIDAD #1 repagar deuda urgente"

Root cause: the LLM got only the raw portfolio JSON, no pre-computed PM block,
so it inverted borrow utilisation into the HF and used the 0.50 max-borrow LTV
(instead of the 0.75 maintenance threshold) for the liq price.

Fix (single source of truth): inject the PRE-COMPUTED PMState — the SAME
``compute_pm_state`` the panel uses — into the LLM user content via
``modules.pm_context.build_pm_llm_block`` and forbid the model from recomputing.

T1  narrative consumes the pre-computed HF/liq (no self-computed 1.076 / 59.89)
T2  no urgent language when the HF is healthy (but NEAR MAX-BORROW note allowed)
T3  real-risk language IS permitted when the HF is low
T4  SYSTEM_PROMPT no longer demonstrates the old capacity/debt HF or
    debt/(qty×0.50) liq-price worked example, and carries the no-recompute rule
T5  panel ↔ narrative parity: same HF and liq price in both surfaces
T6  liq_threshold stays param-driven (0.5 + 0.5×ltv); no hardcoded 0.75
T7  prior required prompt substrings intact + default-prompt HF prohibitions hold
"""
from __future__ import annotations

import os
import re

from modules.portfolio_margin import compute_pm_state, format_pm_state_telegram
from modules.pm_context import build_pm_llm_block


# Mirrors the live 0xc7ae state (HYPE collateral, USDC borrow, mixed basket).
def _live_like_pm():
    """Build a PMState close to the live 0xc7ae state via compute_pm_state.

    ~1,317 HYPE at ~$56 oracle ⇒ collateral ~$73.7K; ~$39.4K USDC borrowed.
    aave-HF = collateral × 0.75 / debt ≈ 1.40+. Uses the real engine so the
    fields are computed exactly the way the panel computes them.
    """
    spot = [
        {"coin": "HYPE", "total": 1317.0},
        {"coin": "USDC", "total": -39431.0, "borrowed": 39431.0},
    ]
    positions = [
        {"coin": "BTC", "size": 0.5, "notional_usd": 30000.0,
         "leverage_type": "isolated", "max_leverage": 5},
    ]
    prices = {"HYPE": 80.0}  # 1317 × 80 = $105,360 collateral
    return compute_pm_state(
        spot, positions, prices, ltv_map={"HYPE": 0.50}, perp_cross_mm=0.0,
    )


def _pm_with_hf(target_hf: float):
    """Construct a PMState whose aave-HF lands near ``target_hf``.

    aave_hf = (collateral × liq_threshold) / debt, liq_threshold = 0.75 for a
    0.50-LTV HYPE. Fix debt = $40,000, solve collateral = hf × debt / 0.75.
    """
    debt = 40000.0
    coll_value = target_hf * debt / 0.75
    px = 80.0
    qty = coll_value / px
    spot = [
        {"coin": "HYPE", "total": qty},
        {"coin": "USDC", "total": -debt, "borrowed": debt},
    ]
    # A short hedge so naked-long never fires (keeps the directive band-driven).
    positions = [
        {"coin": "ETH", "size": -5.0, "notional_usd": 15000.0,
         "leverage_type": "cross"},
    ]
    return compute_pm_state(
        spot, positions, {"HYPE": px}, ltv_map={"HYPE": 0.50}, perp_cross_mm=0.0,
    )


# ── T1 ──────────────────────────────────────────────────────────────────────
def test_t1_narrative_consumes_precomputed_hf_and_liq():
    """The LLM block carries the pre-computed aave-HF + liq, not the bug values."""
    pm = _live_like_pm()
    # Sanity: the engine produces a healthy HF and a 0.75-threshold liq price.
    assert pm.aave_hf >= 1.30, f"expected healthy HF, got {pm.aave_hf}"
    assert abs(pm.liq_threshold - 0.75) < 1e-9

    block = build_pm_llm_block(pm)
    assert block, "PM LLM block should be non-empty for a live-like PM state"

    # The exact pre-computed numbers must appear verbatim.
    assert f"{pm.aave_hf:.2f}" in block
    assert f"${pm.liq_price:,.2f}" in block

    # The buggy self-computed values must NOT appear.
    assert "1.076" not in block
    assert "59.89" not in block
    assert "$59.89" not in block
    # And no inverted-utilisation worked example.
    assert "capacidad / deuda =" not in block


# ── T2 ──────────────────────────────────────────────────────────────────────
def test_t2_no_urgent_language_when_healthy():
    """Healthy HF ⇒ no liquidation/urgency wording; NEAR MAX-BORROW note OK."""
    pm = _pm_with_hf(1.58)
    assert pm.aave_hf >= 1.30
    block = build_pm_llm_block(pm)

    for forbidden in (
        "PRIORIDAD #1",
        "ALERTA CRÍTICA",
        "ZONA DE RIESGO REAL",
        "repagar urgente",
        "urgente",
    ):
        assert forbidden not in block, f"urgent phrase leaked when healthy: {forbidden!r}"

    # A near-max-borrow note is still allowed when utilisation is high.
    high_util = _pm_with_hf(1.58)
    # Force utilisation into the 90-100% band by checking the helper directly:
    # the block must be capable of emitting the NEAR MAX-BORROW label.
    from modules.pm_panel import borrow_utilization_status
    label, is_red = borrow_utilization_status(93.0)
    assert label == "NEAR MAX-BORROW: limited new draws"
    assert is_red is False
    # And the reminder line names NEAR MAX-BORROW as an allowed (non-liq) state.
    assert "NEAR MAX-BORROW" in block


# ── T3 ──────────────────────────────────────────────────────────────────────
def test_t3_real_risk_language_allowed_when_low():
    """Low HF ⇒ real-risk wording (caution/critical) appears."""
    pm = _pm_with_hf(1.05)
    assert pm.aave_hf < 1.15
    block = build_pm_llm_block(pm)
    assert "RIESGO REAL" in block
    # The aave-HF risk label (CRÍTICO/ALERTA/LIQUIDABLE) should surface too.
    assert pm.risk_label in block


# ── T4 ──────────────────────────────────────────────────────────────────────
def test_t4_forbidden_formula_not_in_prompt():
    """SYSTEM_PROMPT must not demonstrate the old HF/liq worked formulas, and
    must carry the explicit no-recompute rule."""
    from templates.system_prompt import SYSTEM_PROMPT

    # Old worked-example patterns that would let the model pattern-match the bug.
    forbidden_patterns = [
        r"capacidad\s*/\s*deuda\s*=",          # inverted-HF worked example
        r"=\s*1\.076",                          # the buggy HF result
        r"\$?59\.89",                           # the buggy liq price
        r"/\s*\([^)]*×\s*0\.50\)",              # deuda / (qty × 0.50) liq formula
        r"/\s*\([^)]*x\s*0\.50\)",
        r"\$42,?440\s*/\s*\$?39,?431",          # the live worked division
    ]
    for pat in forbidden_patterns:
        assert not re.search(pat, SYSTEM_PROMPT), f"old PM formula example leaked: {pat}"

    # The new explicit prohibition must be present (RED before this change).
    low = SYSTEM_PROMPT.lower()
    assert "prohibido recalcular" in low
    assert "verbatim" in low
    assert "0.5+0.5×ltv" in SYSTEM_PROMPT or "0.5 + 0.5×ltv" in SYSTEM_PROMPT


# ── T5 ──────────────────────────────────────────────────────────────────────
def test_t5_panel_narrative_parity():
    """One PMState ⇒ identical HF and liq price in the panel and the LLM block."""
    pm = _live_like_pm()
    panel = format_pm_state_telegram(pm)
    block = build_pm_llm_block(pm)

    hf_str = f"{pm.aave_hf:.2f}"
    liq_str = f"${pm.liq_price:,.2f}"

    assert hf_str in panel, "panel should render the aave-HF"
    assert hf_str in block, "LLM block should render the same aave-HF"
    assert liq_str in panel, "panel should render the liq price"
    assert liq_str in block, "LLM block should render the same liq price"


# ── T6 ──────────────────────────────────────────────────────────────────────
def test_t6_param_driven_threshold_preserved():
    """liq_threshold tracks 0.5 + 0.5×ltv for any ltv; no hardcoded 0.75."""
    for ltv in (0.40, 0.50, 0.60):
        pm = compute_pm_state(
            [{"coin": "HYPE", "total": 1000.0},
             {"coin": "USDC", "total": -20000.0, "borrowed": 20000.0}],
            [{"coin": "ETH", "size": -1.0, "notional_usd": 3000.0,
              "leverage_type": "cross"}],
            {"HYPE": 80.0},
            ltv_map={"HYPE": ltv},
            perp_cross_mm=0.0,
        )
        expected = 0.5 + 0.5 * ltv
        assert abs(pm.liq_threshold - expected) < 1e-9, (ltv, pm.liq_threshold)
        block = build_pm_llm_block(pm)
        # The block reports the same data-derived threshold.
        assert f"{expected:.2f}" in block

    # No hardcoded 0.75 *numeric literal* in the new module's code (docstrings
    # / comments may mention it as explanatory prose — only executable code is
    # checked, via AST, so the threshold can never be baked in).
    import ast
    import modules.pm_context as _ctx
    with open(_ctx.__file__, encoding="utf-8") as f:
        src = f.read()
    hardcoded = [
        node for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, float)
        and abs(node.value - 0.75) < 1e-9
    ]
    assert not hardcoded, "liq threshold must stay param-driven, never a hardcoded 0.75 literal"


# ── T7 ──────────────────────────────────────────────────────────────────────
def test_t7_existing_substrings_intact():
    """Prior required prompt substrings + default-prompt HF prohibitions hold."""
    os.environ["FLYWHEEL_DEPRECATED"] = "true"
    import importlib
    import config
    importlib.reload(config)
    from templates import system_prompt
    importlib.reload(system_prompt)

    block = system_prompt.build_fund_state_block()
    full = block + "\n" + system_prompt.SYSTEM_PROMPT

    # Required substrings (test_reporte_llm_context_clean parity).
    assert "PM MARGIN-RATIO THRESHOLDS" in block
    assert "0.85" in block

    # Default-prompt prohibitions (test_no_legacy_flywheel_concepts parity).
    assert "health factor" not in full.lower()
    assert not re.search(r"HF\s*[<>≥]", full), "legacy HF threshold pattern leaked"
