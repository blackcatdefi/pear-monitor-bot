"""R-SCREEN-TELEMETRY — telemetry auto-attached to screener GO candidates.

The compact telemetry block (funding now/7d, OI vs vol, distance from 7d low,
depth ±0.5%/±1%, squeeze/fails-first/z/Hurst) is attached DIRECTLY under each
5/5 GO candidate in BOTH the standalone /unlockcheck (``format_screen``) and the
embedded screener inside /reporte (``format_embedded_screener``).

Properties covered:
  T1  0 GO            → no telemetry attached, no crash, explicit "sin GO" line.
  T2  1-8 GO          → a telemetry block under EVERY GO line (and only GO).
  T3  >8 GO           → telemetry for the top-8 by ranking + a "top 8 of N" note.
  T4  per-metric n/d  → a failed feed prints n/d inside the attached block while
                        the precomputed gate fields (squeeze/fails/z/H) survive.
  T5  parity          → the embedded path and the standalone path attach the
                        BYTE-IDENTICAL telemetry block for the same GO.
  T6  no engine re-run → render_go_telemetry reads row.gate and NEVER calls
                        check_single / fetch_gate (the costly per-token path).
  T7  per-run cache   → a repeated ticker never re-fires an incremental feed.
  T8  GO-only         → 4/5 context names and squeeze names get NO telemetry.
  T9  robustness      → a hard failure on one GO degrades to a partial block,
                        never breaking the render.
  T10 wiring          → /unlockcheck + /reporte invoke render_go_telemetry.

All OFFLINE: the three incremental HL feeds and the shared ctx map are
monkeypatched so assembly + fallback are deterministic. The 5-gate engine is
NEVER re-implemented — gate fields come straight off the ScreenRow's AltGate.
"""
from __future__ import annotations

import asyncio

import pytest

from modules import telemetry as tel
from modules.screener_core import format_embedded_screener, short_top
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


