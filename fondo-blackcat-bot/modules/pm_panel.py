"""R-PM-RATIO-RELABEL (2026-06-07) — honest Portfolio Margin risk framing.

The bug (live /reporte 2026-06-07)
----------------------------------
The PM panel printed ``Margin ratio: 101.1% 🔴 LIQ-RISK (WARN 40% STRESS 70%
CRÍTICO 85% LIQ 95%)`` while the SAME panel correctly showed aave-HF 1.48 🟢
and liq price $40.32. The 101% is borrow-capacity UTILIZATION = debt /
(collateral × max_ltv); the 0.50 LTV only gates whether you can DRAW MORE
debt. Forced liquidation uses a SEPARATE maintenance threshold
(``0.5 + 0.5×ltv`` = 0.75) and only triggers at portfolio_margin_ratio > 0.95.
Crossing 100% of the 0.50 cap means "over-drawn / cannot borrow more", NOT
"near liquidation". The red 101% was alarmist and self-contradictory.

The fix
-------
* HEADLINE risk = the aave-style Health Factor / distance-to-liquidation,
  coloured GREEN HF≥1.30, YELLOW 1.10≤HF<1.30, RED HF<1.10.
* The utilization line is RENAMED to "Borrow utilization (vs 50% max-borrow)"
  with NON-liquidation status labels (never red, never the string "LIQ-RISK"),
  and the WARN/STRESS/CRÍTICO/LIQ scale is REMOVED from it.
* Any red alarm is re-anchored to the real liquidation math (HF, or
  portfolio_margin_ratio approaching 0.95), never to utilization > 100%.

All LTV/threshold values are PARAMETERS read from data (per-token ltv from the
spot row; liq_threshold = 0.5 + 0.5×ltv). Nothing is hardcoded. NEVER raises.
"""
from __future__ import annotations

# Headline (HF) colour bands — coarser than risk_tier's labels on purpose: the
# headline communicates only safe / watch / danger via distance-to-liquidation.
HF_GREEN_MIN = 1.30
HF_YELLOW_MIN = 1.10

# Borrow-utilization status bands (vs the max-borrow LTV cap, NOT liquidation).
UTIL_OK_MAX = 90.0       # < 90%  → headroom
UTIL_NEAR_MAX = 100.0    # 90-100% → near the cap

# The forbidden token that must NEVER appear on the utilization line.
_FORBIDDEN = "LIQ-RISK"


def headline_color(aave_hf: float, *, has_debt: bool) -> str:
    """Map the aave-style HF to a headline colour emoji. NEVER raises.

    GREEN HF≥1.30 · YELLOW 1.10≤HF<1.30 · RED HF<1.10. With no debt (or no
    HF data) the headline is green/neutral.
    """
    if not has_debt:
        return "🟢"
    try:
        h = float(aave_hf)
    except (TypeError, ValueError):
        return "🟢"
    if h <= 0:
        return "🟢"
    if h >= HF_GREEN_MIN:
        return "🟢"
    if h >= HF_YELLOW_MIN:
        return "🟡"
    return "🔴"


def borrow_utilization_status(util_pct: float) -> tuple[str, bool]:
    """Status label for borrow utilization vs the max-borrow cap.

    Returns ``(label, is_red)``. ``is_red`` is ALWAYS False — utilization is a
    borrow-capacity signal, not a liquidation signal — and the label NEVER
    contains "LIQ". NEVER raises.

      <90%    → "borrow headroom OK"
      90-100% → "NEAR MAX-BORROW: limited new draws"
      >100%   → "OVER MAX-BORROW: no new draws; reduce or add collateral"
    """
    try:
        u = float(util_pct)
    except (TypeError, ValueError):
        u = 0.0
    if u < UTIL_OK_MAX:
        label = "borrow headroom OK"
    elif u <= UTIL_NEAR_MAX:
        label = "NEAR MAX-BORROW: limited new draws"
    else:
        label = "OVER MAX-BORROW: no new draws; reduce or add collateral"
    # Invariant: never a liquidation colour, never the forbidden token.
    assert _FORBIDDEN not in label
    return label, False


def explainer_line(liq_price: float) -> str:
    """One-line clarifier separating the borrow cap from liquidation.

    "Over max-borrow blocks new USDC draws only; liquidation is at HYPE
    ~$<liq> (trigger ratio>0.95)." NEVER raises.
    """
    try:
        lp = float(liq_price or 0.0)
    except (TypeError, ValueError):
        lp = 0.0
    px = f"~${lp:,.2f}" if lp > 0 else "el precio de mantenimiento"
    return (
        "ℹ️ Over max-borrow bloquea solo nuevos draws de USDC; "
        f"la liquidación real es a HYPE {px} (trigger ratio>0.95)."
    )
