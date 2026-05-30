"""R-VARIATIONAL — tests for the Farm-the-DUMP funding scanner + watches.

Covers:
  * annualization math (verified against the live BTC anchor)
  * listing/stats parsing + robustness to missing/garbage fields
  * negative-funding scan threshold + ordering
  * the reversion-trigger condition (the heart of the strategy)
  * SQLite watch persistence surviving a simulated restart
  * 60s in-memory cache + get_market (network mocked)
"""
from __future__ import annotations

import asyncio
import os

import pytest

from modules import variational as var
from modules import variational_alerts as va


# ─── Annualization ───────────────────────────────────────────────────────────
def test_annualize_btc_anchor():
    # Live anchor 2026-05-30: BTC funding_rate=0.086232 per 8h interval.
    # 0.086232 * (31_536_000 / 28_800) = 0.086232 * 1095 ≈ 94.42 %.
    ann = var.annualize_funding(0.086232, 28800)
    assert abs(ann - 94.42) < 0.5
    # Sanity: NOT the impossible 9442% fraction-reading.
    assert ann < 200


def test_annualize_interval_equivalences():
    # 8h interval → ×3×365 = ×1095 ; 1h interval → ×24×365 = ×8760.
    assert var.annualize_funding(1.0, 28800) == pytest.approx(3 * 365)
    assert var.annualize_funding(1.0, 3600) == pytest.approx(24 * 365)
    assert var.annualize_funding(1.0, 14400) == pytest.approx(6 * 365)


def test_annualize_negative_preserves_sign():
    assert var.annualize_funding(-0.5, 28800) < 0


def test_annualize_bad_interval_raises():
    for bad in (0, -1, None):
        with pytest.raises(ValueError):
            var.annualize_funding(0.1, bad)  # type: ignore[arg-type]


# ─── _to_float robustness ────────────────────────────────────────────────────
def test_to_float_handles_garbage():
    assert var._to_float("1.5") == 1.5
    assert var._to_float(2) == 2.0
    assert var._to_float(None) is None
    assert var._to_float("nope") is None
    assert var._to_float(float("nan")) is None
    assert var._to_float(float("inf")) is None


# ─── parse_listing ───────────────────────────────────────────────────────────
def _btc_listing():
    return {
        "ticker": "BTC",
        "name": "Bitcoin",
        "mark_price": "93787.96",
        "volume_24h": "1058107020.46",
        "open_interest": {
            "long_open_interest": "113883049.01",
            "short_open_interest": "82403040.51",
        },
        "funding_rate": "0.086232",
        "funding_interval_s": 28800,
    }


def test_parse_listing_full():
    m = var.parse_listing(_btc_listing())
    assert m is not None
    assert m.ticker == "BTC"
    assert m.funding_interval_s == 28800
    assert m.mark_price == pytest.approx(93787.96)
    assert m.open_interest_usd == pytest.approx(113883049.01 + 82403040.51)
    assert m.annualized_pct == pytest.approx(94.42, abs=0.5)


def test_parse_listing_skips_when_missing_required():
    # No funding_rate → skip (never fabricate).
    bad = _btc_listing()
    del bad["funding_rate"]
    assert var.parse_listing(bad) is None
    # interval 0 → skip.
    bad2 = _btc_listing()
    bad2["funding_interval_s"] = 0
    assert var.parse_listing(bad2) is None
    # no ticker → skip.
    bad3 = _btc_listing()
    del bad3["ticker"]
    assert var.parse_listing(bad3) is None
    # garbage funding → skip.
    bad4 = _btc_listing()
    bad4["funding_rate"] = "n/a"
    assert var.parse_listing(bad4) is None


def test_parse_listing_tolerates_missing_optionals():
    m = var.parse_listing({"ticker": "X", "funding_rate": "-1.0", "funding_interval_s": 14400})
    assert m is not None
    assert m.mark_price is None
    assert m.volume_24h is None
    assert m.open_interest_usd is None


def test_parse_stats_and_errors():
    raw = {"listings": [_btc_listing(), {"ticker": "BAD"}]}
    out = var.parse_stats(raw)
    assert [m.ticker for m in out] == ["BTC"]  # BAD skipped
    with pytest.raises(var.VariationalError):
        var.parse_stats({})  # no listings
    with pytest.raises(var.VariationalError):
        var.parse_stats([])  # type: ignore[arg-type]


# ─── scan ────────────────────────────────────────────────────────────────────
def test_scan_threshold_and_order():
    def mk(t, ann):
        return var.VariationalMarket(t, 0.0, 28800, ann, None, None, None)
    markets = [mk("A", -100), mk("B", -800), mk("C", -550), mk("D", 50)]
    q = var.scan_negative_funding(markets, -500)
    assert [m.ticker for m in q] == ["B", "C"]  # most negative first, A & D excluded


def test_funding_threshold_env(monkeypatch):
    monkeypatch.setenv("VARIATIONAL_FUNDING_THRESHOLD", "-750")
    assert var.funding_threshold() == -750.0
    monkeypatch.setenv("VARIATIONAL_FUNDING_THRESHOLD", "garbage")
    assert var.funding_threshold() == -500.0  # fallback


def test_format_funding_scan_empty_and_full():
    empty = var.format_funding_scan([], -500, 465)
    assert "Ningún activo" in empty
    m = var.parse_listing(_btc_listing())
    full = var.format_funding_scan([m], -500, 465)
    assert "BTC" in full and "anual" in full


# ─── Reversion trigger (the strategy's core) ─────────────────────────────────
def test_reversion_target():
    assert va.reversion_target(-600, 0.5) == -300


