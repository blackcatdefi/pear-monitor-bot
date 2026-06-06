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


# ── R-INTEGRITY-FIX-P0.2: string-date normalization regression ───────────────
# The live 2026-06-05 header still read "ninguno <72h" while the body reported
# the 6-Jun HYPE unlock. Root cause: the unlock's date arrived as a STRING (the
# header only parsed numeric epochs via int(float(...)) → ValueError → dropped).
# The header must normalize date-only ISO, full ISO, "Z"-suffix, and "6-Jun"
# shorthand, and include the unlock REGARDLESS of value_usd being None.

NOW6 = datetime(2026, 6, 5, 22, 0, tzinfo=timezone.utc)  # eve of the 6-Jun unlock


def test_iso_date_only_string_is_parsed_into_window():
    unlocks = {"status": "ok", "data": [
        {"symbol": "HYPE", "date": "2026-06-06", "value_usd": None},
    ]}
    out = formatters._next_catalyst_for_header(72, unlocks, NOW6)
    assert "HYPE unlock" in out
    assert "ninguno" not in out


def test_full_iso_zulu_string_is_parsed():
    unlocks = {"status": "ok", "data": [
        {"symbol": "HYPE", "timestamp": "2026-06-06T12:00:00Z", "value_usd": None},
    ]}
    out = formatters._next_catalyst_for_header(72, unlocks, NOW6)
    assert "HYPE unlock" in out and "ninguno" not in out


def test_day_month_shorthand_is_parsed():
    unlocks = {"status": "ok", "data": [
        {"symbol": "HYPE", "date": "6 Jun", "value_usd": None},
    ]}
    out = formatters._next_catalyst_for_header(72, unlocks, NOW6)
    assert "HYPE unlock" in out and "ninguno" not in out


def test_production_shape_hype_6jun_none_value_with_size_from_price():
    # Exact production shape: symbol HYPE, string date, value_usd None — the
    # header must NAME it and show a size derived from tokens × spot price.
    unlocks = {"status": "ok", "data": [
        {"symbol": "HYPE", "date": "2026-06-06", "value_usd": None, "tokens": 534000},
    ]}
    out = formatters._next_catalyst_for_header(72, unlocks, NOW6, prices={"HYPE": 64.0})
    assert "HYPE unlock" in out
    assert "$34M" in out
    assert "ninguno" not in out


def test_production_shape_hype_6jun_known_value():
    unlocks = {"status": "ok", "data": [
        {"symbol": "HYPE", "date": "2026-06-06", "value_usd": 34_000_000},
    ]}
    out = formatters._next_catalyst_for_header(72, unlocks, NOW6)
    assert "HYPE unlock" in out and "$34M" in out and "ninguno" not in out


def test_string_date_explicit_zero_still_dropped():
    # The SUI explicit-$0 guard survives string-date parsing.
    unlocks = {"status": "ok", "data": [
        {"symbol": "SUI", "date": "2026-06-06", "value_usd": 0},
    ]}
    assert formatters._next_catalyst_for_header(72, unlocks, NOW6) == "ninguno <72h"


def test_string_date_past_is_purged():
    unlocks = {"status": "ok", "data": [
        {"symbol": "HYPE", "date": "2026-06-01", "value_usd": 5_000_000},
    ]}
    assert formatters._next_catalyst_for_header(72, unlocks, NOW6) == "ninguno <72h"


def test_header_and_body_agree_on_imminence():
    # Consistency: the header normalizer and the feed's own parser agree.
    from modules.unlocks import _parse_iso_or_epoch
    raw = "2026-06-06"
    header_epoch = formatters._normalize_unlock_epoch(raw, NOW6)
    feed_epoch = _parse_iso_or_epoch(raw)
    assert header_epoch == feed_epoch


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
