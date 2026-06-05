"""P1.6 / R-AUDIT2-P0.1 — direction-aware per-position funding.

8h funding = HL hourly rate × 8, in bps. Direction is derived from side + rate
sign per the HL convention:

  rate > 0 ⇒ LONGS pay shorts;  rate < 0 ⇒ SHORTS pay longs.
  LONG  → PAYING if rate>0, RECEIVING if rate<0.
  SHORT → PAYING if rate<0, RECEIVING if rate>0.

The expensive-carry flag fires ONLY when the position is PAYING beyond the
positive magnitude threshold (FUNDING_EXPENSIVE_BPS_8H, default 1.5 bp/8h),
computed per side. A RECEIVING position is never flagged and never shows the
monitoring eye.
"""
from __future__ import annotations

from modules.funding_tracker import (
    funding_8h_bps,
    funding_direction,
    evaluate_position_funding,
    build_funding_block,
    format_funding_line,
    PAYING,
    RECEIVING,
)

# HL hourly rate → 8h bps reference points (rate = bps / 8 / 10_000).
RATE_NEG_044 = -0.44 / 8 / 10_000   # → −0.44 bp/8h  (today's SOL LONG case)
RATE_NEG_200 = -2.00 / 8 / 10_000   # → −2.00 bp/8h
RATE_POS_044 = +0.44 / 8 / 10_000   # → +0.44 bp/8h
RATE_POS_200 = +2.00 / 8 / 10_000   # → +2.00 bp/8h  (≥ 1.5 threshold)
RATE_POS_150 = +1.50 / 8 / 10_000   # → +1.50 bp/8h  (exactly at threshold)


def _pos(coin, side="LONG", cum=0.0):
    # cum here is the RAW HL value (positive = PAID).
    return {"coin": coin, "side": side, "cum_funding_since_open": cum}


def test_funding_8h_conversion():
    assert abs(funding_8h_bps(RATE_POS_200) - 2.0) < 1e-6
    assert abs(funding_8h_bps(RATE_NEG_200) - (-2.0)) < 1e-6
    assert funding_8h_bps(None) is None


# ── direction model ─────────────────────────────────────────────────────────
def test_direction_long():
    assert funding_direction("LONG", -0.44)[0] == RECEIVING
    assert funding_direction("LONG", +2.0)[0] == PAYING
    # paying magnitude for a long equals the (positive) rate
    assert abs(funding_direction("LONG", +2.0)[1] - 2.0) < 1e-9
    # receiving long pays nothing
    assert funding_direction("LONG", -0.44)[1] == 0.0


def test_direction_short():
    assert funding_direction("SHORT", +0.44)[0] == RECEIVING
    assert funding_direction("SHORT", -2.0)[0] == PAYING
    # paying magnitude for a short equals |rate|
    assert abs(funding_direction("SHORT", -2.0)[1] - 2.0) < 1e-9
    assert funding_direction("SHORT", +0.44)[1] == 0.0


# ── REGRESSION GUARD: the four quadrants (required by P0.1 acceptance) ───────
def test_guard_long_receiving_not_flagged_no_eye():
    # The exact today bug: SOL LONG at −0.44 bp/8h is RECEIVING → no eye/flag.
    pf = evaluate_position_funding(_pos("SOL", "LONG"), RATE_NEG_044)
    assert pf.direction == RECEIVING
    assert pf.zone == "RECV"
    assert pf.expensive_carry is False
    line = format_funding_line(pf)
    assert "recibiendo funding (favorable)" in line
    assert "👁" not in line and "🚩" not in line


def test_guard_long_paying_above_threshold_flags():
    pf = evaluate_position_funding(_pos("BTC", "LONG"), RATE_POS_200)
    assert pf.direction == PAYING
    assert pf.zone == "FLAG"
    assert pf.expensive_carry is True
    assert "🚩 carry caro — MANUAL REVIEW" in format_funding_line(pf)


