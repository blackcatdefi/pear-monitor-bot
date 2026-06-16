"""R-FUNDING-TRUTH (2026-06-15) — single source of truth for funding direction.

The bug: the FULL ANALYSIS LLM received only raw portfolio JSON and re-derived
funding DIRECTION on its own, getting it backwards — narrating a BTC LONG that
PAYS positive funding (carry accrued −61.20 USD, a real cost) as
"LONG COBRA — no es carry caro". The dedicated funding_por_posición block had it
right ("pagando funding"); the two surfaces disagreed because the LLM did its
own arithmetic.

The fix mirrors pm_context.py: one deterministic ``funding_verdict`` is the
single source of truth, consumed by BOTH the funding_por_posición block
(``evaluate_position_funding``) AND the FULL ANALYSIS LLM context
(``build_funding_llm_block``). The model verbalizes the precomputed
``display_string`` VERBATIM and is forbidden from inferring cobra/paga itself.

Ground truth (perpetual swaps, HL convention):
  rate > 0 ⇒ longs pay shorts;  rate < 0 ⇒ shorts pay longs.
  LONG  + rate>0 → PAYS     · LONG  + rate<0 → RECEIVES
  SHORT + rate>0 → RECEIVES · SHORT + rate<0 → PAYS
  rate == 0 → NEUTRAL (never expensive).
The expensive-carry flag fires ONLY when the position PAYS beyond the threshold.
"""
from __future__ import annotations

from modules.funding_tracker import (
    funding_verdict,
    build_funding_llm_block,
    evaluate_position_funding,
    format_funding_line,
    FundingVerdict,
    PAYS,
    RECEIVES,
    NEUTRAL,
    NA_VERDICT,
)

# bp/8h reference magnitudes (these are passed directly as the signed 8h rate).
POS_2 = +2.0   # ≥ 1.5 threshold → expensive when PAYS
NEG_2 = -2.0
POS_044 = +0.44  # below threshold
NEG_044 = -0.44


# ── FIX 1: the four side/sign quadrants ──────────────────────────────────────
def test_quadrant_long_positive_pays():
    v = funding_verdict("LONG", POS_2, carry_accrued=-61.20)
    assert v.direction == PAYS
    assert "PAGA" in v.display_string


def test_quadrant_long_negative_receives():
    v = funding_verdict("LONG", NEG_2, carry_accrued=+10.0)
    assert v.direction == RECEIVES
    assert "RECIBE" in v.display_string


def test_quadrant_short_positive_receives():
    v = funding_verdict("SHORT", POS_2, carry_accrued=+10.0)
    assert v.direction == RECEIVES
    assert "RECIBE" in v.display_string


def test_quadrant_short_negative_pays():
    v = funding_verdict("SHORT", NEG_2, carry_accrued=-10.0)
    assert v.direction == PAYS
    assert "PAGA" in v.display_string


# ── FIX 1: the zero case ─────────────────────────────────────────────────────
def test_zero_rate_is_neutral_not_expensive():
    v = funding_verdict("LONG", 0.0, carry_accrued=0.0)
    assert v.direction == NEUTRAL
    assert v.is_expensive_carry is False
    assert "PAGA" not in v.display_string and "RECIBE" not in v.display_string


# ── FIX 1: carry-sign disagreement — realized cashflow wins ──────────────────
def test_carry_sign_disagreement_trusts_realized_carry():
    # Rate sign says a LONG with rate<0 RECEIVES, but the realized carry is
    # NEGATIVE (net PAID). Realized cashflow is ground truth → verdict = PAYS.
    v = funding_verdict("LONG", NEG_2, carry_accrued=-50.0, coin="BTC")
    assert v.direction == PAYS
    assert "PAGA" in v.display_string


def test_agreement_no_override():
    # Rate and carry agree (LONG rate>0 pays, carry negative = paid) → PAYS.
    v = funding_verdict("LONG", POS_2, carry_accrued=-61.20)
    assert v.direction == PAYS


# ── FIX 1: expensive-carry flag matches direction + magnitude ────────────────
def test_expensive_only_when_paying_above_threshold():
    assert funding_verdict("LONG", POS_2, -5.0).is_expensive_carry is True   # pays ≥1.5
    assert funding_verdict("LONG", POS_044, -1.0).is_expensive_carry is False  # pays <1.5
    assert funding_verdict("SHORT", NEG_2, -5.0).is_expensive_carry is True   # pays ≥1.5
    assert funding_verdict("SHORT", POS_2, +5.0).is_expensive_carry is False  # receives
    assert funding_verdict("LONG", NEG_2, +5.0).is_expensive_carry is False   # receives


def test_custom_threshold():
    assert funding_verdict("LONG", POS_2, -5.0, threshold_bps=3.0).is_expensive_carry is False


