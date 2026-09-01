"""R-COST-V2 + R-COST-V2-FIX (2026-07-23) — acceptance fixtures.

Locks in the contract points of the X cost optimization:

1. INCREMENTAL: fetch_x_intel passes the persisted since_id as a CLIENT-SIDE
   pagination boundary (R-XSTORE-FIX: the list endpoint 400-rejects a
   since_id query param) and never re-ingests a stored id (INSERT OR IGNORE).
2. PRUNE: tweets older than 72h are removed from the local store.
3. NO LIMIT (R-COST-V2-FIX, owner override): the fetch proceeds normally at
   ANY consumption level — no budget guard, no warning push, no cache
   fallback tied to usage. usage_state() is pure visibility for /costs.

Plus the static guarantee: no scheduler/cron path in bot.py references the
X API — the only call sites are /reporte and /xrefresh.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _mk_tweet(tid: str, hours_ago: float = 1.0, username: str = "docxbt"):
    now = datetime.now(timezone.utc)
    return {
        "id": tid,
        "username": username,
        "name": "DonAlt",
        "verified": True,
        "text": f"tweet {tid}",
        "created_at": _iso(now - timedelta(hours=hours_ago)),
        "metrics": {"like_count": 1, "retweet_count": 0,
                    "reply_count": 0, "quote_count": 0},
        "url": f"https://x.com/{username}/status/{tid}",
        "source": "x_list",
    }


@pytest.fixture()
def fresh_store(tmp_path, monkeypatch):
    """Isolated SQLite store per test."""
    from modules import x_store
    db = tmp_path / "intel_memory.db"
    monkeypatch.setattr(x_store, "DB_PATH", str(db))
    # intel_memory shares the same DB file in prod; point it too so
    # record_x_api_call rows land where x_store reads budget from.
    from modules import intel_memory
    monkeypatch.setattr(intel_memory, "DB_PATH", str(db), raising=False)
    return x_store


# ─── 1. since_id honored + no duplicate ids ────────────────────────────────

def test_upsert_is_idempotent_and_since_id_never_regresses(fresh_store):
    xs = fresh_store
    n1 = xs.upsert_tweets([_mk_tweet("100"), _mk_tweet("101")])
    assert n1 == 2
    # Re-ingesting the same ids stores nothing new.
    n2 = xs.upsert_tweets([_mk_tweet("100"), _mk_tweet("101"), _mk_tweet("102")])
    assert n2 == 1
    assert xs.store_stats()["total_tweets"] == 3
    xs.set_since_id("102")
    xs.set_since_id("101")  # attempt to move backwards
    assert xs.get_since_id() == "102"


@pytest.mark.asyncio
async def test_fetch_x_intel_passes_since_id_and_fetches_only_delta(fresh_store, monkeypatch):
    from modules import x_intel as _xi
    xs = fresh_store
    monkeypatch.setattr(_xi, "X_LIVE_ENABLED", True)
    monkeypatch.setattr(_xi, "X_EXTRA_HANDLES", [])
    monkeypatch.setattr(_xi, "save_x_timeline_payload", lambda *a, **k: None)

    # Seed the store as if a prior /reporte ran.
    xs.upsert_tweets([_mk_tweet("200", hours_ago=3), _mk_tweet("201", hours_ago=2)])
    xs.set_since_id("201")

    seen_params = {}

    async def _fake_list(hours, max_tweets=1200, caller="", bypass_cooldown=False, since_id=None):
        seen_params["since_id"] = since_id
        return [_mk_tweet("202", hours_ago=0.5)], None

    monkeypatch.setattr(_xi, "fetch_timeline_via_list", _fake_list)

    payload = await _xi.fetch_x_intel(hours=48, caller="test", app=None)

    assert seen_params["since_id"] == "201"      # incremental fetch requested
    assert payload["status"] == "ok"
    assert payload["fetched_new"] == 1           # only the delta was fetched
    assert payload["total"] == 3                 # full window rendered from store
    assert payload["from_store"] is True
    assert xs.get_since_id() == "202"


# ─── 2. 72h prune ───────────────────────────────────────────────────────────

def test_prune_removes_only_older_than_retention(fresh_store):
    xs = fresh_store
    xs.upsert_tweets([
        _mk_tweet("300", hours_ago=1),
        _mk_tweet("301", hours_ago=47),
        _mk_tweet("302", hours_ago=71),
        _mk_tweet("303", hours_ago=73),   # beyond 72h retention
        _mk_tweet("304", hours_ago=200),
    ])
    removed = xs.prune_old(hours=72)
    assert removed == 2
    stats = xs.store_stats()
    assert stats["total_tweets"] == 3
    # 48h window excludes the 71h tweet but keeps it stored (retention buffer).
    assert len(xs.get_window(48)) == 2


# ─── 3. NO LIMIT (R-COST-V2-FIX) ────────────────────────────────────────────

def _record_posts(n: int, db_module):
    """Write API-call rows so posts_fetched_month() sees n posts MTD."""
    from modules import intel_memory
    intel_memory.record_x_api_call("lists/tweets", 200, pages=1,
                                   tweets_returned=n, caller="test")


@pytest.mark.asyncio
async def test_fetch_proceeds_at_any_consumption_level(fresh_store, monkeypatch):
    """OWNER OVERRIDE: even with absurd MTD consumption the live fetch runs,
    the payload has NO budget/exhausted semantics, and the report is complete."""
    from modules import x_intel as _xi
    xs = fresh_store
    monkeypatch.setattr(_xi, "X_LIVE_ENABLED", True)
    monkeypatch.setattr(_xi, "X_EXTRA_HANDLES", [])
    monkeypatch.setattr(_xi, "save_x_timeline_payload", lambda *a, **k: None)
    _record_posts(999_999, xs)  # would have been 125x the old 8000 budget

    called = {"live": False}

    async def _fake_list(hours, max_tweets=1200, caller="", bypass_cooldown=False, since_id=None):
        called["live"] = True
        return [_mk_tweet("500", hours_ago=0.5)], None

    monkeypatch.setattr(_xi, "fetch_timeline_via_list", _fake_list)

    payload = await _xi.fetch_x_intel(hours=48, caller="test", app=None)
    assert called["live"] is True                  # API WAS called
    assert payload["status"] == "ok"
    assert payload["fetched_new"] == 1
    assert "budget_exhausted" not in payload       # no limit semantics
    assert payload.get("cache_reason") != "budget_exhausted"
    assert payload["usage"]["used"] >= 999_999     # visibility intact


def test_usage_state_is_pure_visibility(fresh_store):
    """usage_state() exposes only informational keys — no pct/exhausted/
    override/budget keys that could carry limit semantics."""
    xs = fresh_store
    _record_posts(300, xs)
    u = xs.usage_state()
    assert set(u.keys()) == {"used", "today", "projected_month_posts",
                             "projected_month_cost_usd", "mtd_cost_usd"}
    assert u["used"] == 300
    assert u["mtd_cost_usd"] == pytest.approx(300 * xs.COST_PER_POST_USD)


def test_no_budget_guard_symbols_in_prod_code():
    """Static proof: no code path can block/degrade an X fetch by consumption."""
    banned = ("budget_state", "should_send_budget_warning", "budget_exhausted",
              "MONTHLY_POST_BUDGET", "BUDGET_OVERRIDE", "budget_banner_for_report",
              "_send_budget_warning")
    for rel in ("bot.py", "modules/x_intel.py", "modules/x_store.py"):
        src = (REPO / rel).read_text(encoding="utf-8")
        for sym in banned:
            assert sym not in src, f"{sym} still present in {rel}"


# ─── Static: no scheduled path touches the X API ────────────────────────────

def test_no_scheduler_or_startup_hook_calls_x_api():
    src = (REPO / "bot.py").read_text(encoding="utf-8")
    # The dead job + its registration must be gone.
    assert "_x_timeline_cache_job" not in src.replace(
        "# R-COST-V2: _x_timeline_cache_job REMOVED", "")
    assert "poll_and_cache_timeline" not in src
    assert "X_SCHEDULER_ENABLED" not in src
    # The ONLY fetch_x_intel call sites are /reporte and /xrefresh.
    callers = re.findall(r'fetch_x_intel\(hours=48, caller="(\w+)"', src)
    assert sorted(callers) == ["reporte", "xrefresh"]


def test_x_intel_module_has_no_scheduled_entrypoints():
    src = (REPO / "modules" / "x_intel.py").read_text(encoding="utf-8")
    assert "poll_and_cache_timeline" not in src
    assert "def _within_cooldown" not in src


# ─── R-XSTORE-FIX: unsupported params NEVER sent to the list endpoint ───────
# Live probe 2026-07-27: /2/lists/{id}/tweets 400-rejects `since_id` and
# `exclude` ("not one of [id,max_results,pagination_token,post.fields]").
# Sending them froze the store after the Jul-23 backfill. since_id is now a
# CLIENT-SIDE boundary; RT/reply filtering is client-side only.

@pytest.mark.asyncio
async def test_since_id_and_exclude_never_sent_as_params(fresh_store, monkeypatch):
    from modules import x_intel as _xi
    monkeypatch.setattr(_xi, "X_LIVE_ENABLED", True)
    monkeypatch.setattr(_xi, "X_EXCLUDE_RT_REPLIES", True)
    monkeypatch.setattr(_xi, "X_API_BEARER_TOKEN", "dummy")
    monkeypatch.setattr(_xi, "X_LIST_ID", "123")
    monkeypatch.setattr(_xi, "record_x_api_call", lambda *a, **k: None)
    monkeypatch.setattr(_xi, "_track_call", lambda *a, **k: None)

    captured = {}

    class _Resp:
        status_code = 200
        text = ""
        def json(self):
            return {"data": [], "includes": {"users": []}, "meta": {}}

    class _Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *e): return False
        async def get(self, url, params=None, headers=None):
            captured["params"] = dict(params or {})
            return _Resp()

    monkeypatch.setattr(_xi.httpx, "AsyncClient", _Client)

    tweets, diag = await _xi.fetch_timeline_via_list(
        hours=48, caller="test", since_id="555"
    )
    assert "exclude" not in captured["params"]
    assert "since_id" not in captured["params"]
    assert diag is None


# ─── R-TS-BOUND (2026-09-01) — el bug que rompia TODA ventana "since" ───────
# x_api_calls.ts lo escribe SQLite con CURRENT_TIMESTAMP → "YYYY-MM-DD HH:MM:SS".
# Los bounds llegaban en .isoformat() → "YYYY-MM-DDTHH:MM:SS+00:00". En
# comparacion TEXT ' ' (0x20) < 'T' (0x54), asi que CUALQUIER fila cuyo dia
# coincide con el dia del corte quedaba por debajo del bound y se descartaba.
# Efecto en prod: posts_fetched_today() == 0 SIEMPRE, y todas las ventanas
# rolling perdian el dia frontera. Estos tests fallan sin la normalizacion.

def test_posts_fetched_today_counts_a_call_recorded_right_now(fresh_store):
    from modules import intel_memory as im
    xs = fresh_store
    im.record_x_api_call(endpoint="tweets/search", status=200, tweets_returned=300)
    assert xs.posts_fetched_today() == 300
    assert xs.posts_fetched_month() == 300


def test_x_call_counters_see_a_call_recorded_right_now(fresh_store):
    from modules import intel_memory as im
    im.record_x_api_call(endpoint="tweets/search", status=200, tweets_returned=7)
    assert im.count_x_calls_since(24) == 1
    assert im.count_x_calls_today_calendar() == 1
    assert im.count_x_calls_today_live_only() == 1
    assert im.x_api_cost_projection()["calls_7d"] == 1


def test_ts_bound_normalises_both_datetime_and_iso_string():
    from modules.intel_memory import ts_bound
    dt = datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert ts_bound(dt) == "2026-09-01 00:00:00"
    assert ts_bound("2026-09-01T00:00:00+00:00") == "2026-09-01 00:00:00"
    assert ts_bound("2026-09-01T00:00:00Z") == "2026-09-01 00:00:00"
    assert ts_bound("2026-09-01 00:00:00") == "2026-09-01 00:00:00"
    assert ts_bound("") == ""
    assert ts_bound(None) == ""
