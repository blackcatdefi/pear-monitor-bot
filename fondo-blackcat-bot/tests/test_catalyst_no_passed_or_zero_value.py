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


# ── R-AUDIT2-P0.2: production cache-shape regression ─────────────────────────
# The header tests above feed hand-built items. The LIVE bug was a test/prod
# divergence: modules.unlocks.fetch_unlocks (cache branch) emits value_usd as
# None when the source never computed a USD size. Earlier code coerced that to
# 0, so a real future HYPE unlock read as an explicit $0 and got dropped.

def test_unknown_value_none_is_kept_like_missing_key():
    # value_usd EXPLICITLY None (the real cache shape) must behave like a
    # MISSING key: unknown ⇒ assume material ⇒ keep.
    unlocks = {"status": "ok", "data": [
        {"symbol": "HYPE", "timestamp": _ts(36), "value_usd": None, "tokens": 534000},
    ]}
    out = formatters._next_catalyst_for_header(72, unlocks, NOW)
    assert "HYPE unlock" in out
    assert "ninguno" not in out


def test_unknown_value_renders_usd_size_from_tokens_and_price():
    # No USD from the feed, but tokens × spot price yields the size to show.
    unlocks = {"status": "ok", "data": [
        {"symbol": "HYPE", "timestamp": _ts(24), "value_usd": None, "tokens": 534000},
    ]}
    out = formatters._next_catalyst_for_header(72, unlocks, NOW, prices={"HYPE": 64.0})
    assert "HYPE unlock" in out
    assert "$34M" in out  # 534000 × 64 ≈ 34.2M → "$34M"


def test_explicit_zero_still_dropped_under_new_logic():
    unlocks = {"status": "ok", "data": [
        {"symbol": "SUI", "timestamp": _ts(0.3), "value_usd": 0, "tokens": 0},
    ]}
    assert formatters._next_catalyst_for_header(72, unlocks, NOW) == "ninguno <72h"


def test_known_value_renders_usd_size():
    unlocks = {"status": "ok", "data": [
        {"symbol": "HYPE", "timestamp": _ts(40), "value_usd": 34_000_000},
    ]}
    out = formatters._next_catalyst_for_header(72, unlocks, NOW)
    assert "HYPE unlock" in out and "$34M" in out


async def test_fetch_unlocks_cache_preserves_unknown_value_usd(monkeypatch):
    """fetch_unlocks() cache branch must pass value_usd=None through, never 0.

    Direct guard on the test/prod divergence: get_cached_unlocks returns a row
    whose value_usd column is NULL (None); fetch_unlocks must not coerce it.
    """
    from modules import unlocks as unlocks_mod

    fake_rows = [{
        "token": "HYPE", "next_unlock_ts": _ts(36),
        "amount_tokens": 534000, "value_usd": None,
        "pct_supply": None, "category": "cliff", "source": "dropstab",
    }]
    monkeypatch.setattr(
        unlocks_mod.intel_memory, "get_cached_unlocks",
        lambda window_days=14, max_age_hours=6: fake_rows,
    )
    res = await unlocks_mod.fetch_unlocks()
    assert res["status"] == "ok"
    hype = [d for d in res["data"] if d["symbol"] == "HYPE"][0]
    assert hype["value_usd"] is None  # NOT coerced to 0
