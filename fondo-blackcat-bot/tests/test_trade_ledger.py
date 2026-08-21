"""R-TRADE-LEDGER — permanent closed-trade ledger tests.

Covers the acceptance suite mandated by the task:
  * position reconciliation from fixture fills (partials, flips,
    multi-fill closes, spot exclusion, open-lifecycle exclusion)
  * NET formula: NET = gross - fees + funding
  * report-cursor no-gap / no-dup window property
  * basket cycle clustering (15-min window, same wallet)
  * real-time close alert dedup lifecycle (one alert per close, ever)
  * /cierres and CIERRES-section rendering
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))

from modules import alert_dedup
from modules import trade_ledger as tl

W = "0xc7ae23316b47f7e75f455f53ad37873a18351505"
MIN = 60_000
H = 3_600_000


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(tl, "DB_PATH", str(tmp_path / "ledger.db"))
    monkeypatch.setattr(alert_dedup, "DB_PATH", str(tmp_path / "dedup.db"))
    monkeypatch.setattr(tl, "_sync_lock", None)
    yield


def _fill(coin, side, px, sz, t, *, pnl=0.0, fee=0.0, tid=None, d=""):
    if not d:
        d = "Open Long" if side == "B" else "Close Long"
    return {"coin": coin, "side": side, "px": px, "sz": sz, "time": t,
            "closedPnl": pnl, "fee": fee, "tid": tid or (t * 10), "dir": d,
            "oid": 1, "startPosition": 0, "feeToken": "USDC"}


# ─── NET formula ─────────────────────────────────────────────────────────────

def test_net_formula():
    # THE formula: NET = gross - fees + funding (funding>0 = cobrado)
    assert tl.compute_net(300.0, 4.0, -3.0) == pytest.approx(293.0)
    assert tl.compute_net(100.0, 10.0, 5.0) == pytest.approx(95.0)
    assert tl.compute_net(-50.0, 2.0, 0.0) == pytest.approx(-52.0)


# ─── reconciliation ──────────────────────────────────────────────────────────

def test_reconcile_partial_opens_multi_fill_close():
    fills = [
        _fill("ETH", "B", 2000, 1, 1000, fee=1.0, d="Open Long"),
        _fill("ETH", "B", 2100, 1, 1000 + 5 * MIN, fee=1.0, d="Open Long"),
        _fill("ETH", "A", 2200, 1.5, 1000 + H, pnl=225.0, fee=1.5, d="Close Long"),
        _fill("ETH", "A", 2200, 0.5, 1000 + H + MIN, pnl=75.0, fee=0.5, d="Close Long"),
    ]
    closed = tl.reconcile_positions(fills)
    assert len(closed) == 1
    p = closed[0]
    assert p["side"] == "LONG"
    assert p["avg_entry"] == pytest.approx(2050.0)
    assert p["avg_exit"] == pytest.approx(2200.0)
    assert p["max_size"] == pytest.approx(2.0)
    assert p["open_fills"] == 2 and p["close_fills"] == 2
    assert p["gross_pnl"] == pytest.approx(300.0)
    assert p["fees_total"] == pytest.approx(4.0)
    assert p["open_ts"] == 1000 and p["close_ts"] == 1000 + H + MIN
    assert p["notional_open"] == pytest.approx(2050.0 * 2.0)


def test_reconcile_direction_flip_splits_fill():
    fills = [
        _fill("SOL", "B", 100, 1, 1000, fee=1.0, d="Open Long"),
        # Sell 3: closes the 1 LONG (pnl 10) + opens 2 SHORT — fee pro-rated
        _fill("SOL", "A", 110, 3, 2000, pnl=10.0, fee=3.0, d="Long > Short"),
        _fill("SOL", "B", 105, 2, 3000, pnl=10.0, fee=1.0, d="Close Short"),
    ]
    closed = tl.reconcile_positions(fills)
    assert len(closed) == 2
    lng, sht = closed
    assert lng["side"] == "LONG" and sht["side"] == "SHORT"
    assert lng["gross_pnl"] == pytest.approx(10.0)
    assert lng["fees_total"] == pytest.approx(1.0 + 3.0 / 3)  # open fee + 1/3 flip
    assert lng["close_ts"] == 2000
    assert sht["open_ts"] == 2000 and sht["close_ts"] == 3000
    assert sht["avg_entry"] == pytest.approx(110.0)
    assert sht["max_size"] == pytest.approx(2.0)
    assert sht["fees_total"] == pytest.approx(3.0 * 2 / 3 + 1.0)


def test_reconcile_excludes_spot_and_open_lifecycles():
    fills = [
        _fill("@107", "B", 1, 100, 1000, d="Buy"),          # spot → ignored
        _fill("HYPE/USDC", "A", 30, 5, 1000, d="Sell"),     # spot → ignored
        _fill("BTC", "B", 60000, 0.5, 1000, fee=5, d="Open Long"),  # still open
        _fill("ETH", "A", 2500, 2, 1000, fee=2, d="Open Short"),
        _fill("ETH", "B", 2400, 2, 2000, pnl=200.0, fee=2, d="Close Short"),
    ]
    closed = tl.reconcile_positions(fills)
    assert [p["coin"] for p in closed] == ["ETH"]
    assert closed[0]["side"] == "SHORT"
    assert closed[0]["gross_pnl"] == pytest.approx(200.0)


# ─── cycle clustering (Part 3) ───────────────────────────────────────────────

def test_cluster_cycles_15min_window_and_singles():
    base = 1_755_648_000_000  # 2025-08-20 00:00 UTC
    ps = [
        {"coin": "A", "open_ts": base},
        {"coin": "B", "open_ts": base + 5 * MIN},
        {"coin": "C", "open_ts": base + 14 * MIN},
        {"coin": "D", "open_ts": base + 3 * H},          # single → no tag
        {"coin": "E", "open_ts": base + 6 * H},          # 2nd cycle same day
        {"coin": "F", "open_ts": base + 6 * H + MIN},
    ]
    tl.cluster_cycles(ps)
    tags = {p["coin"]: p["cycle_tag"] for p in ps}
    assert tags["A"] == tags["B"] == tags["C"]
    assert tags["A"] is not None and tags["A"].startswith("ciclo ")
    assert tags["D"] is None
    assert tags["E"] == tags["F"] and tags["E"].endswith("#2")


# ─── store + rebuild + cursor property ───────────────────────────────────────

def _seed_closures(wallet=W):
    """3 closed lifecycles at close_ts ~1h/2h/3h + funding rows."""
    fills = []
    for i, coin in enumerate(["ETH", "SOL", "BTC"]):
        t0 = 1_000_000 + i * 10 * MIN
        t1 = 1_000_000 + (i + 1) * H
        fills += [
            _fill(coin, "B", 100, 10, t0, fee=1.0, d="Open Long"),
            _fill(coin, "A", 110, 10, t1, pnl=100.0, fee=1.0, d="Close Long"),
        ]
    tl._store_fills(wallet, fills)
    tl._store_funding(wallet, [
        {"time": 1_000_000 + 30 * MIN, "delta": {"type": "funding", "coin": "ETH",
                                                 "usdc": -2.5, "szi": 10, "fundingRate": 0.0001}},
        {"time": 1_000_000 + 90 * MIN, "delta": {"type": "funding", "coin": "SOL",
                                                 "usdc": 4.0, "szi": 10, "fundingRate": -0.0001}},
    ])
    return tl.rebuild_wallet_positions(wallet)


def test_rebuild_funding_attribution_net_and_roe_assumed():
    n = _seed_closures()
    assert n == 3
    rows = tl.closures_between(0, 10 ** 15)
    by = {r["coin"]: r for r in rows}
    # ETH paid 2.5 funding → NET = 100 - 2 - 2.5
    assert by["ETH"]["funding_net"] == pytest.approx(-2.5)
    assert by["ETH"]["net_pnl"] == pytest.approx(100 - 2 - 2.5)
    # SOL received 4.0 → NET = 100 - 2 + 4
    assert by["SOL"]["funding_net"] == pytest.approx(4.0)
    assert by["SOL"]["net_pnl"] == pytest.approx(102.0)
    # BTC no funding rows
    assert by["BTC"]["funding_net"] == pytest.approx(0.0)
    # ROE with assumed leverage 5: margin = 1000/5 = 200
    assert by["SOL"]["leverage_source"] == "assumed"
    assert by["SOL"]["roe_pct"] == pytest.approx(102.0 / (1000.0 / tl.ASSUMED_LEVERAGE) * 100)
    # rebuild is idempotent — no duplicate rows
    tl.rebuild_wallet_positions(W)
    assert len(tl.closures_between(0, 10 ** 15)) == 3


def test_report_cursor_no_gap_no_dup():
    _seed_closures()
    rows = tl.closures_between(0, 10 ** 15)
    all_ids = sorted(r["id"] for r in rows)
    cuts = sorted(r["close_ts"] for r in rows)
    # Simulate 3 consecutive reports advancing the cursor mid-stream
    assert tl.get_report_cursor() == 0
    seen: list[int] = []
    for cut in [cuts[0], cuts[1], 10 ** 15]:
        prev = tl.get_report_cursor()
        win = tl.closures_between(prev, cut)
        seen.extend(r["id"] for r in win)
        tl.set_report_cursor(cut)
    assert sorted(seen) == all_ids          # no gaps
    assert len(seen) == len(set(seen))      # no dups
    # next report with no new closures → empty window
    assert tl.closures_between(tl.get_report_cursor(), 10 ** 15 + 1) == []


# ─── real-time close alerts (Part 4) ────────────────────────────────────────

def _payload(positions):
    return [{"status": "ok", "data": {"wallet": W, "label": "core",
                                      "positions": positions}}]


def test_close_alert_dedup_lifecycle(monkeypatch):
    _seed_closures()

    async def _fake_sync(wallet):  # no network in tests
        return 0

    monkeypatch.setattr(tl, "sync_wallet", _fake_sync)
    sent: list[str] = []

    open_pos = [{"coin": "BTC", "size": 10.0, "leverage": 3.0,
                 "cum_funding_since_open": 1.2, "entry_px": 100.0,
                 "margin_used": 333.0}]

    # cycle 1: BTC open → snapshot only, no alert
    n1 = asyncio.run(tl.run_close_alerts(None, _payload(open_pos), send=sent.append))
    assert n1 == 0 and sent == []
    # cycle 2: BTC gone → ONE close alert with economics + formula footer
    n2 = asyncio.run(tl.run_close_alerts(None, _payload([]), send=sent.append))
    assert n2 == 1 and len(sent) == 1
    assert "POSICION CERRADA" in sent[0]
    assert "BTC" in sent[0] and "NET" in sent[0]
    assert "NET = gross - fees + funding" in sent[0]
    # live leverage captured → ROE without the '~' assumed marker
    assert "ROE ~" not in sent[0]
    # cycle 3+: still closed → dedup, NO second alert ever
    n3 = asyncio.run(tl.run_close_alerts(None, _payload([]), send=sent.append))
    assert n3 == 0 and len(sent) == 1
    # live snapshot funding: cumFunding.sinceOpen=+1.2 (paid) → stored as -1.2
    row = [r for r in tl.closures_between(0, 10 ** 15) if r["coin"] == "BTC"][0]
    assert row["funding_live_snapshot"] == pytest.approx(-1.2)
    assert row["leverage"] == pytest.approx(3.0)
    assert row["leverage_source"] == "live"


def test_failed_wallet_fetch_never_fakes_close(monkeypatch):
    _seed_closures()

    async def _fake_sync(wallet):
        return 0

    monkeypatch.setattr(tl, "sync_wallet", _fake_sync)
    sent: list[str] = []
    open_pos = [{"coin": "BTC", "size": 10.0, "leverage": 3.0,
                 "cum_funding_since_open": 0.0, "entry_px": 100.0}]
    asyncio.run(tl.run_close_alerts(None, _payload(open_pos), send=sent.append))
    # wallet fetch FAILS this cycle → position must NOT be treated as closed
    bad = [{"status": "error", "error": "timeout"}]
    n = asyncio.run(tl.run_close_alerts(None, bad, send=sent.append))
    assert n == 0 and sent == []
    # recovery: wallet ok again and position truly gone → alert fires now
    n2 = asyncio.run(tl.run_close_alerts(None, _payload([]), send=sent.append))
    assert n2 == 1 and len(sent) == 1


def test_stale_cache_payload_never_fakes_close(monkeypatch):
    """H1: portfolio returns status='ok' + stale=True when every retry
    failed and it served the cache — that must NOT drive close detection."""
    _seed_closures()

    async def _fake_sync(wallet):
        return 0

    monkeypatch.setattr(tl, "sync_wallet", _fake_sync)
    sent: list[str] = []
    open_pos = [{"coin": "BTC", "size": 10.0, "leverage": 3.0,
                 "cum_funding_since_open": 0.0, "entry_px": 100.0}]
    asyncio.run(tl.run_close_alerts(None, _payload(open_pos), send=sent.append))
    # stale=ok payload WITHOUT the position → must not fake a close
    stale = [{"status": "ok", "stale": True, "stale_reason": "fetch_failed_after_retries",
              "data": {"wallet": W, "label": "core", "positions": []}}]
    n = asyncio.run(tl.run_close_alerts(None, stale, send=sent.append))
    assert n == 0 and sent == []
    # live fetch confirms the close → alert fires exactly once
    n2 = asyncio.run(tl.run_close_alerts(None, _payload([]), send=sent.append))
    assert n2 == 1 and len(sent) == 1


# ─── rendering (Parts 2 & 5) ────────────────────────────────────────────────

def test_render_cierres_section_empty_and_populated():
    empty = tl.render_cierres_section(0, 5_000_000_000)
    assert "CIERRES DESDE EL ULTIMO REPORTE" in empty
    assert "Sin cierres desde el ultimo reporte." in empty

    _seed_closures()
    out = tl.render_cierres_section(0, 5_000_000_000_000)
    assert "CIERRES DESDE EL ULTIMO REPORTE" in out
    for coin in ("ETH", "SOL", "BTC"):
        assert coin in out
    assert "subtotal wallet" in out
    assert "TOTAL: 3 pata(s)" in out
    assert "NET = gross - fees + funding" in out
    assert "cobra" in out and "paga" in out


def test_render_cierres_command_variants():
    _seed_closures()
    # default: last N + all-time totals
    out = tl.render_cierres_command(None)
    assert "LEDGER DE CIERRES" in out and "ALL-TIME (3 cierres)" in out
    # explicit N
    out2 = tl.render_cierres_command("2")
    assert "Ultimos 2 cierres" in out2
    # day filter (fixture closes are all in 1970-01-01 epoch-ms space)
    out3 = tl.render_cierres_command("1970-01-01")
    assert "Cierres del 1970-01-01" in out3 and "ETH" in out3
    # bad arg → usage
    assert "Uso:" in tl.render_cierres_command("garbage")
    # unknown cycle → helpful list
    assert "no encontrado" in tl.render_cierres_command("ciclo 2099-01-01")


def test_cycle_detail_and_subtotal_render():
    # Two legs opened within 15 min → one cycle, then closed
    fills = [
        _fill("ETH", "B", 100, 1, 1_000_000, fee=0.5, tid=1, d="Open Long"),
        _fill("SOL", "B", 50, 2, 1_000_000 + 5 * MIN, fee=0.5, tid=2, d="Open Long"),
        _fill("ETH", "A", 110, 1, 1_000_000 + H, pnl=10.0, fee=0.5, tid=3, d="Close Long"),
        _fill("SOL", "A", 45, 2, 1_000_000 + H, pnl=-10.0, fee=0.5, tid=4, d="Close Long"),
    ]
    tl._store_fills(W, fills)
    tl.rebuild_wallet_positions(W)
    rows = tl.closures_between(0, 10 ** 15)
    tag = rows[0]["cycle_tag"]
    assert tag and all(r["cycle_tag"] == tag for r in rows)
    detail = tl.render_cierres_command(tag)
    assert f"Detalle {tag}" in detail and "fills:" in detail
    sub = tl.render_cycle_subtotal(W, tag)
    assert "CICLO CERRADO" in sub and "2 pata(s)" in sub


def test_format_position_line_economics():
    _seed_closures()
    row = [r for r in tl.closures_between(0, 10 ** 15) if r["coin"] == "SOL"][0]
    line = tl.format_position_line(row)
    assert "SOL LONG" in line
    assert "100" in line and "110" in line      # entry → exit
    assert "fees -$2.00" in line
    assert "funding +$4.00 (cobra)" in line
    assert "NET +$102.00" in line
    assert "ROE ~" in line                       # assumed leverage marker
