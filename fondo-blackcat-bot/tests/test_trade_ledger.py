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
W2 = "0x171b7880939d76abbc6b6b2094f54e6636f829a7"   # wallet de RETO
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
    # ROE with the assumed leverage: margin = notional / ASSUMED_LEVERAGE
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
    # R-LEDGER-FIX D3: the snapshot posted margin_used=333.0 on a 1000 notional,
    # so leverage is DERIVED (1000/333) and outranks the reported 3.0x.
    assert row["margin_open"] == pytest.approx(333.0)
    assert row["leverage"] == pytest.approx(1000.0 / 333.0)
    assert row["leverage_source"] == "derived"


def test_leverage_derived_from_posted_margin_beats_assumed(monkeypatch):
    """D3: leverage comes from notional_open / margin actually posted; the
    env default is the LAST resort and it is 3x, not 5x."""
    assert tl.ASSUMED_LEVERAGE == pytest.approx(3.0)
    # derived, in band
    assert tl.derive_leverage(3000.0, 1000.0) == pytest.approx(3.0)
    # out of band / unusable → None, never a bogus ROE
    assert tl.derive_leverage(3000.0, 0.0) is None
    assert tl.derive_leverage(None, 1000.0) is None
    assert tl.derive_leverage(3000.0, 1.0) is None       # 3000x → rejected
    # precedence: derived > live > assumed
    assert tl.resolve_leverage(3000.0, 1000.0, 5.0) == (pytest.approx(3.0), "derived")
    assert tl.resolve_leverage(3000.0, None, 5.0) == (pytest.approx(5.0), "live")
    assert tl.resolve_leverage(3000.0, None, None) == (
        pytest.approx(tl.ASSUMED_LEVERAGE), "assumed")
    # '~' marker ONLY on assumed values
    base = {"coin": "ETH", "side": "LONG", "avg_entry": 100.0, "avg_exit": 110.0,
            "max_size": 10.0, "gross_pnl": 100.0, "fees_total": 2.0,
            "funding_net": 0.0, "net_pnl": 98.0, "roe_pct": 29.4,
            "open_ts": 1_000_000, "close_ts": 1_000_000 + H, "wallet": W}
    assumed = tl.format_position_line({**base, "leverage": 3.0,
                                       "leverage_source": "assumed"})
    derived = tl.format_position_line({**base, "leverage": 3.0,
                                       "leverage_source": "derived"})
    live = tl.format_position_line({**base, "leverage": 3.0,
                                    "leverage_source": "live"})
    assert "ROE ~" in assumed and "@3x" in assumed
    assert "ROE ~" not in derived and "@3x" in derived
    assert "ROE ~" not in live


def test_assumed_roe_uses_3x_not_5x():
    """The inflated-ROE regression: a 1000 notional basket leg at 3x has
    margin 333.33, so ROE must be ~3/5 of what the old 5x default printed."""
    _seed_closures()
    row = [r for r in tl.closures_between(0, 10 ** 15) if r["coin"] == "SOL"][0]
    assert row["leverage"] == pytest.approx(3.0)
    assert row["leverage_source"] == "assumed"
    roe_3x = 102.0 / (1000.0 / 3.0) * 100
    assert row["roe_pct"] == pytest.approx(roe_3x)
    assert row["roe_pct"] == pytest.approx(102.0 / (1000.0 / 5.0) * 100 * 3 / 5)


# ─── D1: a failing funding fetch surfaces instead of storing zeros ──────────

def test_funding_fetch_failure_raises_and_marks_unhealthy(monkeypatch):
    """D1 root cause: _fetch_funding_paged used to catch-log-break and return
    [], so a 429 became funding 0.00 on every leg. Now it raises, the partial
    rows are still persisted, and the wallet is marked unhealthy."""
    _seed_closures()

    async def _ok_fills(wallet, start_ms):
        return []

    async def _boom_funding(wallet, start_ms):
        raise tl.LedgerSyncError("userFunding: page 3 failed after 3 attempts (429)",
                                 wallet=wallet, kind="userFunding",
                                 partial=[{"time": 1_000_000 + 45 * MIN,
                                           "delta": {"type": "funding", "coin": "BTC",
                                                     "usdc": "-1.5", "szi": 10,
                                                     "fundingRate": 0.0001}}])

    monkeypatch.setattr(tl, "_fetch_fills_paged", _ok_fills)
    monkeypatch.setattr(tl, "_fetch_funding_paged", _boom_funding)

    with pytest.raises(tl.LedgerSyncError):
        asyncio.run(tl.sync_wallet(W))

    h = tl.sync_health()[W]
    assert h["ok"] == 0 and h["funding_ok"] == 0 and h["fills_ok"] == 1
    assert "429" in h["detail"]
    # partial funding was NOT thrown away
    row = [r for r in tl.closures_between(0, 10 ** 15) if r["coin"] == "BTC"][0]
    assert row["funding_net"] == pytest.approx(-1.5)
    # and the incompleteness is announced at the top of the report section
    out = tl.render_cierres_section(0, 10 ** 13)
    assert "LEDGER INCOMPLETO" in out
    assert "funding" in out.lower()


