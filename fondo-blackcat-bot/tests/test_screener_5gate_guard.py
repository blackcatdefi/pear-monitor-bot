"""P1.8 — deterministic 5-gate short filter regression guard.

The short screen is: z POSITIVE + Hurst < cutoff + funding ≥ 0 + squeeze CLEAR
+ data ≥90% ⇒ 5/5 or EXCLUDE. Squeeze risk forces score −1000 (hard exclude,
both gating AND ranking). The per-token output must show pass/fail on EACH of
the five gates (data, z, Hurst, squeeze, funding).
"""
from __future__ import annotations

from modules.unlock_monitor import AltGate
from modules.universal_screener import (
    short_pass_count,
    short_score,
    short_verdict,
    _short_gate_detail,
    constants,
)


def _gate(**over):
    base = dict(
        ticker="TST", sector="alt", z=1.5, z_streak=3, hurst=0.40, rsi=50.0,
        pct_k=5.0, higher_highs=False, funding=0.0, funding_sign=1, corr=None,
        repairing=None, coverage=1.0, data_ok=True, z_floor_ok=True,
        z_persistent=True, z_ok=True, hurst_ok=True, squeeze_flag=False,
        squeeze_reasons=[], funding_ok=True, counts=True, reason="",
    )
    base.update(over)
    return AltGate(**base)


def test_pass_count_is_five_gates():
    assert short_pass_count(_gate()) == 5
    assert short_pass_count(_gate(z_ok=False)) == 4
    assert short_pass_count(_gate(hurst_ok=False, funding_ok=False)) == 3


def test_squeeze_forces_minus_1000_in_ranking():
    clean_low = _gate(z_ok=False, z=0.1)            # 4/5, no squeeze
    squeezing_high = _gate(squeeze_flag=True, squeeze_reasons=["blow-off"], z=3.0)  # 4/5 but squeeze
    # The squeezing name must rank BELOW the clean one despite a huge z.
    assert short_score(squeezing_high) < short_score(clean_low)
    assert short_score(squeezing_high) <= -1000 + short_pass_count(squeezing_high) * 100 + 100


def test_data_insufficient_is_excluded_from_ranking():
    g = _gate(data_ok=False, coverage=0.5)
    assert short_score(g) == float("-inf")
    assert "DATA INSUF" in short_verdict(g)


def test_only_full_5_of_5_is_go_candidate():
    assert "5/5 GO" in short_verdict(_gate())
    assert "5/5 GO" not in short_verdict(_gate(funding_ok=False))


def test_per_token_breakdown_shows_all_five_gates():
    detail = _short_gate_detail(_gate(), constants())
    for token in ("data", "z ", "Hurst", "squeeze", "fund"):
        assert token in detail, f"gate {token!r} missing from per-token breakdown"
    # Both pass and fail markers must be representable.
    fail_detail = _short_gate_detail(_gate(hurst_ok=False), constants())
    assert "❌" in fail_detail and "✅" in fail_detail


def test_squeezing_name_verdict_is_no_go():
    g = _gate(squeeze_flag=True, squeeze_reasons=["RSI 80+HH (blow-off)"])
    assert "NO-GO" in short_verdict(g)
    assert "squeeze" in short_verdict(g).lower()