# ── FIX 1: production bug case end-to-end (the exact reported failure) ────────
def test_production_btc_long_positive_funding_says_paga():
    # BTC LONG, +0.00125/8h funding (= +1.0 bp/8h), carry −61.20 USD (a real
    # cost). Pre-fix the LLM said "COBRA". Now both the verdict AND the LLM
    # block must say PAGA.
    v = funding_verdict("LONG", 1.0, carry_accrued=-61.20)
    assert v.direction == PAYS
    assert "PAGA" in v.display_string

    pos = [{"coin": "BTC", "side": "LONG", "cum_funding_since_open": 61.20}]
    rates = {"BTC": 1.0 / 8 / 10_000}  # +1.0 bp/8h
    block = build_funding_llm_block(pos, rates)
    assert "BTC LONG: PAGA" in block
    # And the LLM is explicitly forbidden from re-deriving direction.
    assert "PROHIBIDO" in block
    assert "VERBATIM" in block


# ── single source of truth: block + por-posición agree ───────────────────────
def test_block_and_por_posicion_agree_on_direction():
    pos = {"coin": "BTC", "side": "LONG", "cum_funding_since_open": 61.20}
    rate_bps_8h = 2.0
    hourly = rate_bps_8h / 8 / 10_000
    # por-posición path
    pf = evaluate_position_funding(pos, hourly)
    # LLM block path
    block = build_funding_llm_block([pos], {"BTC": hourly})
    # Both must agree this position PAYS and is expensive carry.
    assert pf.direction == "PAYING" and pf.expensive_carry is True
    assert "BTC LONG: PAGA" in block and "CARRY CARO" in block


# ── FIX 2: never fabricate a value — missing carry renders n/d ────────────────
def test_missing_carry_renders_nd_not_zero():
    # No cum_funding_since_open key at all (partial/failed fetch) → n/d, never 0.
    pos = {"coin": "ETH", "side": "LONG"}
    rates = {"ETH": 2.0 / 8 / 10_000}
    block = build_funding_llm_block([pos], rates)
    assert "n/d" in block
    assert "+0.00" not in block


def test_explicit_none_carry_renders_nd():
    pos = {"coin": "ETH", "side": "LONG", "cum_funding_since_open": None}
    pf = evaluate_position_funding(pos, 2.0 / 8 / 10_000)
    assert pf.cum_funding_usd is None
    assert "carry acum n/d" in format_funding_line(pf)


def test_genuine_zero_carry_is_not_nd():
    # A real, present 0.0 is a genuine flat result — shown as +0.00, not n/d.
    pos = {"coin": "ETH", "side": "LONG", "cum_funding_since_open": 0.0}
    pf = evaluate_position_funding(pos, 2.0 / 8 / 10_000)
    assert pf.cum_funding_usd == 0.0
    assert "+0.00 USD" in format_funding_line(pf)


# ── n/d when neither rate nor carry available ────────────────────────────────
def test_no_rate_no_carry_is_na():
    v = funding_verdict("LONG", None, carry_accrued=None)
    assert v.direction == NA_VERDICT
    assert "n/d" in v.display_string


def test_no_rate_falls_back_to_carry_sign():
    # Missing live rate but realized carry is negative (paid) → PAYS.
    v = funding_verdict("LONG", None, carry_accrued=-30.0)
    assert v.direction == PAYS


# ── struct shape ─────────────────────────────────────────────────────────────
def test_verdict_struct_fields():
    v = funding_verdict("LONG", POS_2, -5.0)
    assert isinstance(v, FundingVerdict)
    assert set(("direction", "is_expensive_carry", "display_string")).issubset(
        v.__dict__.keys() if hasattr(v, "__dict__") else
        {"direction", "is_expensive_carry", "display_string", "paying_bps"}
    )
    assert v.direction in (PAYS, RECEIVES, NEUTRAL, NA_VERDICT)
    assert isinstance(v.is_expensive_carry, bool)
    assert isinstance(v.display_string, str)


# ── empty injection ──────────────────────────────────────────────────────────
def test_empty_positions_injects_nothing():
    assert build_funding_llm_block([], {}) == ""
    assert build_funding_llm_block(None, None) == ""


# ── end-to-end: compile_raw_data injects the funding verdict into LLM context ──
def test_compile_raw_data_injects_funding_verdict():
    from templates.formatters import compile_raw_data
    portfolio = [{
        "status": "ok",
        "data": {
            "wallet": "0xc7ae",
            "positions": [{"coin": "BTC", "side": "LONG", "cum_funding_since_open": 61.20}],
        },
    }]
    rates = {"BTC": 1.0 / 8 / 10_000}  # +1.0 bp/8h → LONG PAYS
    out = compile_raw_data(portfolio, None, {}, None, None, funding_rates=rates)
    assert "FUNDING POR POSICIÓN — VEREDICTO PRE-CALCULADO" in out
    assert "BTC LONG: PAGA" in out
    # The model must be forbidden from re-deriving direction itself.
    assert "PROHIBIDO inferir 'cobra'/'paga'" in out
