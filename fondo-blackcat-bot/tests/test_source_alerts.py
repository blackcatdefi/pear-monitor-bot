"""R-PERFECT Phase 3 #4 — source_alerts (flap detection) tests."""
from __future__ import annotations

import sys
import time


def _import_with_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    import modules.source_alerts as sa  # noqa: WPS433
    importlib.reload(sa)
    return sa


def _matrix(rows):
    return {"rows": rows, "counts": {}, "total": len(rows), "ts_utc": int(time.time())}


def test_first_seen_no_alert(tmp_path, monkeypatch):
    sa = _import_with_tmp(tmp_path, monkeypatch)
    alerts = sa.evaluate_matrix(_matrix([
        {"name": "fred_api", "status": "LIVE", "latency_ms": 100, "reason": ""}
    ]))
    assert alerts == []


def test_recovery_emits_alert(tmp_path, monkeypatch):
    sa = _import_with_tmp(tmp_path, monkeypatch)
    sa.evaluate_matrix(_matrix([
        {"name": "fred_api", "status": "UNAVAILABLE", "latency_ms": 100, "reason": "x"}
    ]))
    alerts = sa.evaluate_matrix(_matrix([
        {"name": "fred_api", "status": "LIVE", "latency_ms": 100, "reason": ""}
    ]))
    assert any("RECOVERED" in a for a in alerts)


def test_dedup_within_window(tmp_path, monkeypatch):
    sa = _import_with_tmp(tmp_path, monkeypatch)
    sa.evaluate_matrix(_matrix([
        {"name": "fred_api", "status": "UNAVAILABLE", "latency_ms": 100, "reason": "x"}
    ]))
    sa.evaluate_matrix(_matrix([
        {"name": "fred_api", "status": "LIVE", "latency_ms": 100, "reason": ""}
    ]))
    sa.evaluate_matrix(_matrix([
        {"name": "fred_api", "status": "UNAVAILABLE", "latency_ms": 100, "reason": "y"}
    ]))
    alerts = sa.evaluate_matrix(_matrix([
        {"name": "fred_api", "status": "LIVE", "latency_ms": 100, "reason": ""}
    ]))
    # second recovery should be deduped within 24h
    assert all("RECOVERED" not in a for a in alerts)


def test_persisted_state_round_trip(tmp_path, monkeypatch):
    sa = _import_with_tmp(tmp_path, monkeypatch)
    sa.evaluate_matrix(_matrix([
        {"name": "hl_info_api", "status": "LIVE", "latency_ms": 50, "reason": ""}
    ]))
    state = sa.get_persisted_state()
    assert "hl_info_api" in state
    assert state["hl_info_api"]["status"] == "LIVE"


def test_format_alerts_empty(tmp_path, monkeypatch):
    sa = _import_with_tmp(tmp_path, monkeypatch)
    assert sa.format_alerts([]) == ""


def test_format_alerts_present(tmp_path, monkeypatch):
    sa = _import_with_tmp(tmp_path, monkeypatch)
    out = sa.format_alerts(["fake1", "fake2"])
    assert "Source flap report" in out
    assert "fake1" in out