def test_has_reverted_basic():
    # baseline -600 → fires when current ≥ -300.
    assert va.has_reverted(-600, -300, 0.5) is True   # exactly at target
    assert va.has_reverted(-600, -250, 0.5) is True   # past target (less negative)
    assert va.has_reverted(-600, -400, 0.5) is False  # still too negative
    assert va.has_reverted(-600, -301, 0.5) is False  # just shy


def test_has_reverted_guards_non_negative_baseline():
    # A positive/zero baseline has no meaningful dump-reversion target.
    assert va.has_reverted(0, 100, 0.5) is False
    assert va.has_reverted(200, 500, 0.5) is False


def test_has_reverted_fraction_configurable():
    # fraction 0.25 → target -150 for baseline -600.
    assert va.has_reverted(-600, -150, 0.25) is True
    assert va.has_reverted(-600, -200, 0.25) is False


def test_pct_reverted():
    assert va.pct_reverted(-600, -600) == pytest.approx(0.0)
    assert va.pct_reverted(-600, -300) == pytest.approx(50.0)
    assert va.pct_reverted(-600, 0) == pytest.approx(100.0)
    assert va.pct_reverted(0, 5) is None


def test_reversion_fraction_env(monkeypatch):
    monkeypatch.setenv("VARIATIONAL_REVERSION_FRACTION", "0.3")
    assert va.reversion_fraction() == 0.3
    monkeypatch.setenv("VARIATIONAL_REVERSION_FRACTION", "0")
    assert va.reversion_fraction() == 0.5  # non-positive → fallback
    monkeypatch.setenv("VARIATIONAL_REVERSION_FRACTION", "bad")
    assert va.reversion_fraction() == 0.5


# ─── Persistence (survives restart) ──────────────────────────────────────────
@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    db = tmp_path / "variational_test.db"
    monkeypatch.setattr(va, "DB_PATH", str(db))
    return str(db)


def test_register_list_remove_clear(temp_db):
    w = va.register("PORTAL", -650.0)
    assert w.ticker == "PORTAL" and w.baseline_funding == -650.0 and not w.triggered
    va.register("MBOX", -770.0)
    watches = va.list_watches()
    assert {x.ticker for x in watches} == {"PORTAL", "MBOX"}
    # remove
    assert va.remove("PORTAL") is True
    assert va.remove("PORTAL") is False  # already gone
    assert {x.ticker for x in va.list_watches()} == {"MBOX"}
    # clear
    assert va.clear() == 1
    assert va.list_watches() == []


def test_register_persists_across_restart(temp_db):
    """A fresh connection (= process restart) must still see the watch."""
    va.register("HEI", -740.0)
    # Simulate restart: new connection is opened inside each call; just re-read.
    got = va.get_watch("hei")  # case-insensitive
    assert got is not None
    assert got.baseline_funding == -740.0
    assert got.triggered is False


def test_mark_triggered_and_reregister_rearms(temp_db):
    va.register("LAB", -757.0)
    va.mark_triggered("LAB", -300.0)
    w = va.get_watch("LAB")
    assert w.triggered is True and w.current_funding == -300.0
    # untriggered-only listing excludes it.
    assert va.list_watches(include_triggered=False) == []
    # Re-register re-arms (resets baseline + triggered flag).
    va.register("LAB", -400.0)
    w2 = va.get_watch("LAB")
    assert w2.triggered is False and w2.baseline_funding == -400.0


def test_update_current_no_trigger(temp_db):
    va.register("XCN", -349.0)
    va.update_current("XCN", -300.0)
    w = va.get_watch("XCN")
    assert w.current_funding == -300.0 and w.triggered is False


def test_format_watch_list(temp_db):
    assert "No hay watches" in va.format_watch_list([], 0.5)
    va.register("BOBA", -400.0)
    txt = va.format_watch_list(va.list_watches(), 0.5)
    assert "BOBA" in txt and "baseline" in txt


def test_format_reversion_alert(temp_db):
    w = va.register("HOME", -420.0)
    txt = va.format_reversion_alert(w, -200.0, 0.5, mark_price=0.0272)
    assert "HOME" in txt
    assert "REVERSION HIT" in txt
    assert "Apply your 5 checks" in txt


# ─── Cache + get_market (network mocked) ─────────────────────────────────────
def test_cache_and_get_market(monkeypatch):
    calls = {"n": 0}

    async def fake_http():
        calls["n"] += 1
        return {"listings": [_btc_listing()]}

    monkeypatch.setattr(var, "_http_get_stats", fake_http)
    # Reset module cache so prior tests don't interfere.
    var._cache.update({"ts": 0.0, "markets": None})

    async def scenario():
        m1 = await var.fetch_markets()
        m2 = await var.fetch_markets()  # within 60s → served from cache
        assert calls["n"] == 1
        assert [m.ticker for m in m1] == ["BTC"] == [m.ticker for m in m2]
        # get_market uses cache too.
        btc = await var.get_market("btc")
        assert btc is not None and btc.ticker == "BTC"
        assert await var.get_market("DOESNOTEXIST") is None
        assert calls["n"] == 1  # still cached

    asyncio.run(scenario())


def test_fetch_markets_error_propagates(monkeypatch):
    async def boom():
        raise var.VariationalError("network error (ConnectError)")

    monkeypatch.setattr(var, "_http_get_stats", boom)
    var._cache.update({"ts": 0.0, "markets": None})

    async def scenario():
        with pytest.raises(var.VariationalError):
            await var.fetch_markets(force=True)

    asyncio.run(scenario())
