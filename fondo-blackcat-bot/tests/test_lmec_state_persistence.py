"""R-BOT-LMEC-AUTOFEED — Test #2.

Validate that ``modules.lmec_state``:

* Writes JSON state to ``$DATA_DIR/lmec_state.json``.
* Survives a "restart" (state object discarded → reload from disk).
* Round-trips all schema fields without corruption.
* Self-heals on corrupted JSON (returns empty defaults, never raises).
"""
from __future__ import annotations

import importlib
import json
import os
import tempfile
from datetime import datetime, timezone

import pytest


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="lmec_test_")
    monkeypatch.setenv("DATA_DIR", tmp)
    import config

    monkeypatch.setattr(config, "DATA_DIR", tmp, raising=False)
    yield tmp


def _fresh_module():
    from modules import lmec_state as _ls

    return importlib.reload(_ls)


def test_state_file_written_on_save(_isolated_data_dir):
    ls = _fresh_module()
    s = ls.load()
    s["ma50w_consecutive_weeks"] = 3
    s["ma50w_first_break_iso"] = "2026-05-01T00:00:00+00:00"
    ls.save(s)
    p = os.path.join(_isolated_data_dir, "lmec_state.json")
    assert os.path.isfile(p)
    with open(p, "r", encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk["ma50w_consecutive_weeks"] == 3


def test_state_survives_restart():
    """Simulate restart: write state, reload module, verify state intact."""
    ls = _fresh_module()
    t = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
    ls.update_weeks_counter(80_000, 75_000, now=t)
    # Reload module — fresh import, state must come from disk.
    ls2 = importlib.reload(ls)
    s = ls2.load()
    assert s["ma50w_consecutive_weeks"] == 1


def test_state_corrupt_json_returns_empty_defaults(_isolated_data_dir):
    ls = _fresh_module()
    p = os.path.join(_isolated_data_dir, "lmec_state.json")
    with open(p, "w", encoding="utf-8") as f:
        f.write("{not valid json {")
    s = ls.load()  # must not raise
    assert s["ma50w_consecutive_weeks"] == 0
    assert s["last_legs"] == []


def test_state_partial_dict_filled_with_defaults(_isolated_data_dir):
    """Missing keys are forward-compat: schema upgrade fills defaults."""
    ls = _fresh_module()
    p = os.path.join(_isolated_data_dir, "lmec_state.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"ma50w_consecutive_weeks": 5}, f)
    s = ls.load()
    assert s["ma50w_consecutive_weeks"] == 5
    assert "last_legs" in s
    assert s["tradermap_failure_streak"] == 0


def test_record_legs_snapshot_round_trip():
    ls = _fresh_module()
    conditions = [
        {"id": "btc_above_ath", "status": "INVALIDA", "detail": "x"},
        {"id": "macd_weekly_positive", "status": "VALIDA", "detail": "y"},
    ]
    ls.record_legs_snapshot(conditions)
    # Reload from disk
    ls2 = importlib.reload(ls)
    s = ls2.load()
    assert len(s["last_legs"]) == 2
    assert s["last_legs"][1]["status"] == "VALIDA"


def test_status_summary_keys():
    ls = _fresh_module()
    summary = ls.status_summary()
    for k in (
        "ma50w_consecutive_weeks",
        "ma50w_first_break_iso",
        "last_iso_week",
        "tradermap_failure_streak",
        "thresholds",
    ):
        assert k in summary, f"missing summary key {k}"
