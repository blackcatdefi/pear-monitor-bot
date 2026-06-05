"""P1.6 — per-position funding tracking + expensive-carry flag.

8h funding = HL hourly rate × 8, in bps. Cumulative carry = cumFunding.
sinceOpen. LONG cycle-accumulation positions raise the carry flag only when
8h funding is at/below the floor (default −2.0 bp). ZEC at −0.83 bp/8h is in
the MONITOR zone (not flagged); at −5.5 bp it FLAGS.
"""
from __future__ import annotations

from modules.funding_tracker import (
    funding_8h_bps,
    evaluate_position_funding,
    build_funding_block,
)

# HL hourly rates → 8h bps reference points.
RATE_NEG_083 = -0.83 / 8 / 10_000   # → −0.83 bp/8h
RATE_NEG_550 = -5.50 / 8 / 10_000   # → −5.50 bp/8h
RATE_POS = 0.0000058924             # BTC-ish, positive


def _pos(coin, side="LONG", cum=0.0):
    return {"coin": coin, "side": side, "cum_funding_since_open": cum}


def test_funding_8h_conversion():
    assert abs(funding_8h_bps(RATE_NEG_550) - (-5.50)) < 1e-6
    assert funding_8h_bps(None) is None


def test_zec_monitor_zone_not_flagged():
    # −0.83 bp > −2.0 floor → MONITOR, not a flag, even for a cycle long.
    pf = evaluate_position_funding(_pos("ZEC", cum=-1.21), RATE_NEG_083, is_cycle_long=True)
    assert pf.zone == "MONITOR"
    assert pf.expensive_carry is False
    assert abs(pf.cum_funding_usd - (-1.21)) < 1e-9


def test_cycle_long_below_floor_flags():
    pf = evaluate_position_funding(_pos("ZEC"), RATE_NEG_550, is_cycle_long=True)
    assert pf.zone == "FLAG"
    assert pf.expensive_carry is True


def test_non_cycle_below_floor_monitor_not_flag():
    # A tactical (non-cycle) leg past the floor is surfaced but NOT flagged
    # for expensive-carry (the flag is cycle-only per the fund rule).
    pf = evaluate_position_funding(_pos("WLD"), RATE_NEG_550, is_cycle_long=False)
    assert pf.expensive_carry is False
    assert pf.zone == "MONITOR"


def test_positive_funding_is_ok_zone():
    pf = evaluate_position_funding(_pos("BTC"), RATE_POS, is_cycle_long=True)
    assert pf.zone == "OK"
    assert pf.expensive_carry is False


def test_missing_rate_is_na():
    pf = evaluate_position_funding(_pos("XYZ"), None, is_cycle_long=True)
    assert pf.zone == "N/A"
    assert pf.funding_8h_bps is None


def test_custom_floor_override():
    # With a −6 bp floor, −5.5 bp is no longer flagged.
    pf = evaluate_position_funding(_pos("ZEC"), RATE_NEG_550, is_cycle_long=True, floor_bps=-6.0)
    assert pf.expensive_carry is False


def test_build_block_marks_manual_review():
    positions = [_pos("ZEC", cum=-1.21)]
    rates = {"ZEC": RATE_NEG_550}
    block = build_funding_block(positions, rates, cycle_long_coins={"ZEC"})
    assert "MANUAL REVIEW" in block
    assert "ZEC" in block


def test_build_block_empty_when_no_positions():
    assert build_funding_block([], {}, set()) == ""
