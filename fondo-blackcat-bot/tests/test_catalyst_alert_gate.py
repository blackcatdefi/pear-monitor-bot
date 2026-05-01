"""R-SILENT — tests for ``auto.catalyst_alert_gate``.

Cover the impact_level + timing allow-list + post-event window:

  * Default config: ALLOW_IMPACTS={critical}, ALLOW_TIMINGS={t30,t_post}.
  * T-24h on critical → suppressed (timing not allowed).
  * T-30min on medium  → suppressed (impact not allowed).
  * T-30min on critical → fires.
  * T+15min before window → no fire; inside window → fires; after window
    → no fire.
  * mark_post_sent dedupes subsequent post checks.
  * Kill switch returns True for pre and False for post (legacy passthrough
    only allows pre-alerts; post-alerts are an R-SILENT-only feature).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from auto import catalyst_alert_gate as cgate


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    db = tmp_path / "catalyst_alerts.db"
    monkeypatch.setattr(cgate, "_db_path", lambda: str(db))
    monkeypatch.setattr(cgate, "ENABLED", True)
    monkeypatch.setattr(cgate, "ALLOW_IMPACTS", {"critical"})
    monkeypatch.setattr(cgate, "ALLOW_TIMINGS", {"t30", "t_post"})
    monkeypatch.setattr(cgate, "POSTEVENT_DELAY_MIN", 15)
    monkeypatch.setattr(cgate, "POSTEVENT_WINDOW_MIN", 10)
    yield


def _ev(impact="critical", ts=None, eid="evt-1"):
    return {
        "event_id": eid,
        "name": "FOMC press",
        "impact_level": impact,
        "timestamp_utc": (ts or datetime.now(timezone.utc)).isoformat(),
    }


def test_pre_t24h_suppressed_even_for_critical():
    assert cgate.should_fire_pre(_ev(), "T-24h") is False


def test_pre_t2h_suppressed_even_for_critical():
    assert cgate.should_fire_pre(_ev(), "T-2h") is False


def test_pre_t30min_critical_fires():
    assert cgate.should_fire_pre(_ev(), "T-30min") is True


def test_pre_t30min_medium_suppressed():
    assert cgate.should_fire_pre(_ev(impact="medium"), "T-30min") is False


def test_pre_t30min_high_suppressed_by_default():
    assert cgate.should_fire_pre(_ev(impact="high"), "T-30min") is False


def test_pre_kill_switch_passthrough(monkeypatch):
    monkeypatch.setattr(cgate, "ENABLED", False)
    # All pre-alerts pass through with gate disabled
    assert cgate.should_fire_pre(_ev(impact="medium"), "T-24h") is True
    assert cgate.should_fire_pre(_ev(impact="medium"), "T-2h") is True


def test_post_event_before_window_no_fire():
    ev_dt = datetime.now(timezone.utc)
    ev = _ev(ts=ev_dt)
    # 5 min after event, window starts at 15 → no fire
    now = ev_dt + timedelta(minutes=5)
    assert cgate.should_fire_post(ev, now=now) is False


def test_post_event_inside_window_fires():
    ev_dt = datetime.now(timezone.utc)
    ev = _ev(ts=ev_dt)
    # 16 min after event → inside [15, 25) → fire
    now = ev_dt + timedelta(minutes=16)
    assert cgate.should_fire_post(ev, now=now) is True


def test_post_event_after_window_no_fire():
    ev_dt = datetime.now(timezone.utc)
    ev = _ev(ts=ev_dt)
    # 30 min after event → past [15, 25) → no fire
    now = ev_dt + timedelta(minutes=30)
    assert cgate.should_fire_post(ev, now=now) is False


def test_post_event_dedup_after_mark():
    ev_dt = datetime.now(timezone.utc)
    ev = _ev(ts=ev_dt)
    now = ev_dt + timedelta(minutes=18)
    assert cgate.should_fire_post(ev, now=now) is True
    cgate.mark_post_sent(ev)
    assert cgate.was_post_sent(ev) is True
    assert cgate.should_fire_post(ev, now=now) is False


def test_post_event_medium_impact_suppressed():
    ev_dt = datetime.now(timezone.utc)
    ev = _ev(impact="medium", ts=ev_dt)
    now = ev_dt + timedelta(minutes=18)
    assert cgate.should_fire_post(ev, now=now) is False


def test_post_event_kill_switch_returns_false(monkeypatch):
    """When gate disabled, post alerts NEVER fire (legacy behaviour)."""
    monkeypatch.setattr(cgate, "ENABLED", False)
    ev_dt = datetime.now(timezone.utc)
    ev = _ev(ts=ev_dt)
    now = ev_dt + timedelta(minutes=18)
    assert cgate.should_fire_post(ev, now=now) is False


def test_post_event_t_post_timing_disabled(monkeypatch):
    """If t_post not in ALLOW_TIMINGS, post alerts are suppressed."""
    monkeypatch.setattr(cgate, "ALLOW_TIMINGS", {"t30"})
    ev_dt = datetime.now(timezone.utc)
    ev = _ev(ts=ev_dt)
    now = ev_dt + timedelta(minutes=18)
    assert cgate.should_fire_post(ev, now=now) is False


def test_post_alert_id_format():
    ev = _ev(eid="abc-123")
    assert cgate.post_alert_id(ev) == "post:abc-123"


def test_status_summary_shape():
    s = cgate.status_summary()
    assert s["enabled"] is True
    assert "critical" in s["allow_impacts"]
    assert "t30" in s["allow_timings"]
    assert s["postevent_delay_min"] == 15
    assert isinstance(s["recent_post_alerts"], list)


def test_pre_event_ignores_legacy_impact_field():
    """Some calendars use 'impact' instead of 'impact_level'. Both must work."""
    ev = {
        "event_id": "x",
        "name": "FOMC",
        "impact": "critical",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }
    assert cgate.should_fire_pre(ev, "T-30min") is True