def test_sync_all_alerts_once_on_failed_wallet_and_funding_gap(monkeypatch):
    """D1+D2: a failed wallet and an empty-funding window each produce ONE
    deduped alert instead of a silent zero / a missing wallet."""
    _seed_closures()
    monkeypatch.setattr(tl, "ledger_wallets", lambda: {W: "core", W2: "reto"})

    async def _fail(wallet):
        raise tl.LedgerSyncError("userFillsByTime: page 2 failed (429)",
                                 wallet=wallet, kind="userFillsByTime")

    monkeypatch.setattr(tl, "sync_wallet", _fail)
    sent: list[str] = []
    res = asyncio.run(tl.sync_all(send=sent.append))
    assert res["ok"] == [] and set(res["failed"]) == {W, W2}
    joined = "\n".join(sent)
    assert joined.count("LEDGER SYNC FALLIDO") == 2
    assert res["alerts"] >= 2
    # second run in the cooldown window → deduped, no repeat spam
    sent2: list[str] = []
    res2 = asyncio.run(tl.sync_all(send=sent2.append))
    assert set(res2["failed"]) == {W, W2}
    assert sent2 == []


def test_funding_gap_detects_closures_without_carry():
    """The D1 signature: closures inside the window but zero funding rows."""
    fills = [
        _fill("ETH", "B", 100, 10, 1_000_000, fee=1.0, d="Open Long"),
        _fill("ETH", "A", 110, 10, 1_000_000 + H, pnl=100.0, fee=1.0, d="Close Long"),
    ]
    tl._store_fills(W, fills)
    tl.rebuild_wallet_positions(W)
    assert tl.funding_gap(W, 0, 10 ** 15) is True
    tl._store_funding(W, [{"time": 1_000_000 + 30 * MIN,
                           "delta": {"type": "funding", "coin": "ETH",
                                     "usdc": "-2.5", "szi": 10,
                                     "fundingRate": 0.0001}}])
    assert tl.funding_gap(W, 0, 10 ** 15) is False
    # no closures in the window → not a gap, just a quiet window
    assert tl.funding_gap(W, 10 ** 14, 10 ** 15) is False


def test_sync_all_alerts_when_challenge_wallet_missing_from_scope(monkeypatch):
    """D2: one wallet in scope means the challenge wallet cannot appear in
    the track record — that must be loud, not invisible."""
    monkeypatch.setattr(tl, "ledger_wallets", lambda: {W: "core"})

    async def _noop(wallet):
        return 0

    monkeypatch.setattr(tl, "sync_wallet", _noop)
    sent: list[str] = []
    res = asyncio.run(tl.sync_all(send=sent.append))
    assert res["ok"] == [W]
    assert any("SCOPE DEGRADADO" in s for s in sent)
    # two wallets in scope → no scope alert
    monkeypatch.setattr(tl, "ledger_wallets", lambda: {W: "core", W2: "reto"})
    sent2: list[str] = []
    asyncio.run(tl.sync_all(send=sent2.append))
    assert not any("SCOPE DEGRADADO" in s for s in sent2)


# ─── D2: both wallets + the COMBINED cycle total ────────────────────────────

def test_two_wallet_window_renders_both_wallets_and_combined_total():
    """THE public-track-record test: when the same basket cycle ran on the
    core AND the challenge wallet, the section must render both wallets,
    both subtotals, and the COMBINED cycle line."""
    base = 1_755_648_000_000
    for wallet, pnl in ((W, 100.0), (W2, 25.0)):
        fills = [
            _fill("ETH", "B", 100, 10, base, fee=1.0, tid=hash(wallet) % 10 ** 6 + 1,
                  d="Open Long"),
            _fill("SOL", "B", 50, 20, base + 5 * MIN, fee=1.0,
                  tid=hash(wallet) % 10 ** 6 + 2, d="Open Long"),
            _fill("ETH", "A", 110, 10, base + H, pnl=pnl, fee=1.0,
                  tid=hash(wallet) % 10 ** 6 + 3, d="Close Long"),
            _fill("SOL", "A", 55, 20, base + H, pnl=pnl, fee=1.0,
                  tid=hash(wallet) % 10 ** 6 + 4, d="Close Long"),
        ]
        tl._store_fills(wallet, fills)
        tl.rebuild_wallet_positions(wallet)

    rows = tl.closures_between(0, 10 ** 15)
    assert len({r["wallet"] for r in rows}) == 2
    tag = rows[0]["cycle_tag"]
    assert tag and all(r["cycle_tag"] == tag for r in rows)

    out = tl.render_cierres_section(0, 10 ** 13)
    # both wallets present, each with its own subtotal
    assert W[:6] in out and W2[:6] in out
    assert out.count("subtotal wallet") == 2
    # the combined cycle line — 4 legs across both wallets
    assert f"COMBINADO {tag}" in out
    combined_line = [l for l in out.splitlines() if l.startswith("COMBINADO")][0]
    assert "4 pata(s)" in combined_line
    # NET = gross(2*100 + 2*25) - fees(8) = 242
    assert "NET +$242.00" in combined_line
    assert "TOTAL: 4 pata(s)" in out


