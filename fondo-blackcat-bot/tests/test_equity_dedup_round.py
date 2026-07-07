"""R-EQUITY-DEDUP-DREAMCASH (2026-07-07) — round-specific coverage.

1. Delta-block one-time baseline-correction note: env-gated + SQLite
   consumed-flag → fires EXACTLY once, never in tests (env absent).
2. Persisted-flag price-action dismissal: a stale PA flag on a held-negative
   asset auto-clears; real events keep the held-adverse guard.
3. Paginated userFillsByTime PPC fetch: pages advance by max fill time,
   dedup by tid, truncation only when max pages exhausted.
"""
from __future__ import annotations

import sqlite3

import pytest

import modules.hype_acquisition as ha
import modules.intel_memory as im
from modules.integrity_halt import IntegrityHit, get_active_flags, raise_flags
from modules.integrity_reconcile import reconcile_persisted_flags
from modules.report_delta import format_report_delta_block

PREV = {
    "ts": "2026-07-06T12:00:00+00:00",
    "total_equity": 158_800.0, "aave_hf": 1.81, "hype_px": 44.10,
    "btc_mark": 104_000.0, "pm_debt": 39_800.0, "perp_upnl": -1_200.0,
}
CURR = dict(PREV, ts="2026-07-07T12:00:00+00:00", total_equity=144_700.0)


@pytest.fixture()
def _tmp_db(tmp_path, monkeypatch):
    db = str(tmp_path / "kpis.db")

    def _conn():
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr("modules.intel_memory._get_conn", _conn)
    yield


# ─── 1. baseline-correction note ─────────────────────────────────────────────
def test_no_baseline_note_when_env_absent(_tmp_db, monkeypatch):
    monkeypatch.delenv("EQUITY_BASELINE_CORRECTION_NOTE", raising=False)
    block = format_report_delta_block(CURR, PREV)
    assert "baseline corregida" not in block
    assert "TOTAL EQUITY" in block          # normal delta still renders


def test_baseline_note_fires_exactly_once(_tmp_db, monkeypatch):
    monkeypatch.setenv("EQUITY_BASELINE_CORRECTION_NOTE", "1")
    first = format_report_delta_block(CURR, PREV)
    assert "baseline corregida" in first
    assert "doble-contaban DreamCash" in first
    # Second run: consumed-flag persisted → normal delta, no note.
    second = format_report_delta_block(CURR, PREV)
    assert "baseline corregida" not in second
    assert "TOTAL EQUITY" in second


def test_baseline_note_never_breaks_empty_block(_tmp_db, monkeypatch):
    monkeypatch.setenv("EQUITY_BASELINE_CORRECTION_NOTE", "1")
    # No previous snapshot → block stays omitted (note must not leak alone).
    assert format_report_delta_block(CURR, None) == ""


# ─── 2. persisted PA flag auto-dismisses; real events keep the guard ─────────
def _pos(coin, upnl):
    return {"coin": coin, "side": "LONG", "unrealized_pnl": upnl}


def test_stale_price_action_flag_on_held_negative_asset_clears(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(im, "DB_PATH", str(tmp_path / "intel.db"))
    # ZordXBT-style chart commentary that matched the «rug» keyword and got
    # persisted on held-negative BTC — pre-fix the held-adverse guard pinned
    # it alive forever.
    raise_flags([IntegrityHit(
        asset="BTC",
        keyword="rug",
        excerpt=(
            "$BTC price action looks bearish, struggling to hold support at "
            "the yearly low — chart shows a clear downtrend, feels like a "
            "slow rug if this level breaks"
        ),
        source="ZordXBT", shielded=False,
    )])
    assert "BTC" in {f["asset"] for f in get_active_flags()}
    dismissed = reconcile_persisted_flags([_pos("BTC", -3400.0)])
    assert ("BTC", "price_action_commentary") in dismissed
    assert "BTC" not in {f["asset"] for f in get_active_flags()}


def test_real_event_flag_on_held_negative_asset_still_protected(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(im, "DB_PATH", str(tmp_path / "intel.db"))
    raise_flags([IntegrityHit(
        asset="BTC",
        keyword="exploit",
        excerpt=(
            "Bitcoin core exploit: a consensus bug enables double-spend on "
            "BTC, chart looks bearish at support"
        ),
        source="AIXBT", shielded=False,
    )])
    dismissed = reconcile_persisted_flags([_pos("BTC", -5000.0)])
    assert all(a != "BTC" for a, _ in dismissed)
    assert "BTC" in {f["asset"] for f in get_active_flags()}


# ─── 3. paginated fills fetch ────────────────────────────────────────────────
def _fill(tid, t, sz="1", px="10"):
    return {"tid": tid, "time": t, "coin": "HYPE", "side": "B",
            "sz": sz, "px": px}


def test_fetch_fills_paginates_and_dedups(monkeypatch):
    import modules.hl_client as hc

    monkeypatch.setattr(ha, "_FILLS_CAP", 3)
    monkeypatch.setattr(ha, "_FILLS_MAX_PAGES", 5)
    calls = []

    def fake_post(payload):
        calls.append(payload)
        assert payload["type"] == "userFillsByTime"
        st = payload["startTime"]
        if st == 0:                       # page 1 — FULL (3) → paginate
            return [_fill(1, 100), _fill(2, 200), _fill(3, 300)]
        assert st == 301                  # max time + 1ms
        return [_fill(3, 300), _fill(4, 400)]   # overlap dedup + partial page

    monkeypatch.setattr(hc, "post_info_sync", fake_post)
    fills = ha._fetch_fills("0xabc")
    assert [f["tid"] for f in fills] == [1, 2, 3, 4]   # tid 3 deduped
    assert len(calls) == 2
    assert ha._LAST_FETCH_TRUNCATED is False


def test_fetch_fills_truncated_when_pages_exhausted(monkeypatch):
    import modules.hl_client as hc

    monkeypatch.setattr(ha, "_FILLS_CAP", 2)
    monkeypatch.setattr(ha, "_FILLS_MAX_PAGES", 2)

    def fake_post(payload):
        st = payload["startTime"]
        return [_fill(st + 1, st + 1), _fill(st + 2, st + 2)]  # always full

    monkeypatch.setattr(hc, "post_info_sync", fake_post)
    fills = ha._fetch_fills("0xabc")
    assert len(fills) == 4
    assert ha._LAST_FETCH_TRUNCATED is True


def test_fetch_fills_first_page_failure_falls_back_to_legacy(monkeypatch):
    import modules.hl_client as hc

    def fake_post(payload):
        if payload["type"] == "userFillsByTime":
            return {"error": "boom"}      # not a list → fallback
        assert payload["type"] == "userFills"
        return [_fill(1, 100)]

    monkeypatch.setattr(hc, "post_info_sync", fake_post)
    fills = ha._fetch_fills("0xabc")
    assert [f["tid"] for f in fills] == [1]
    assert ha._LAST_FETCH_TRUNCATED is False


def test_fetch_fills_returns_none_on_total_failure(monkeypatch):
    import modules.hl_client as hc

    def fake_post(payload):
        raise RuntimeError("network down")

    monkeypatch.setattr(hc, "post_info_sync", fake_post)
    assert ha._fetch_fills("0xabc") is None
