"""R-COST-V2 (2026-07-23) — acceptance fixtures.

Locks in the four contract points of the X cost optimization:

1. INCREMENTAL: fetch_x_intel passes the persisted since_id to the API and
   never re-ingests an already-stored tweet id (INSERT OR IGNORE).
2. PRUNE: tweets older than 72h are removed from the local store.
3. BUDGET 80%: one warning per calendar month when MTD posts cross 80%.
4. BUDGET 100%: fetch_x_intel makes ZERO API calls and renders from the
   store with budget_exhausted=True (cache banner in /reporte).

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


# ─── 3 + 4. Monthly budget guard ────────────────────────────────────────────

def _record_posts(n: int, db_module):
    """Write API-call rows so posts_fetched_month() sees n posts MTD."""
    from modules import intel_memory
    intel_memory.record_x_api_call("lists/tweets", 200, pages=1,
                                   tweets_returned=n, caller="test")


def test_budget_80pct_warning_fires_once_per_month(fresh_store, monkeypatch):
    xs = fresh_store
    monkeypatch.setattr(xs, "MONTHLY_POST_BUDGET", 1000)
    _record_posts(850, xs)
    b = xs.budget_state()
    assert 80.0 <= b["pct"] < 100.0
    assert not b["exhausted"]
    assert xs.should_send_budget_warning(b["pct"]) is True
    # Second crossing in the same month: throttled.
    assert xs.should_send_budget_warning(b["pct"]) is False


def test_budget_below_80_no_warning(fresh_store, monkeypatch):
    xs = fresh_store
    monkeypatch.setattr(xs, "MONTHLY_POST_BUDGET", 1000)
    _record_posts(500, xs)
    assert xs.should_send_budget_warning(xs.budget_state()["pct"]) is False


@pytest.mark.asyncio
async def test_budget_exhausted_renders_cache_only_zero_api_calls(fresh_store, monkeypatch):
    from modules import x_intel as _xi
    xs = fresh_store
    monkeypatch.setattr(_xi, "X_LIVE_ENABLED", True)
    monkeypatch.setattr(_xi, "save_x_timeline_payload", lambda *a, **k: None)
    monkeypatch.setattr(xs, "MONTHLY_POST_BUDGET", 100)
    monkeypatch.setattr(xs, "BUDGET_OVERRIDE", False)
    _record_posts(120, xs)  # over budget
    xs.upsert_tweets([_mk_tweet("400", hours_ago=1)])

    async def _boom(*a, **k):
        raise AssertionError("X API called while budget exhausted")

    monkeypatch.setattr(_xi, "fetch_timeline_via_list", _boom)

    payload = await _xi.fetch_x_intel(hours=48, caller="test", app=None)
    assert payload["budget_exhausted"] is True
    assert payload["from_store"] is True
    assert payload["total"] == 1  # cached timeline still renders


def test_budget_override_unblocks(fresh_store, monkeypatch):
    xs = fresh_store
    monkeypatch.setattr(xs, "MONTHLY_POST_BUDGET", 100)
    monkeypatch.setattr(xs, "BUDGET_OVERRIDE", True)
    _record_posts(120, xs)
    assert xs.budget_state()["exhausted"] is False


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


# ─── RT/reply exclusion at query level ──────────────────────────────────────

@pytest.mark.asyncio
async def test_exclude_rt_replies_param_sent(fresh_store, monkeypatch):
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

    await _xi.fetch_timeline_via_list(hours=48, caller="test", since_id="555")
    assert captured["params"].get("exclude") == "retweets,replies"
    assert captured["params"].get("since_id") == "555"
