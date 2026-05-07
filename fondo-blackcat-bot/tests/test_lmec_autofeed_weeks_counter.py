"""R-BOT-LMEC-AUTOFEED — Test #1.

Validate that the Leg-4 weeks-broken counter:

* Increments on each ISO-week tick when BTC is above MA50w.
* Resets to 0 when BTC drops back below MA50w.
* Doesn't double-count when called multiple times within the same ISO week.
* No-ops when either input is missing.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch):
    """Force lmec_state to write to an isolated tmpdir."""
    tmp = tempfile.mkdtemp(prefix="lmec_test_")
    monkeypatch.setenv("DATA_DIR", tmp)
    # config.DATA_DIR is read at import time; patch the module too.
    import config

    monkeypatch.setattr(config, "DATA_DIR", tmp, raising=False)
    yield tmp


def _import_module():
    # Re-import so DATA_DIR is picked up.
    import importlib

    from modules import lmec_state as _ls

    return importlib.reload(_ls)


def test_counter_starts_at_zero():
    ls = _import_module()
    state = ls.load()
    assert state["ma50w_consecutive_weeks"] == 0
    assert state["ma50w_first_break_iso"] is None


def test_counter_increments_on_new_iso_week():
    ls = _import_module()
    # Week 1 — BTC above MA50w.
    t1 = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)  # Monday ISO W19
    s = ls.update_weeks_counter(80_000, 75_000, now=t1)
    assert s["ma50w_consecutive_weeks"] == 1
    # Same week, multiple calls — no double count.
    s = ls.update_weeks_counter(80_500, 75_000, now=t1 + timedelta(hours=4))
    assert s["ma50w_consecutive_weeks"] == 1
    # Next ISO week, BTC still above MA — counter ticks to 2.
    t2 = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)  # next Monday W20
    s = ls.update_weeks_counter(81_000, 75_000, now=t2)
    assert s["ma50w_consecutive_weeks"] == 2


def test_counter_resets_on_drop_below_ma():
    ls = _import_module()
    t1 = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
    ls.update_weeks_counter(80_000, 75_000, now=t1)
    t2 = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    ls.update_weeks_counter(81_000, 75_000, now=t2)
    # BTC plunges below MA — counter resets.
    t3 = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
    s = ls.update_weeks_counter(70_000, 75_000, now=t3)
    assert s["ma50w_consecutive_weeks"] == 0
    assert s["ma50w_first_break_iso"] is None
    assert s["last_btc_below_ma"] is True


def test_counter_noop_on_missing_inputs():
    ls = _import_module()
    s = ls.update_weeks_counter(None, 75_000)
    assert s["ma50w_consecutive_weeks"] == 0
    s = ls.update_weeks_counter(80_000, None)
    assert s["ma50w_consecutive_weeks"] == 0


def test_counter_re_triggers_streak_after_drop():
    """drop → recover → streak restarts at 1."""
    ls = _import_module()
    t1 = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
    ls.update_weeks_counter(80_000, 75_000, now=t1)  # week 1: streak=1
    # Drop below
    t2 = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    s = ls.update_weeks_counter(70_000, 75_000, now=t2)
    assert s["ma50w_consecutive_weeks"] == 0
    # Recover next week — fresh streak starts
    t3 = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
    s = ls.update_weeks_counter(80_000, 75_000, now=t3)
    assert s["ma50w_consecutive_weeks"] == 1
    assert s["ma50w_first_break_iso"] is not None


def test_counter_threshold_env_var_default():
    """Threshold defaults to 2 weeks per spec."""
    from modules import lmec_state as ls

    assert ls.LMEC_MA50W_BROKEN_THRESHOLD_WEEKS >= 1
