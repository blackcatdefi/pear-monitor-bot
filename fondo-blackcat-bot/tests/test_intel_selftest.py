"""R-PERFECT Phase 3 #9 — intel_selftest classifier + matrix tests."""
from __future__ import annotations

import sys


def _import_with_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    import modules.intel_selftest as its  # noqa: WPS433
    importlib.reload(its)
    return its


def test_classify_live(tmp_path, monkeypatch):
    its = _import_with_tmp(tmp_path, monkeypatch)
    out = its._classify("foo", {"series": [{"valor": 1}]}, 100)
    assert out["status"] == "LIVE"


def test_classify_graceful_no_key(tmp_path, monkeypatch):
    its = _import_with_tmp(tmp_path, monkeypatch)
    out = its._classify("foo", {"_status": "GRACEFUL_NO_KEY"}, 50)
    assert out["status"] == "GRACEFUL_NO_KEY"


def test_classify_degraded_spa(tmp_path, monkeypatch):
    its = _import_with_tmp(tmp_path, monkeypatch)
    out = its._classify("foo", {"_global_error": "spa_only_no_data"}, 50)
    assert out["status"] == "DEGRADED"


def test_classify_unavailable(tmp_path, monkeypatch):
    its = _import_with_tmp(tmp_path, monkeypatch)
    out = its._classify("foo", {"_global_error": "connection refused"}, 5000)
    assert out["status"] == "UNAVAILABLE"


def test_classify_empty_when_no_rows(tmp_path, monkeypatch):
    its = _import_with_tmp(tmp_path, monkeypatch)
    out = its._classify("foo", {"series": []}, 10)
    assert out["status"] == "EMPTY"


def test_format_matrix_renders(tmp_path, monkeypatch):
    its = _import_with_tmp(tmp_path, monkeypatch)
    matrix = {
        "ts_utc": 0,
        "rows": [{"name": "fred_api", "status": "LIVE", "latency_ms": 100, "reason": "ok"}],
        "counts": {"LIVE": 1},
        "total": 1,
    }
    out = its.format_matrix(matrix)
    assert "Selftest" in out
    assert "fred_api" in out
    assert "LIVE" in out


def test_format_source_status_no_snapshot(tmp_path, monkeypatch):
    its = _import_with_tmp(tmp_path, monkeypatch)
    out = its.format_source_status()
    assert "no selftest snapshot" in out
