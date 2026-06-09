"""R-REPORTE-SCREENER-EMBED (2026-06-09) — tests B1-B6.

The /reporte now embeds a COMPACT top-15-SHORT / top-15-LONG screener block.
Same R-SCREEN 5-gate engine as /unlockcheck (engine parity is CRITICAL — B6);
only the 30 names surface, never the long tail; squeeze stays an inviolable
exclusion; the LONG block carries the tactical/AiPear/not-mandate disclaimer.
"""
from __future__ import annotations

import asyncio

import pytest

from modules.screener_core import (
    EMBED_TOP_N,
    LONG_DISCLAIMER,
    LONG_HEADER,
    SHORT_HEADER,
    build_embedded_screener_block,
    format_embedded_screener,
    long_component_count,
    long_top,
    short_top,
)
from modules.unlock_monitor import constants as _constants
from modules.universal_screener import (
    AltGate,
    LongRead,
    ScreenResult,
    ScreenRow,
    format_screen,
    short_pass_count,
    short_score,
)


# ─── Synthetic universe builders ─────────────────────────────────────────────
def _gate(ticker: str, *, z=1.5, hurst=0.40, squeeze=False, funding=0.0001,
          data_ok=True, z_ok=True, hurst_ok=True, funding_ok=True,
          coverage=0.98, rsi=60.0, pct_k=5.0) -> AltGate:
    return AltGate(
        ticker=ticker, sector="majors", z=z, z_streak=3, hurst=hurst, rsi=rsi,
        pct_k=pct_k, higher_highs=False, funding=funding,
        funding_sign=(1 if (funding or 0) >= 0 else -1), corr=None,
        repairing=None, coverage=coverage, data_ok=data_ok, z_floor_ok=z_ok,
        z_persistent=z_ok, z_ok=z_ok, hurst_ok=hurst_ok, squeeze_flag=squeeze,
        squeeze_reasons=(["RSI70+HH"] if squeeze else []), funding_ok=funding_ok,
        counts=(data_ok and z_ok and hurst_ok and (not squeeze) and funding_ok),
        reason="",
    )


def _long_read(*, flag=False, z_over=False, mr=True, crowded=False, cap=False) -> LongRead:
    return LongRead(
        flag=flag, z_oversold=z_over, mean_reverting=mr,
        funding_crowded_short=crowded, capitulating=cap,
        capitulation_reasons=(["caída parabólica"] if cap else []),
        note="LONG ctx test",
    )


def _row(ticker: str, gate: AltGate, lr: LongRead) -> ScreenRow:
    return ScreenRow(
        ticker=ticker, sector="majors", venue_label="HL",
        liquidity_note="liq: HL", gate=gate, data_ok=gate.data_ok,
        pass_count=short_pass_count(gate), score=short_score(gate),
        short_verdict="SHORT: test", long=lr, excluded_reason="",
    )


def _short_row(ticker: str, *, z=1.5, squeeze=False, n_fail=0) -> ScreenRow:
    g = _gate(
        ticker, z=z, squeeze=squeeze,
        z_ok=(n_fail < 1), hurst_ok=(n_fail < 2), funding_ok=(n_fail < 3),
    )
    return _row(ticker, g, _long_read())


def _long_row(ticker: str, *, z=-2.0, cap=False) -> ScreenRow:
    g = _gate(ticker, z=z, z_ok=False, funding=-0.0001, funding_ok=False, rsi=35.0, pct_k=-5.0)
    lr = _long_read(flag=(not cap), z_over=True, mr=True, crowded=True, cap=cap)
    return _row(ticker, g, lr)


def _result(rows: list[ScreenRow], excluded: list[ScreenRow] | None = None) -> ScreenResult:
    ranked = [r for r in rows if r.data_ok]
    ranked.sort(key=lambda r: r.score, reverse=True)
    return ScreenResult(
        ts_utc="2026-06-09 21:00 UTC", ranked=ranked,
        long_context=[r for r in ranked if r.long.flag],
        excluded=excluded or [], universe_size=len(rows),
        n_hl=len(rows), n_var=0, n_both=0, notes=[],
        constants=_constants(),
    )


def _big_universe() -> ScreenResult:
    rows = [_short_row(f"S{i:02d}", z=2.5 - i * 0.05) for i in range(20)]
    rows += [_long_row(f"L{i:02d}", z=-2.5 + i * 0.05) for i in range(20)]
    rows += [_short_row(f"W{i:02d}", n_fail=3) for i in range(10)]  # weak 2/5 noise
    return _result(rows)


# ─── B1: /reporte assembly contains the screener section ─────────────────────
def test_b1_reporte_contains_screener(monkeypatch):
    import modules.screener_core as core
    res = _big_universe()

    async def fake_compute(advance_state=False):
        assert advance_state is False  # embed is a PURE READ
        return res

    monkeypatch.setattr(core, "compute_screen", fake_compute)
    block = asyncio.run(build_embedded_screener_block())
    assert block is not None
    assert SHORT_HEADER in block and "TOP 15 SHORT" in block
    assert LONG_HEADER in block and "TOP 15 LONG" in block


def test_b1_reporte_wiring_in_bot_source():
    """The /reporte assembly invokes the embedded block automatically (no flag)."""
    import inspect
    import bot
    src = inspect.getsource(bot.cmd_reporte)
    assert "build_embedded_screener_block" in src