def test_guard_short_receiving_not_flagged_no_eye():
    pf = evaluate_position_funding(_pos("WLD", "SHORT"), RATE_NEG_044)
    # SHORT with rate<0 PAYS — so use a positive rate for receiving:
    pf_recv = evaluate_position_funding(_pos("WLD", "SHORT"), RATE_POS_044)
    assert pf_recv.direction == RECEIVING
    assert pf_recv.expensive_carry is False
    assert "👁" not in format_funding_line(pf_recv)
    # and the rate<0 short is PAYING (sanity for the quadrant)
    assert pf.direction == PAYING


def test_guard_short_paying_above_threshold_flags():
    pf = evaluate_position_funding(_pos("ENA", "SHORT"), RATE_NEG_200)
    assert pf.direction == PAYING
    assert pf.zone == "FLAG"
    assert pf.expensive_carry is True


# ── threshold boundary + zones ──────────────────────────────────────────────
def test_paying_below_threshold_is_monitor_not_flag():
    pf = evaluate_position_funding(_pos("ARB", "LONG"), RATE_POS_044)
    assert pf.direction == PAYING
    assert pf.zone == "MONITOR"
    assert pf.expensive_carry is False
    assert "👁 monitoreo" in format_funding_line(pf)


def test_paying_at_threshold_flags():
    pf = evaluate_position_funding(_pos("OP", "LONG"), RATE_POS_150)
    assert pf.expensive_carry is True
    assert pf.zone == "FLAG"


def test_custom_threshold_override():
    # With a 3.0 bp threshold, +2.0 bp paying is no longer flagged.
    pf = evaluate_position_funding(_pos("BTC", "LONG"), RATE_POS_200, threshold_bps=3.0)
    assert pf.expensive_carry is False
    assert pf.zone == "MONITOR"


def test_stale_negative_threshold_is_clamped(monkeypatch):
    # A leftover NEGATIVE env value (pre-fix semantics) must NOT flag everyone.
    monkeypatch.setenv("FUNDING_EXPENSIVE_BPS_8H", "-2.0")
    pf = evaluate_position_funding(_pos("SOL", "LONG"), RATE_NEG_044)  # receiving
    assert pf.expensive_carry is False
    pf2 = evaluate_position_funding(_pos("ARB", "LONG"), RATE_POS_044)  # paying 0.44 < 1.5
    assert pf2.expensive_carry is False


# ── cumulative display sign (favorable convention) ──────────────────────────
def test_cum_funding_display_is_favorable_convention():
    # Raw HL +1.21 = PAID → display as −1.21 (cost / red).
    paid = evaluate_position_funding(_pos("BTC", "LONG", cum=1.21), RATE_POS_044)
    assert abs(paid.cum_funding_usd - (-1.21)) < 1e-9
    # Raw HL −1.21 = RECEIVED → display as +1.21 (favorable / green).
    recv = evaluate_position_funding(_pos("SOL", "LONG", cum=-1.21), RATE_NEG_044)
    assert abs(recv.cum_funding_usd - (+1.21)) < 1e-9


def test_missing_rate_is_na():
    pf = evaluate_position_funding(_pos("XYZ"), None)
    assert pf.zone == "N/A"
    assert pf.funding_8h_bps is None
    assert pf.direction == "N/A"


# ── block rendering ─────────────────────────────────────────────────────────
def test_build_block_marks_manual_review_for_paying():
    positions = [_pos("BTC", "LONG", cum=2.0)]
    rates = {"BTC": RATE_POS_200}
    block = build_funding_block(positions, rates)
    assert "MANUAL REVIEW" in block
    assert "BTC" in block


def test_build_block_receiving_long_has_no_review():
    positions = [_pos("SOL", "LONG", cum=-1.0)]
    rates = {"SOL": RATE_NEG_044}
    block = build_funding_block(positions, rates)
    assert "MANUAL REVIEW" not in block
    assert "👁" not in block
    assert "recibiendo funding (favorable)" in block


def test_build_block_empty_when_no_positions():
    assert build_funding_block([], {}, set()) == ""
