"""R-PERFECT Phase 3 #3 — cost_tracker tests."""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path


def _import_with_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    import modules.cost_tracker as ct  # noqa: WPS433
    importlib.reload(ct)
    return ct


def test_estimate_cost_known_model(tmp_path, monkeypatch):
    ct = _import_with_tmp(tmp_path, monkeypatch)
    cost = ct.estimate_cost_usd("claude-opus-4-6", 1_000_000, 1_000_000)
    assert cost == 15.0 + 75.0


def test_estimate_cost_unknown_falls_back(tmp_path, monkeypatch):
    ct = _import_with_tmp(tmp_path, monkeypatch)
    cost = ct.estimate_cost_usd("does-not-exist", 1_000_000, 1_000_000)
    assert cost == 3.0 + 15.0


def test_log_llm_call_persists(tmp_path, monkeypatch):
    ct = _import_with_tmp(tmp_path, monkeypatch)
    cost = ct.log_llm_call("claude-haiku-4-5", 1000, 500, source="unit-test")
    assert cost > 0
    with sqlite3.connect(str(tmp_path / "cost.db")) as conn:
        rows = list(conn.execute("SELECT model, tokens_in, tokens_out FROM llm_calls"))
    assert rows == [("claude-haiku-4-5", 1000, 500)]


def test_format_cost_report_no_calls(tmp_path, monkeypatch):
    ct = _import_with_tmp(tmp_path, monkeypatch)
    out = ct.format_cost_report(days=7)
    assert "sin llamadas registradas" in out


def test_format_cost_report_with_calls(tmp_path, monkeypatch):
    ct = _import_with_tmp(tmp_path, monkeypatch)
    ct.log_llm_call("claude-sonnet-4-6", 2000, 1000)
    ct.log_llm_call("claude-sonnet-4-6", 1000, 500)
    out = ct.format_cost_report(days=7)
    assert "2 llamadas" in out or "calls" in out
    assert "claude-sonnet-4-6" in out


def test_threshold_alert_fires(tmp_path, monkeypatch):
    monkeypatch.setenv("COST_DAILY_ALERT_USD", "0.0001")
    ct = _import_with_tmp(tmp_path, monkeypatch)
    ct.log_llm_call("claude-opus-4-6", 1000, 1000)
    msg = ct.check_alert_thresholds()
    assert msg is not None
    assert "Cost 24h" in msg


def test_threshold_alert_silent_when_below(tmp_path, monkeypatch):
    monkeypatch.setenv("COST_DAILY_ALERT_USD", "100.0")
    monkeypatch.setenv("COST_MONTHLY_ALERT_USD", "1000.0")
    ct = _import_with_tmp(tmp_path, monkeypatch)
    ct.log_llm_call("claude-haiku-4-5", 100, 100)
    assert ct.check_alert_thresholds() is None


def test_pricing_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("PRICING_USD_PER_MTOK", '{"foo-model": [10.0, 20.0]}')
    ct = _import_with_tmp(tmp_path, monkeypatch)
    cost = ct.estimate_cost_usd("foo-model", 1_000_000, 1_000_000)
    assert cost == 30.0
