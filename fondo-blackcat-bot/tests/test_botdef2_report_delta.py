"""R-BOT-DEFINITIVE-2 T6 — DELTA BLOCK vs previous /reporte.

Mission spec coverage: with/without previous snapshot + delta math.
"""
from __future__ import annotations

import sqlite3

import pytest

from modules.report_delta import (
    _parse_usd,
    collect_report_kpis,
    format_report_delta_block,
    load_last_kpis,
    save_report_kpis,
)


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    db = str(tmp_path / "kpis.db")

    def _conn():
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr("modules.intel_memory._get_conn", _conn)
    yield


PREV = {
    "ts": "2026-07-01T12:00:00+00:00",
    "total_equity": 106_000.0, "aave_hf": 1.81, "hype_px": 44.10,
    "btc_mark": 104_000.0, "pm_debt": 39_800.0, "perp_upnl": -1_200.0,
}
CURR = {
    "ts": "2026-07-02T12:00:00+00:00",
    "total_equity": 107_200.0, "aave_hf": 1.76, "hype_px": 45.30,
    "btc_mark": 103_000.0, "pm_debt": 39_800.0, "perp_upnl": -900.0,
}


# ─── No previous snapshot → omit silently ────────────────────────────────────
def test_no_previous_snapshot_omits_block():
    assert format_report_delta_block(CURR, None) == ""
    assert format_report_delta_block(CURR, {}) == ""
    assert load_last_kpis() is None  # empty DB


# ─── With previous snapshot: arrows + absolute + % ───────────────────────────
def test_delta_math_and_arrows():
    block = format_report_delta_block(CURR, PREV)
    assert "DELTA vs REPORTE ANTERIOR" in block
    # equity up +$1.2K = +1.1%
    assert "💰 TOTAL EQUITY: $106.0K → $107.2K  ▲ +1,200 (+1.1%)" in block
    # HF down, no % (not meaningful for a ratio)
    assert "⚖️ aave-HF: 1.81 → 1.76  ▼ -0.05" in block
    # HYPE px up +2.7%
    assert "💠 HYPE oracle: $44.10 → $45.30  ▲ +1.20 (+2.7%)" in block
    # BTC down
    assert "₿ BTC mark: $104,000.00 → $103,000.00  ▼ -1,000.00 (-1.0%)" in block
    # unchanged debt
    assert "🏦 PM deuda: $39.8K → $39.8K  ＝ +0" in block
    # UPnL improved
    assert "📈 Σ PERP UPnL: -$1.2K → -$900.00  ▲ +300" in block


def test_missing_kpis_skip_lines_not_block():
    prev = dict(PREV, btc_mark=None)
    curr = dict(CURR, hype_px=None)
    block = format_report_delta_block(curr, prev)
    assert "BTC mark" not in block and "HYPE oracle" not in block
    assert "TOTAL EQUITY" in block


# ─── Persistence round-trip ──────────────────────────────────────────────────
def test_save_and_load_roundtrip():
    assert save_report_kpis(PREV) is True
    got = load_last_kpis()
    assert got is not None
    assert got["total_equity"] == pytest.approx(106_000.0)
    assert got["aave_hf"] == pytest.approx(1.81)
    assert got["ts"] == PREV["ts"]
    # A second save becomes the new "previous".
    save_report_kpis(CURR)
    assert load_last_kpis()["total_equity"] == pytest.approx(107_200.0)


# ─── Collection: header equity parse + graceful degradation ──────────────────
def test_parse_usd_shapes():
    assert _parse_usd("$106.2K") == pytest.approx(106_200.0)
    assert _parse_usd("-$1.05M") == pytest.approx(-1_050_000.0)
    assert _parse_usd("$923.00") == pytest.approx(923.0)
    assert _parse_usd("—") is None


def test_collect_reads_equity_from_header_and_never_raises():
    header = "⚡ DESTACADO\n💰 TOTAL EQUITY: $106.2K\n⚖️ PM SALUD: 🟢"
    kpis = collect_report_kpis([], {}, header)
    assert kpis["total_equity"] == pytest.approx(106_200.0)
    assert "ts" in kpis
    # Garbage inputs degrade to None KPIs, never an exception.
    kpis2 = collect_report_kpis(None, None, None)
    assert kpis2["total_equity"] is None
