"""P0.2 — NEXT CATALYST header never shows a passed or $0/incomplete unlock.

Regression for the "SUI unlock 2 Jun (en 17m)" bug: SUI is a PRIORITY token
tracked by the live feed even at $0 value; its near-future emission tick
surfaced an already-passed, valueless unlock as an upcoming catalyst. The
header must (a) purge any past-dated event and (b) drop $0 / missing-value
items so only material, future cliffs qualify.
"""
from __future__ import annotations

from datetime import datetime, timezone

from templates import formatters

NOW = datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc)


def _ts(hours_from_now: float) -> int:
    return int(NOW.timestamp() + hours_from_now * 3600)


def test_zero_value_unlock_excluded_even_if_future():
    unlocks = {"status": "ok", "data": [
        {"symbol": "SUI", "timestamp": _ts(0.28), "value_usd": 0},  # ~17m, $0
    ]}
    out = formatters._next_catalyst_for_header(72, unlocks, NOW)
    assert out == "ninguno <72h"
    assert "SUI" not in out


def test_missing_value_unlock_kept_but_past_purge_still_applies():
    # A MISSING value field is "unknown" — kept if future (can't prove $0),
    # but the past-purge must still drop it when the tick is already passed.
    future = {"status": "ok", "data": [{"symbol": "ABC", "timestamp": _ts(5)}]}
    assert "ABC unlock" in formatters._next_catalyst_for_header(72, future, NOW)
    past = {"status": "ok", "data": [{"symbol": "ABC", "timestamp": _ts(-5)}]}
    assert formatters._next_catalyst_for_header(72, past, NOW) == "ninguno <72h"


def test_past_dated_unlock_never_shown():
    unlocks = {"status": "ok", "data": [
        {"symbol": "SUI", "timestamp": _ts(-48), "value_usd": 9_000_000},  # 2 days ago
    ]}
    out = formatters._next_catalyst_for_header(72, unlocks, NOW)
    assert "SUI" not in out
    assert out == "ninguno <72h"


def test_material_future_unlock_is_shown():
    unlocks = {"status": "ok", "data": [
        {"symbol": "HYPE", "timestamp": _ts(36), "value_usd": 5_000_000},
    ]}
    out = formatters._next_catalyst_for_header(72, unlocks, NOW)
    assert "HYPE unlock" in out
    assert "🔓" in out


def test_zero_value_dropped_but_material_one_survives():
    unlocks = {"status": "ok", "data": [
        {"symbol": "SUI", "timestamp": _ts(0.28), "value_usd": 0},
        {"symbol": "HYPE", "timestamp": _ts(40), "value_usd": 5_000_000},
    ]}
    out = formatters._next_catalyst_for_header(72, unlocks, NOW)
    assert "HYPE" in out and "SUI" not in out