def test_sync_failure_during_close_detection_never_loses_the_alert(monkeypatch):
    """D1 fallout guard: sync_wallet now RAISES on a failed HL read. That must
    not eat the close — the open snapshot survives so the next cycle retries,
    and the alert fires once the sync recovers."""
    _seed_closures()
    calls = {"n": 0}

    async def _flaky_sync(wallet):
        calls["n"] += 1
        if calls["n"] == 1:
            raise tl.LedgerSyncError("userFunding: page 1 failed (429)",
                                     wallet=wallet, kind="userFunding")
        return 0

    monkeypatch.setattr(tl, "sync_wallet", _flaky_sync)
    sent: list[str] = []
    open_pos = [{"coin": "BTC", "size": 10.0, "leverage": 3.0,
                 "cum_funding_since_open": 0.0, "entry_px": 100.0}]
    asyncio.run(tl.run_close_alerts(None, _payload(open_pos), send=sent.append))
    # position gone + sync blows up → NO alert, but the snapshot is kept
    n = asyncio.run(tl.run_close_alerts(None, _payload([]), send=sent.append))
    assert n == 0 and sent == []
    con = tl._conn()
    try:
        assert con.execute("SELECT COUNT(*) c FROM ledger_open_snap "
                           "WHERE coin='BTC'").fetchone()["c"] == 1
    finally:
        con.close()
    # next cycle the sync works → the close is alerted exactly once
    n2 = asyncio.run(tl.run_close_alerts(None, _payload([]), send=sent.append))
    assert n2 == 1 and len(sent) == 1
    n3 = asyncio.run(tl.run_close_alerts(None, _payload([]), send=sent.append))
    assert n3 == 0 and len(sent) == 1


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


def test_first_run_cursor_seeded_no_history_dump():
    """First /reporte after deploy must NOT dump the whole backfilled
    horizon: the cursor is seeded to (last closure - lookback)."""
    # Closures months apart: old ones at ~epoch, recent one "now"-ish
    old_close = 1_000_000
    recent_close = old_close + 200 * 24 * H  # ~200 days later
    fills = [
        _fill("ETH", "B", 100, 1, old_close - H, fee=1, tid=11, d="Open Long"),
        _fill("ETH", "A", 110, 1, old_close, pnl=10, fee=1, tid=12, d="Close Long"),
        _fill("SOL", "B", 50, 1, recent_close - H, fee=1, tid=13, d="Open Long"),
        _fill("SOL", "A", 55, 1, recent_close, pnl=5, fee=1, tid=14, d="Close Long"),
    ]
    tl._store_fills(W, fills)
    tl.rebuild_wallet_positions(W)
    assert tl.get_report_cursor() == 0
    tl.ensure_report_cursor_seeded()
    cur = tl.get_report_cursor()
    assert cur == recent_close - tl.FIRST_RUN_LOOKBACK_MS
    # window from the seeded cursor contains ONLY the recent closure
    rows = tl.closures_between(cur, recent_close + 1)
    assert [r["coin"] for r in rows] == ["SOL"]
    out = tl.render_cierres_section(cur, recent_close + 1)
    assert "SOL" in out and "ETH" not in out
    assert "historial anterior" in out and "/cierres" in out
    # after a successful send the one-shot note is consumed
    tl.set_report_cursor(recent_close + 1)
    out2 = tl.render_cierres_section(tl.get_report_cursor(), recent_close + 2)
    assert "historial anterior" not in out2
    # seeding is idempotent — never moves an already-set cursor
    tl.ensure_report_cursor_seeded()
    assert tl.get_report_cursor() == recent_close + 1


def test_section_render_cap_totals_never_truncated(monkeypatch):
    """Cap the rendered lines at SECTION_MAX_ROWS but compute subtotals and
    the final total over the FULL window."""
    monkeypatch.setattr(tl, "SECTION_MAX_ROWS", 2)
    fills = []
    coins = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    for i, coin in enumerate(coins):
        t0 = 1_000_000 + i * 20 * MIN
        t1 = 2_000_000 + i * H  # closes spread out; all in window
        fills += [
            _fill(coin, "B", 100, 1, t0, fee=1.0, tid=100 + i, d="Open Long"),
            _fill(coin, "A", 110, 1, t1, pnl=10.0, fee=1.0, tid=200 + i, d="Close Long"),
        ]
    tl._store_fills(W, fills)
    tl.rebuild_wallet_positions(W)
    out = tl.render_cierres_section(0, 10 ** 13)
    # only the 2 most recent closures rendered
    assert "EEE" in out and "DDD" in out
    assert "AAA LONG" not in out and "BBB LONG" not in out
    assert "3 cierre(s) mas antiguos omitidos" in out
    # totals over the FULL window: 5 legs, gross 50, fees 10, NET 40
    assert "TOTAL: 5 pata(s)" in out
    assert "NET +$40.00" in out
    assert "subtotal wallet: 5 pata(s)" in out


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
