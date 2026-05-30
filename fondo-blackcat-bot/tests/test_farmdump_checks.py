"""R-FARMDUMP — tests for the automatic 5-check pre-trade filter.

Covers:
  * each check's PASS / WARN / FAIL boundaries with the actual number
  * verdict aggregation (GO / CAUTION / NO-GO from checks 1-4)
  * missing-field degradation → WARN (never fabricated, never a crash)
  * a forced reversion producing the full enriched message block
  * the documentation one-liner (check 5)
  * HL ticker-alias resolution + run_checks never raising even on total outage
All network is mocked — these tests are offline and deterministic.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from modules import farmdump_checks as fd


def _th(**over):
    th = fd.thresholds()
    th.update(over)
    return th


# ─── Check 1 — funding reverted / not crowded ────────────────────────────────
def test_funding_pass_near_mean():
    assert fd.eval_funding(-50.0, _th()).status == fd.PASS
    assert fd.eval_funding(-100.0, _th()).status == fd.PASS  # at the ceil
    assert fd.eval_funding(0.0, _th()).status == fd.PASS


def test_funding_fail_still_crowded():
    c = fd.eval_funding(-450.0, _th())
    assert c.status == fd.FAIL
    assert "-450" in c.detail


def test_funding_fail_overshoot_positive():
    c = fd.eval_funding(250.0, _th())
    assert c.status == fd.FAIL
    assert "overshoot" in c.detail.lower() or "+250" in c.detail


def test_funding_warn_partial_revert():
    # Between crowded floor (-300) and mean ceil (-100).
    assert fd.eval_funding(-200.0, _th()).status == fd.WARN


def test_funding_warn_when_missing():
    c = fd.eval_funding(None, _th())
    assert c.status == fd.WARN
    assert "no disponible" in c.detail


def test_funding_thresholds_are_env_tunable(monkeypatch):
    monkeypatch.setenv("FARMDUMP_FUNDING_CROWDED_FLOOR", "-150")
    th = fd.thresholds()
    assert th["funding_crowded_floor"] == -150.0
    # -200 now sits below the (raised) floor → FAIL instead of WARN.
    assert fd.eval_funding(-200.0, th).status == fd.FAIL


# ─── Check 2 — price action 24h ──────────────────────────────────────────────
def test_price_action_pass_stable():
    assert fd.eval_price_action(-4.0, _th()).status == fd.PASS
    assert fd.eval_price_action(3.0, _th()).status == fd.PASS


def test_price_action_warn_rising():
    c = fd.eval_price_action(12.0, _th())
    assert c.status == fd.WARN
    assert "12" in c.detail


def test_price_action_fail_vertical():
    c = fd.eval_price_action(25.0, _th())
    assert c.status == fd.FAIL


def test_price_action_warn_when_missing():
    assert fd.eval_price_action(None, _th()).status == fd.WARN


# ─── Check 3 — OI vs volume / liquidity ──────────────────────────────────────
def test_liquidity_pass_healthy():
    assert fd.eval_liquidity(5_000_000, 3_000_000, _th()).status == fd.PASS


def test_liquidity_warn_borderline():
    # Between min_vol (1M) and 2× min_vol.
    assert fd.eval_liquidity(1_500_000, None, _th()).status == fd.WARN


def test_liquidity_fail_illiquid():
    c = fd.eval_liquidity(100_000, None, _th())
    assert c.status == fd.FAIL
    assert "ilíquido" in c.detail or "iliquido" in c.detail.lower()


def test_liquidity_warn_when_both_missing():
    assert fd.eval_liquidity(None, None, _th()).status == fd.WARN


def test_liquidity_falls_back_to_oi_when_vol_missing():
    # No volume, but OI present and healthy → judged on OI.
    c = fd.eval_liquidity(None, 5_000_000, _th())
    assert c.status == fd.PASS
    assert "OI" in c.detail


# ─── Check 4 — daily trend ───────────────────────────────────────────────────
def test_trend_pass_downtrend():
    c = fd.eval_trend([10, 9, 8.5, 8, 7.5, 7], _th())
    assert c.status == fd.PASS


def test_trend_fail_strong_uptrend():
    c = fd.eval_trend([6, 7, 8, 9, 11], _th())
    assert c.status == fd.FAIL


def test_trend_warn_mild_uptrend():
    # Above SMA but gain below the uptrend threshold (10%).
    c = fd.eval_trend([10.0, 10.1, 10.2, 10.3, 10.4], _th())
    assert c.status == fd.WARN


def test_trend_warn_when_missing():
    assert fd.eval_trend(None, _th()).status == fd.WARN
    assert fd.eval_trend([10.0], _th()).status == fd.WARN  # too few points


# ─── Verdict aggregation ─────────────────────────────────────────────────────
def _chk(n, status):
    return fd.Check(n, f"c{n}", status, "")


def test_verdict_go_all_pass():
    checks = [_chk(i, fd.PASS) for i in range(1, 5)] + [_chk(5, fd.PASS)]
    assert fd.aggregate_verdict(checks) == fd.GO


def test_verdict_caution_on_warn():
    checks = [_chk(1, fd.PASS), _chk(2, fd.WARN), _chk(3, fd.PASS), _chk(4, fd.PASS)]
    assert fd.aggregate_verdict(checks) == fd.CAUTION


def test_verdict_nogo_on_any_fail():
    checks = [_chk(1, fd.PASS), _chk(2, fd.WARN), _chk(3, fd.FAIL), _chk(4, fd.PASS)]
    assert fd.aggregate_verdict(checks) == fd.NO_GO


def test_verdict_ignores_check5_status():
    # Even if check 5 were FAIL (it never is), it must not drive the verdict.
    checks = [_chk(i, fd.PASS) for i in range(1, 5)] + [_chk(5, fd.FAIL)]
    assert fd.aggregate_verdict(checks) == fd.GO


# ─── Documentation line (check 5) ────────────────────────────────────────────
def test_doc_line_contains_all_fields():
    line = fd.build_doc_line(
        "PORTAL", -600, -90, 85, 1.2345, 8_000_000, 5_000_000,
        ts_utc="2026-05-30 13:00 UTC",
    )
    for token in ["PORTAL", "-600", "-90", "85", "OI", "vol24h", "2026-05-30"]:
        assert token in line


def test_doc_line_handles_missing_numbers():
    line = fd.build_doc_line("XYZ", None, None, None, None, None, None,
                             ts_utc="2026-05-30 13:00 UTC")
    assert "n/a" in line and "XYZ" in line


# ─── HL alias resolution ─────────────────────────────────────────────────────
def test_hl_aliases():
    assert "PEPE" in fd._hl_coin_aliases("kPEPE")
    assert "kPEPE" in fd._hl_coin_aliases("PEPE")
    assert fd._hl_coin_aliases("BTC")[0] == "BTC"


# ─── Async orchestration (network mocked) ────────────────────────────────────
def _run(coro):
    # Fresh loop each call so we don't depend on (or clobber) a loop another
    # test may have closed — keeps these robust inside the full suite.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_run_checks_full_go_message(monkeypatch):
    async def fake_market(t):
        return {"chg_24h": -3.0, "mark": 1.23, "oi_usd": 8e6, "vol_24h": 5e6, "coin": t}

    async def fake_closes(t, n):
        return [10, 9.5, 9, 8.5, 8, 7.5, 7]

    with patch.object(fd, "fetch_hl_market", fake_market), \
         patch.object(fd, "fetch_hl_daily_closes", fake_closes):
        r = _run(fd.run_checks("PORTAL", -600, -90, var_price=1.23,
                               var_vol_24h=5e6, var_oi_usd=8e6, pct_reverted=85))
    assert r.verdict == fd.GO
    block = fd.format_checks_block(r)
    assert "5 CHECKS (Farm the DUMP):" in block
    assert "VEREDICTO: GO" in block
    assert "Decisión final tuya, BCD" in block
    assert "Price: $1.23" in block and "Vol24h: $5.00M" in block
    # All five checks present and numbered.
    for i in range(1, 6):
        assert f"{i}." in block


def test_run_checks_nogo_when_funding_still_deep(monkeypatch):
    async def fake_market(t):
        return {"chg_24h": -3.0, "mark": 0.01, "oi_usd": 8e6, "vol_24h": 5e6, "coin": t}

    async def fake_closes(t, n):
        return [10, 9.5, 9, 8.5, 8, 7.5, 7]

    with patch.object(fd, "fetch_hl_market", fake_market), \
         patch.object(fd, "fetch_hl_daily_closes", fake_closes):
        r = _run(fd.run_checks("SHIT", -600, -450, var_vol_24h=5e6, var_oi_usd=8e6))
    assert r.verdict == fd.NO_GO
    assert r.checks[0].status == fd.FAIL


def test_run_checks_degrades_to_caution_on_total_outage():
    async def dead_market(t):
        return {"chg_24h": None, "mark": None, "oi_usd": None, "vol_24h": None, "coin": None}

    async def dead_closes(t, n):
        return None

    with patch.object(fd, "fetch_hl_market", dead_market), \
         patch.object(fd, "fetch_hl_daily_closes", dead_closes):
        # Funding near mean (PASS) but everything else n/a → WARNs → CAUTION.
        r = _run(fd.run_checks("XYZ", -600, -90, pct_reverted=85))
    assert r.verdict == fd.CAUTION
    assert r.n_fail == 0 and r.n_warn >= 1
    block = fd.format_checks_block(r)
    assert "no disponible" in block


def test_run_checks_never_raises_even_if_enrichment_throws():
    async def boom(*a, **k):
        raise RuntimeError("network exploded")

    with patch.object(fd, "fetch_hl_market", boom), \
         patch.object(fd, "fetch_hl_daily_closes", boom):
        # run_checks wraps enrichment; must still produce a result.
        r = _run(fd.run_checks("BOOM", -600, -90))
    assert r is not None
    # Checks 2 & 4 degrade to WARN (no data), check 1 PASS → CAUTION.
    assert r.verdict in (fd.CAUTION, fd.NO_GO)


def test_run_checks_safe_returns_none_on_fatal():
    async def boom(*a, **k):
        raise RuntimeError("fatal")

    # Force the top-level run_checks itself to blow up.
    with patch.object(fd, "run_checks", boom):
        r = _run(fd.run_checks_safe("X", -600, -90))
    assert r is None


def test_format_block_caution_tally():
    checks = [
        fd.Check(1, "f", fd.PASS, "x"),
        fd.Check(2, "p", fd.WARN, "y"),
        fd.Check(3, "l", fd.PASS, "z"),
        fd.Check(4, "t", fd.WARN, "w"),
        fd.Check(5, "doc", fd.PASS, "line"),
    ]
    res = fd.ChecksResult("T", checks, fd.aggregate_verdict(checks), "line")
    block = fd.format_checks_block(res)
    assert "CAUTION" in block
    assert "2 warn, 0 fail" in block
