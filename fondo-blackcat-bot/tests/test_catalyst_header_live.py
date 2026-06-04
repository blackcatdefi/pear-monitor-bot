"""R-CATALYST-LIVE — regression tests for the NEXT CATALYST <72h header.

Guards the 2026-06-04 fix in ``templates.formatters._next_catalyst_for_header``:

  * The header must surface REAL dated catalysts within 72h, derived from
    the SAME live sources the report body uses — at minimum the live
    token-unlock feed (``modules.unlocks.fetch_unlocks`` shape) plus the
    macro calendar.
  * It must print "ninguno <72h" ONLY when no wired source has a dated
    event inside the window (the genuinely-empty case).
  * An expired/past-dated entry (e.g. an old April roadmap row) must NEVER
    be surfaced — date math is anchored to the report's run timestamp (UTC).

Before the fix, the line read the stale hardcoded SQLite roadmap whose
seeded events had all expired, so it printed "ninguno <72h" even on days
when the body clearly discussed an upcoming HYPE unlock.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from templates import formatters as fmt

# Deterministic run timestamp so the window math never depends on wall clock.
RUN_NOW = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)


def _unlock_feed(symbol: str, dt: datetime) -> dict:
    """fetch_unlocks()-shaped payload with one dated unlock (live API shape)."""
    return {
        "status": "ok",
        "source": "defillama",
        "data": [{"symbol": symbol, "timestamp": int(dt.timestamp())}],
    }


def _no_calendar(monkeypatch) -> None:
    """Isolate the unlock path: pretend the macro calendar has no rows.

    (Its SQL already purges ts < now, so an all-expired roadmap returns []).
    """
    monkeypatch.setattr(
        "modules.macro_calendar.upcoming_events", lambda limit=30: []
    )


# ── (i) a dated event inside 72h must be shown, never "ninguno" ─────────────
def test_unlock_inside_72h_is_shown_not_ninguno(monkeypatch):
    _no_calendar(monkeypatch)
    hype_dt = RUN_NOW + timedelta(days=2)  # 6 Jun 12:00 — well inside 72h
    out = fmt._next_catalyst_for_header(
        window_hours=72, unlocks=_unlock_feed("HYPE", hype_dt), now=RUN_NOW
    )
    assert "ninguno" not in out
    assert "HYPE unlock" in out
    assert "6 Jun" in out
    assert "en 2d" in out


def test_passthrough_via_format_report_header(monkeypatch):
    """format_report_header must thread ``unlocks`` into the catalyst line."""
    _no_calendar(monkeypatch)
    # Anchor to real now so this is window-safe whenever the suite runs.
    now = datetime.now(timezone.utc)
    hype_dt = now + timedelta(hours=36)
    header = fmt.format_report_header(
        [], [], {"status": "error"}, _unlock_feed("HYPE", hype_dt)
    )
    assert "🗓 NEXT CATALYST <72h:" in header
    cat_line = [ln for ln in header.splitlines() if "NEXT CATALYST" in ln][0]
    assert "HYPE unlock" in cat_line
    assert "ninguno" not in cat_line


# ── (ii) genuinely empty → "ninguno <72h" stays correct ─────────────────────
def test_true_empty_returns_ninguno(monkeypatch):
    _no_calendar(monkeypatch)
    out = fmt._next_catalyst_for_header(
        window_hours=72, unlocks={"status": "ok", "data": []}, now=RUN_NOW
    )
    assert out == "ninguno <72h"
    # No unlock payload at all behaves identically.
    out2 = fmt._next_catalyst_for_header(window_hours=72, unlocks=None, now=RUN_NOW)
    assert out2 == "ninguno <72h"


# ── (iii) an expired April-dated entry is NEVER surfaced ────────────────────
def test_expired_april_entry_never_surfaced(monkeypatch):
    april = datetime(2026, 4, 30, 18, 30, tzinfo=timezone.utc)  # 5 weeks past
    stale_ev = SimpleNamespace(
        name="Powell FOMC (April roadmap)",
        timestamp_utc=april,
        impact_level="critical",
    )
    monkeypatch.setattr(
        "modules.macro_calendar.upcoming_events", lambda limit=30: [stale_ev]
    )
    out = fmt._next_catalyst_for_header(
        window_hours=72, unlocks=_unlock_feed("OLDCOIN", april), now=RUN_NOW
    )
    assert out == "ninguno <72h"
    assert "Apr" not in out
    assert "Powell" not in out


def test_expired_calendar_ignored_but_live_unlock_shown(monkeypatch):
    """Stale April calendar row coexists with a real in-window unlock:
    surface the unlock, drop the expired row."""
    april = datetime(2026, 4, 30, 18, 30, tzinfo=timezone.utc)
    stale_ev = SimpleNamespace(
        name="Old April Roadmap", timestamp_utc=april, impact_level="critical"
    )
    monkeypatch.setattr(
        "modules.macro_calendar.upcoming_events", lambda limit=30: [stale_ev]
    )
    hype_dt = RUN_NOW + timedelta(days=2)
    out = fmt._next_catalyst_for_header(
        window_hours=72, unlocks=_unlock_feed("HYPE", hype_dt), now=RUN_NOW
    )
    assert "HYPE unlock" in out
    assert "April" not in out and "Apr" not in out


# ── extras: ordering, window boundary, dedup, cache-shape keys ──────────────
def test_lists_nearest_up_to_three_sorted(monkeypatch):
    _no_calendar(monkeypatch)
    feed = {
        "status": "ok",
        "data": [
            {"symbol": "AAA", "timestamp": int((RUN_NOW + timedelta(hours=60)).timestamp())},
            {"symbol": "BBB", "timestamp": int((RUN_NOW + timedelta(hours=10)).timestamp())},
            {"symbol": "CCC", "timestamp": int((RUN_NOW + timedelta(hours=30)).timestamp())},
            {"symbol": "DDD", "timestamp": int((RUN_NOW + timedelta(hours=100)).timestamp())},
        ],
    }
    out = fmt._next_catalyst_for_header(window_hours=72, unlocks=feed, now=RUN_NOW)
    assert "DDD" not in out  # outside the 72h window
    # nearest first: BBB (10h) → CCC (30h) → AAA (60h)
    assert out.index("BBB") < out.index("CCC") < out.index("AAA")


def test_window_boundary_inclusive_and_exclusive(monkeypatch):
    _no_calendar(monkeypatch)
    at_72 = RUN_NOW + timedelta(hours=72)
    over_72 = RUN_NOW + timedelta(hours=72, minutes=1)
    inside = fmt._next_catalyst_for_header(
        window_hours=72, unlocks=_unlock_feed("ABC", at_72), now=RUN_NOW
    )
    outside = fmt._next_catalyst_for_header(
        window_hours=72, unlocks=_unlock_feed("ABC", over_72), now=RUN_NOW
    )
    assert "ABC unlock" in inside
    assert outside == "ninguno <72h"


def test_same_token_same_day_deduped(monkeypatch):
    """An identical unlock present in both calendar and feed collapses to one."""
    hype_dt = RUN_NOW + timedelta(days=2)
    cal_ev = SimpleNamespace(
        name="HYPE unlock", timestamp_utc=hype_dt, impact_level="high"
    )
    monkeypatch.setattr(
        "modules.macro_calendar.upcoming_events", lambda limit=30: [cal_ev]
    )
    out = fmt._next_catalyst_for_header(
        window_hours=72, unlocks=_unlock_feed("HYPE", hype_dt), now=RUN_NOW
    )
    assert out.count("HYPE unlock") == 1


def test_unlock_cache_shape_keys_supported(monkeypatch):
    """The SQLite-cache branch emits token/next_unlock_ts keys — accept them."""
    _no_calendar(monkeypatch)
    dt = RUN_NOW + timedelta(days=1)
    feed = {
        "status": "ok",
        "source": "cache",
        "data": [{"token": "HYPE", "next_unlock_ts": int(dt.timestamp())}],
    }
    out = fmt._next_catalyst_for_header(window_hours=72, unlocks=feed, now=RUN_NOW)
    assert "HYPE unlock" in out


def test_unavailable_unlock_feed_degrades_to_ninguno(monkeypatch):
    """A failed/unavailable unlock feed must not crash and must not invent."""
    _no_calendar(monkeypatch)
    out = fmt._next_catalyst_for_header(
        window_hours=72,
        unlocks={"status": "unavailable", "error": "all sources failed"},
        now=RUN_NOW,
    )
    assert out == "ninguno <72h"