# ─── B2: compact only — no long tail, no per-gate multiline, ≤15 per side ────
def test_b2_compact_only_no_long_tail():
    text = format_embedded_screener(_big_universe())
    assert "RESTO / NO-GO" not in text
    assert "DATA INSUFICIENTE" not in text
    assert "SUB-GATES" not in text          # no per-gate multi-line blocks
    assert "✅" not in text and "❌" not in text
    s_idx, l_idx = text.index(SHORT_HEADER), text.index(LONG_HEADER)
    short_sec = text[s_idx:l_idx]
    long_sec = text[l_idx:]
    assert sum(1 for ln in short_sec.splitlines() if "/5 ·" in ln) <= EMBED_TOP_N
    assert sum(1 for ln in long_sec.splitlines() if "/5 ·" in ln) <= EMBED_TOP_N
    # weak 2/5 noise names never padded in
    assert "W00" not in text


def test_b2_fewer_than_15_says_solo_n():
    rows = [_short_row(f"S{i}", z=2.0) for i in range(4)]
    text = format_embedded_screener(_result(rows))
    assert "(solo 4 con señal)" in text
    assert "0 candidatos" in text  # long side empty → explicit zero, no padding


# ─── B3: squeeze stays an inviolable exclusion in the embedded SHORT top ─────
def test_b3_squeeze_name_excluded_from_short_top():
    rows = [_short_row(f"S{i:02d}", z=1.5) for i in range(5)]
    rows.append(_short_row("SQZ", z=3.0, squeeze=True))  # huge z but in squeeze
    res = _result(rows)
    tops = short_top(res)
    assert all(r.ticker != "SQZ" for r in tops)
    text = format_embedded_screener(res)
    short_sec = text[text.index(SHORT_HEADER):text.index(LONG_HEADER)]
    assert "SQZ" not in short_sec


# ─── B4: LONG ranking is symmetric — most oversold mean-revert first ─────────
def test_b4_long_ranking_symmetric_order():
    rows = [
        _long_row("DEEP", z=-2.8),
        _long_row("MID", z=-1.8),
        _long_row("KNIFE", z=-2.9, cap=True),   # capitulating → never in top
        _short_row("HOT", z=2.5),
    ]
    res = _result(rows)
    tops = long_top(res)
    tickers = [r.ticker for r in tops]
    assert tickers[:2] == ["DEEP", "MID"]        # more oversold ranks first
    assert "KNIFE" not in tickers                # falling knife excluded
    assert "HOT" not in tickers                  # overbought is not longable
    assert all(long_component_count(r) >= 3 for r in tops)
    assert all(r.long.mean_reverting for r in tops)  # Hurst mean-revert is HARD


def test_b4_long_top_excludes_squeeze_and_trending():
    """Symmetric criteria are HARD: an oversold name that is in an up-squeeze
    or Hurst-trending (not mean-reverting) never enters the LONG top — caught
    leaking in the 2026-06-09 live smoke before this guard."""
    import dataclasses as dc
    deep = _long_row("DEEP", z=-2.5)
    sq = _long_row("SQLONG", z=-2.0)
    sq = dc.replace(sq, gate=dc.replace(sq.gate, squeeze_flag=True, squeeze_reasons=["RSI70+HH"]))
    trend = _long_row("TREND", z=-2.2)
    trend = dc.replace(trend, long=dc.replace(trend.long, mean_reverting=False))
    res = _result([deep, sq, trend])
    tickers = [r.ticker for r in long_top(res)]
    assert tickers == ["DEEP"]


# ─── B5: mandate disclaimer present in the LONG block ────────────────────────
def test_b5_mandate_disclaimer_present():
    text = format_embedded_screener(_big_universe())
    assert LONG_DISCLAIMER in text
    for must in ("táctico", "AiPear", "NO mandato", "HYPE-core"):
        assert must in text


# ─── B6 (CRITICAL): engine parity — same core function, same ranking ─────────
def test_b6_same_core_function_object():
    """The embedded screener and /unlockcheck call the SAME compute_screen."""
    import inspect
    import modules.screener_core as core
    import modules.universal_screener as scr
    import bot
    assert core.compute_screen is scr.compute_screen
    src = inspect.getsource(bot.cmd_unlockcheck)
    assert "compute_screen" in src and "format_screen" in src


def test_b6_short_ranking_parity_with_engine():
    """Embedded SHORT top = head of the engine's own ranked order (filtered only
    by the inviolable squeeze exclusion + signal floor — never re-sorted)."""
    res = _big_universe()
    tops = short_top(res)
    engine_order = [
        r.ticker for r in res.ranked
        if not (r.gate and r.gate.squeeze_flag) and r.pass_count >= 3
    ][:EMBED_TOP_N]
    assert [r.ticker for r in tops] == engine_order


def test_b6_unlockcheck_full_output_unchanged():
    """/unlockcheck keeps its FULL detail (top block + RESTO + DATA INSUF +
    long-context) — the embed is additive, format_screen untouched."""
    rows = [_short_row(f"S{i:02d}", z=2.0 - i * 0.01) for i in range(20)]
    excluded = [
        ScreenRow(
            ticker="THIN", sector="majors", venue_label="VAR",
            liquidity_note="liq: VAR",
            gate=_gate("THIN", data_ok=False, coverage=0.1),
            data_ok=False, pass_count=0, score=float("-inf"),
            short_verdict="SHORT: DATA INSUF", long=_long_read(),
            excluded_reason="VAR-only, sin velas 4h en HL",
        )
    ]
    res = _result(rows, excluded=excluded)
    full = format_screen(res, top_n=15)
    assert "MÁS SHORTEABLES" in full
    assert "RESTO / NO-GO" in full
    assert "DATA INSUFICIENTE" in full
    assert "LONG CONTEXT" in full
    assert "SUB-GATES" in full