# ─── Synthetic universe builders (mirror test_reporte_screener_embed) ────────
def _gate(ticker: str, *, z=1.6, hurst=0.40, squeeze=False, funding=0.0001,
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


def _long_read() -> LongRead:
    return LongRead(False, False, True, False, False, [], "LONG ctx")


def _row(ticker: str, gate: AltGate) -> ScreenRow:
    return ScreenRow(
        ticker=ticker, sector="majors", venue_label="HL",
        liquidity_note="liq: HL", gate=gate, data_ok=gate.data_ok,
        pass_count=short_pass_count(gate), score=short_score(gate),
        short_verdict="SHORT: test", long=_long_read(), excluded_reason="",
    )


def _go_row(ticker: str, *, z=2.0) -> ScreenRow:
    """A clean 5/5 GO candidate."""
    return _row(ticker, _gate(ticker, z=z))


def _ctx_row(ticker: str, *, z=1.5) -> ScreenRow:
    """A 4/5 context name (z gate fails) — never a GO."""
    return _row(ticker, _gate(ticker, z=z, z_ok=False))


def _squeeze_row(ticker: str, *, z=3.0) -> ScreenRow:
    """High-z but in squeeze → inviolably NO-GO."""
    return _row(ticker, _gate(ticker, z=z, squeeze=True))


def _result(rows: list[ScreenRow]) -> ScreenResult:
    ranked = [r for r in rows if r.data_ok]
    ranked.sort(key=lambda r: r.score, reverse=True)
    return ScreenResult(
        ts_utc="2026-06-22 00:00 UTC", ranked=ranked,
        long_context=[], excluded=[], universe_size=len(rows),
        n_hl=len(rows), n_var=0, n_both=0, notes=[], constants=_constants(),
    )


# ─── Deterministic feed patches (all incremental HL calls + shared ctx) ──────
@pytest.fixture
def patch_feeds(monkeypatch):
    calls = {"fund": [], "low": [], "depth": [], "ctx": 0}

    async def _ctx():
        calls["ctx"] += 1
        # Every GO/CTX ticker present on HL with full ctx metrics.
        return {
            f"{p}{i}": {"funding": 0.00002, "openInterest": 1000.0,
                        "markPx": 50.0, "dayNtlVlm": 1e8}
            for p in ("GO", "CTX", "SQZ") for i in range(40)
        }

    async def _avg(coin):
        calls["fund"].append(coin)
        return 0.00001, 168

    async def _low(coin):
        calls["low"].append(coin)
        return 45.0

    async def _depth(coin):
        calls["depth"].append(coin)
        return {"bid_05": 1e6, "ask_05": 8e5, "bid_10": 2.5e6, "ask_10": 2.1e6}

    async def _gate_boom(coin):
        raise AssertionError("fetch_gate must NOT be called — gate is precomputed")

    monkeypatch.setattr(tel, "fetch_ctx_map", _ctx)
    monkeypatch.setattr(tel, "fetch_funding_avg_7d", _avg)
    monkeypatch.setattr(tel, "fetch_low_7d", _low)
    monkeypatch.setattr(tel, "fetch_depth", _depth)
    monkeypatch.setattr(tel, "fetch_gate", _gate_boom)
    return calls


def _blocks(res):
    return asyncio.run(tel.render_go_telemetry(res))


# ─── T1: 0 GO ────────────────────────────────────────────────────────────────
def test_t1_zero_go_no_telemetry_no_crash(patch_feeds):
    res = _result([_ctx_row("CTX0"), _squeeze_row("SQZ0")])
    blocks, note, n_go = _blocks(res)
    assert blocks == {} and note is None and n_go == 0
    # No incremental feed should fire when there are no GO names.
    assert patch_feeds["fund"] == [] and patch_feeds["low"] == []
    # Both renders emit the explicit "no GO" line, neither crashes.
    full = format_screen(res, top_n=15, telemetry_blocks=blocks, telemetry_note=note)
    emb = format_embedded_screener(res, telemetry_blocks=blocks, telemetry_note=note)
    assert "sin candidatos GO" in full
    assert "sin candidatos GO" in emb


# ─── T2: 1-8 GO each gets a block ────────────────────────────────────────────
@pytest.mark.parametrize("n_go", [1, 2, 3, 4, 5, 6, 7, 8])
def test_t2_each_go_gets_a_block(patch_feeds, n_go):
    rows = [_go_row(f"GO{i}", z=2.5 - i * 0.05) for i in range(n_go)]
    rows.append(_ctx_row("CTX0"))  # one context name, must stay bare
    res = _result(rows)
    blocks, note, total = _blocks(res)
    assert total == n_go and note is None
    assert set(blocks) == {f"GO{i}" for i in range(n_go)}
    # Each incremental feed fired exactly once per GO (cache → no dupes).
    assert sorted(patch_feeds["fund"]) == sorted(f"GO{i}" for i in range(n_go))
    full = format_screen(res, top_n=15, telemetry_blocks=blocks, telemetry_note=note)
    emb = format_embedded_screener(res, telemetry_blocks=blocks, telemetry_note=note)
    for tk in blocks:
        assert blocks[tk] in full and blocks[tk] in emb
    # CTX0 never gets a telemetry block (GO-only).
    assert "CTX0" not in blocks


# ─── T3: >8 GO → top-8 by ranking + note ─────────────────────────────────────
def test_t3_more_than_eight_go_top8_plus_note(patch_feeds):
    # 11 GO, decreasing score by z so ranking is deterministic GO0 > GO1 > …
    rows = [_go_row(f"GO{i}", z=3.0 - i * 0.1) for i in range(11)]
    res = _result(rows)
    blocks, note, total = _blocks(res)
    assert total == 11
    assert len(blocks) == tel.MAX_TICKERS == 8
    # The 8 highest-ranked GO (top of res.ranked) — GO0..GO7.
    assert set(blocks) == {f"GO{i}" for i in range(8)}
    assert note is not None and "top 8 de 11" in note
    full = format_screen(res, top_n=15, telemetry_blocks=blocks, telemetry_note=note)
    assert note in full  # note surfaced to the reader
    # Only 8 incremental fetches fired — the overflow GO never hit the API.
    assert len(set(patch_feeds["fund"])) == 8


# ─── T4: per-metric n/d fallback inside an attached block ────────────────────
def test_t4_per_metric_nd_fallback(monkeypatch):
    async def _ctx():
        # GO0 ABSENT from ctx → funding live / OI / vol all n/d.
        return {}

    async def _avg(coin):
        return None, 0          # funding-7d feed down

    async def _low(coin):
        return None             # candle low feed down

    async def _depth(coin):
        return {"bid_05": None, "ask_05": None, "bid_10": None, "ask_10": None}

    monkeypatch.setattr(tel, "fetch_ctx_map", _ctx)
    monkeypatch.setattr(tel, "fetch_funding_avg_7d", _avg)
    monkeypatch.setattr(tel, "fetch_low_7d", _low)
    monkeypatch.setattr(tel, "fetch_depth", _depth)

    res = _result([_go_row("GO0", z=2.2)])
    blocks, _, n_go = _blocks(res)
    assert n_go == 1
    blk = blocks["GO0"]
    assert "n/d" in blk                         # failed feeds → n/d, not 0-fill
    # Precomputed gate fields STILL render (read off row.gate, never re-fetched).
    assert "sq CLEAR" in blk
    assert "fails none — 5/5 GO" in blk
    assert "z +2.20" in blk and "H 0.40" in blk


# ─── T5: standalone vs embedded telemetry block parity ───────────────────────
def test_t5_embedded_and_standalone_blocks_identical(patch_feeds):
    rows = [_go_row(f"GO{i}", z=2.4 - i * 0.1) for i in range(4)]
    res = _result(rows)
    blocks, note, _ = _blocks(res)
    full = format_screen(res, top_n=15, telemetry_blocks=blocks, telemetry_note=note)
    emb = format_embedded_screener(res, telemetry_blocks=blocks, telemetry_note=note)
    for tk, blk in blocks.items():
        # The SAME pre-rendered string is embedded verbatim in both surfaces.
        assert blk in full, f"{tk} block missing from /unlockcheck"
        assert blk in emb, f"{tk} block missing from embedded /reporte"


# ─── T6: NO engine re-run (gate read from the row, fetch_gate never called) ───
def test_t6_no_engine_rerun(patch_feeds):
    # patch_feeds installs a fetch_gate that raises if called. A clean run
    # proves the GO telemetry path reads row.gate and never re-runs check_single.
    rows = [_go_row(f"GO{i}", z=2.0 - i * 0.1) for i in range(3)]
    blocks, _, n_go = _blocks(_result(rows))
    assert n_go == 3 and len(blocks) == 3  # built purely from precomputed gates


# ─── T7: per-run cache de-dupes a repeated ticker ────────────────────────────
def test_t7_cache_dedupes_repeated_ticker(patch_feeds):
    row = _go_row("GO0", z=2.0)
    cache: dict = {}

    async def _twice():
        ctx_map = await tel.fetch_ctx_map()
        a = await tel.build_one_from_row(row, ctx_map, cache)
        b = await tel.build_one_from_row(row, ctx_map, cache)
        return a, b

    a, b = asyncio.run(_twice())
    # Two assembles of the SAME ticker → each incremental feed fired ONCE.
    assert patch_feeds["fund"] == ["GO0"]
    assert patch_feeds["low"] == ["GO0"]
    assert patch_feeds["depth"] == ["GO0"]
    assert a.funding_avg7d == b.funding_avg7d == 0.00001


# ─── T8: GO-only — context + squeeze names never get telemetry ───────────────
def test_t8_go_only_excludes_context_and_squeeze(patch_feeds):
    rows = [
        _go_row("GO0", z=2.0),
        _ctx_row("CTX0", z=1.4),     # 4/5 — context only
        _squeeze_row("SQZ0", z=3.5),  # high z but squeezing → NO-GO
    ]
    res = _result(rows)
    blocks, _, n_go = _blocks(res)
    assert set(blocks) == {"GO0"} and n_go == 1
    full = format_screen(res, top_n=15, telemetry_blocks=blocks, telemetry_note=None)
    # The CTX0/SQZ0 lines exist but carry no 📟 telemetry block beneath them.
    lines = full.splitlines()
    for i, ln in enumerate(lines):
        if ("CTX0" in ln or "SQZ0" in ln) and ln.strip().startswith(tuple("0123456789")):
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            assert "📟" not in nxt


# ─── T9: robustness — one GO blowing up degrades, never breaks the render ─────
def test_t9_one_go_failure_degrades_not_breaks(monkeypatch):
    async def _ctx():
        return {"GO0": {"funding": 0.00002, "openInterest": 1000.0,
                        "markPx": 50.0, "dayNtlVlm": 1e8},
                "GO1": {"funding": 0.00002, "openInterest": 1000.0,
                        "markPx": 50.0, "dayNtlVlm": 1e8}}

    async def _avg(coin):
        if coin == "GO1":
            raise RuntimeError("boom feed")  # one GO's feed hard-fails
        return 0.00001, 168

    async def _low(coin):
        return 45.0

    async def _depth(coin):
        return {"bid_05": 1e6, "ask_05": 8e5, "bid_10": 2.5e6, "ask_10": 2.1e6}

    monkeypatch.setattr(tel, "fetch_ctx_map", _ctx)
    monkeypatch.setattr(tel, "fetch_funding_avg_7d", _avg)
    monkeypatch.setattr(tel, "fetch_low_7d", _low)
    monkeypatch.setattr(tel, "fetch_depth", _depth)

    res = _result([_go_row("GO0", z=2.2), _go_row("GO1", z=2.1)])
    blocks, _, n_go = _blocks(res)
    assert n_go == 2
    # BOTH GO still produce a block; GO1 degrades to a partial (gate-only) one.
    assert set(blocks) == {"GO0", "GO1"}
    assert "sq CLEAR" in blocks["GO1"]            # precomputed gate survived
    assert "parcial" in blocks["GO1"]             # marked partial, never faked
    # The full render is intact and contains both blocks.
    full = format_screen(res, top_n=15, telemetry_blocks=blocks, telemetry_note=None)
    assert blocks["GO0"] in full and blocks["GO1"] in full


# ─── T10: wiring — both surfaces invoke render_go_telemetry ───────────────────
def test_t10_unlockcheck_wiring():
    import inspect
    import bot
    src = inspect.getsource(bot.cmd_unlockcheck)
    assert "render_go_telemetry" in src
    assert "telemetry_blocks" in src


def test_t10_reporte_embedded_wiring():
    import inspect
    import modules.screener_core as core
    src = inspect.getsource(core.build_embedded_screener_block)
    assert "render_go_telemetry" in src
    assert "telemetry_blocks" in src


# ─── Legacy guard: default (no telemetry param) output is unchanged ───────────
def test_legacy_no_telemetry_param_is_unchanged(patch_feeds):
    res = _result([_go_row("GO0", z=2.0)])
    # No telemetry_blocks → no 📟 block, no "sin GO" line (byte-compatible).
    full = format_screen(res, top_n=15)
    emb = format_embedded_screener(res)
    assert "📟" not in full and "sin candidatos GO" not in full
    assert "📟" not in emb and "sin candidatos GO" not in emb
