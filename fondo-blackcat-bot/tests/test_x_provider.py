"""R-UNIFIED-LIQ Phase B — twitterapi.io transport acceptance tests.

Locked-in contracts:
  1. X_LIST_ID accepts a raw numeric id OR a full list URL.
  2. Dark deploy: without X_PROVIDER_API_KEY the provider is INACTIVE and the
     official client keeps serving; with the key, the provider serves and
     ZERO api.x.com calls happen (pinned).
  3. Provider tweets are emitted in the EXACT canonical shape of
     ``fetch_timeline_via_list`` and the client-side since_id STRING frontier
     stops pagination at the stored high-water mark.
  4. Fallback lifecycle: list endpoint failure → advanced_search batched over
     cached list member handles (weekly cache), client-side merge/dedupe,
     ONE deduped fallback event.
  5. Credits cost accounting: provider/ endpoints bill
     $0.0015/call + $0.15/1K tweets (NOT the Console $5/1K rate).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from modules import x_provider as xp


def _fmt_provider_ts(hours_ago: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return dt.strftime("%a %b %d %H:%M:%S %z %Y")


def _ptweet(tid: int, hours_ago: float, user: str = "acc1", **extra):
    t = {
        "id": str(tid),
        "text": f"t{tid}",
        "createdAt": _fmt_provider_ts(hours_ago),
        "author": {"userName": user, "name": user.upper(), "isBlueVerified": True},
        "likeCount": 3,
        "retweetCount": 1,
        "replyCount": 0,
        "quoteCount": 0,
    }
    t.update(extra)
    return t


class _Resp:
    def __init__(self, payload, status=200, text=""):
        self._payload = payload
        self.status_code = status
        self.text = text

    def json(self):
        return self._payload


def _client_for(pages, captured=None):
    calls = {"i": 0}

    class _Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *e): return False

        async def get(self, url, params=None, headers=None):
            if captured is not None:
                captured.append({"url": url, "params": dict(params or {}),
                                 "headers": dict(headers or {})})
            i = min(calls["i"], len(pages) - 1)
            calls["i"] += 1
            return pages[i]

    return _Client


@pytest.fixture()
def provider_env(monkeypatch, tmp_path):
    monkeypatch.setenv("X_PROVIDER_API_KEY", "test-key")
    monkeypatch.setenv("X_LIST_ID", "2046698139873378486")
    monkeypatch.delenv("X_FETCH_BACKEND", raising=False)
    monkeypatch.setenv("X_EXCLUDE_RT_REPLIES", "true")
    monkeypatch.setattr(xp, "_record", lambda *a, **k: None)
    monkeypatch.setattr(
        xp, "_member_cache_path",
        lambda: str(tmp_path / "members.json"),
    )
    return monkeypatch


# ── 1. list id normalization ────────────────────────────────────────────────

def test_normalize_list_id_raw_and_urls():
    lid = "2046698139873378486"
    assert xp.normalize_list_id(lid) == lid
    assert xp.normalize_list_id(f"https://x.com/i/lists/{lid}") == lid
    assert xp.normalize_list_id(f"https://twitter.com/i/lists/{lid}?src=a") == lid
    assert xp.normalize_list_id("") == ""
    assert xp.normalize_list_id(None) == ""


# ── 2. dark-deploy selection contract ───────────────────────────────────────

def test_provider_inactive_without_key(monkeypatch):
    monkeypatch.delenv("X_PROVIDER_API_KEY", raising=False)
    monkeypatch.delenv("X_FETCH_BACKEND", raising=False)
    assert xp.backend_selected() == "twitterapi_io"  # default selector
    assert xp.provider_active() is False             # …but dark without key
    assert xp.backend_name() == "official"


def test_provider_active_with_key_and_official_override(monkeypatch):
    monkeypatch.setenv("X_PROVIDER_API_KEY", "k")
    monkeypatch.delenv("X_FETCH_BACKEND", raising=False)
    assert xp.provider_active() is True
    assert xp.backend_name() == "twitterapi_io"
    monkeypatch.setenv("X_FETCH_BACKEND", "official")
    assert xp.provider_active() is False
    assert xp.backend_name() == "official"


# ── 3. list fetch: canonical shape + frontier ───────────────────────────────

@pytest.mark.asyncio
async def test_list_fetch_canonical_shape_and_frontier(provider_env):
    b = 9_100_000_000_000_000_001  # frontier above 2^53
    page1 = _Resp({
        "tweets": [_ptweet(b + 30, 0.5), _ptweet(b + 20, 1.0)],
        "has_next_page": True, "next_cursor": "c2",
    })
    page2 = _Resp({
        "tweets": [_ptweet(b + 10, 2.0), _ptweet(b, 3.0), _ptweet(b - 10, 4.0)],
        "has_next_page": True, "next_cursor": "c3",
    })
    captured: list[dict] = []
    provider_env.setattr(xp.httpx, "AsyncClient", _client_for([page1, page2], captured))

    tweets, diag = await xp.fetch_timeline(hours=48, caller="test", since_id=str(b))
    assert diag is None
    ids = [int(t["id"]) for t in tweets]
    assert ids == [b + 30, b + 20, b + 10]  # strictly newer than frontier
    assert len(captured) == 2               # page 3 never requested
    assert captured[0]["headers"].get("X-API-Key") == "test-key"
    assert captured[0]["params"]["listId"] == "2046698139873378486"
    # Canonical shape — exact keys the store/scoring/render pipeline expects.
    t0 = tweets[0]
    assert set(t0) == {"id", "username", "name", "verified", "text",
                       "created_at", "metrics", "url"}
    assert isinstance(t0["id"], str)
    assert t0["username"] == "acc1"
    assert t0["metrics"]["like_count"] == 3
    assert t0["url"].startswith("https://x.com/acc1/status/")
    # created_at parses as ISO UTC.
    datetime.fromisoformat(t0["created_at"].replace("Z", "+00:00"))
    # Meta mirrors the billed pages/tweets for /xrefresh cost lines.
    assert xp.last_fetch_meta() == {"pages": 2, "returned": 5}


@pytest.mark.asyncio
async def test_rt_replies_filtered_client_side(provider_env):
    page = _Resp({
        "tweets": [
            _ptweet(3001, 0.2),
            _ptweet(3002, 0.3, retweeted_tweet={"id": "1"}),
            _ptweet(3003, 0.4, isReply=True),
            _ptweet(3004, 0.5, quoted_tweet={"id": "2"}),  # quotes KEPT
        ],
        "has_next_page": False, "next_cursor": "",
    })
    provider_env.setattr(xp.httpx, "AsyncClient", _client_for([page]))
    tweets, diag = await xp.fetch_timeline(hours=48, caller="test")
    assert diag is None
    assert [t["id"] for t in tweets] == ["3001", "3004"]


# ── 4. fallback lifecycle ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fallback_members_search_and_dedupe(provider_env):
    listing_fail = _Resp({}, status=500, text="boom")
    members_page = _Resp({
        "members": [{"userName": "acc1"}, {"userName": "acc2"}],
        "has_next_page": False, "next_cursor": "",
    })
    search_page = _Resp({
        "tweets": [_ptweet(4002, 0.5), _ptweet(4001, 1.0),
                   _ptweet(4002, 0.5)],  # duplicate id → deduped
        "has_next_page": False, "next_cursor": "",
    })

    calls = {"n": 0}

    class _Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *e): return False

        async def get(self, url, params=None, headers=None):
            calls["n"] += 1
            if "list/tweets" in url:
                return listing_fail
            if "list/members" in url:
                return members_page
            assert "advanced_search" in url
            assert "from:acc1" in params["query"]
            return search_page

    provider_env.setattr(xp.httpx, "AsyncClient", _Client)
    tweets, diag = await xp.fetch_timeline(hours=48, caller="test")
    assert diag is None
    assert sorted(t["id"] for t in tweets) == ["4001", "4002"]  # deduped
    ev = xp.pop_fallback_event()
    assert ev and ev["active"] is True   # ONE event…
    assert xp.pop_fallback_event() is None  # …consumed exactly once
    # Member handles were cached to disk.
    handles, ts = xp._load_member_cache()
    assert handles == ["acc1", "acc2"] and ts > 0


@pytest.mark.asyncio
async def test_member_cache_reused_within_week(provider_env):
    xp._save_member_cache(["cachedacc"])

    class _NoMembersClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *e): return False

        async def get(self, url, params=None, headers=None):
            raise AssertionError("members endpoint must not be re-fetched")

    provider_env.setattr(xp.httpx, "AsyncClient", _NoMembersClient)
    handles = await xp.get_list_members(caller="test")
    assert handles == ["cachedacc"]


# ── 5. zero official api.x.com calls under the provider backend ─────────────

@pytest.mark.asyncio
async def test_zero_official_calls_when_provider_active(provider_env, tmp_path):
    from modules import x_intel as _xi
    from modules import x_store, intel_memory
    db = tmp_path / "intel_memory.db"
    provider_env.setattr(x_store, "DB_PATH", str(db))
    provider_env.setattr(intel_memory, "DB_PATH", str(db), raising=False)
    provider_env.setattr(_xi, "X_LIVE_ENABLED", True)
    provider_env.setattr(_xi, "X_EXTRA_HANDLES", [])
    provider_env.setattr(_xi, "save_x_timeline_payload", lambda *a, **k: None)

    async def _official_forbidden(*a, **k):
        raise AssertionError("official api.x.com client must NOT be called "
                             "while the provider backend is active")

    provider_env.setattr(_xi, "fetch_timeline_via_list", _official_forbidden)
    page = _Resp({
        "tweets": [_ptweet(5001, 0.5)],
        "has_next_page": False, "next_cursor": "",
    })
    provider_env.setattr(xp.httpx, "AsyncClient", _client_for([page]))

    payload = await _xi.fetch_x_intel(hours=48, caller="test")
    assert payload.get("fetched_new") == 1
    assert x_store.get_since_id() == "5001"


# ── 6. provider credits cost accounting ─────────────────────────────────────

def test_provider_cost_model_in_record(tmp_path, monkeypatch):
    from modules import intel_memory as im
    db = tmp_path / "intel_memory.db"
    monkeypatch.setattr(im, "DB_PATH", str(db), raising=False)
    im.record_x_api_call("provider/list_tweets", 200, pages=4,
                         tweets_returned=2000, caller="test")
    im.record_x_api_call("lists/tweets", 200, pages=4,
                         tweets_returned=2000, caller="test")
    conn = im._get_conn()
    rows = conn.execute(
        "SELECT endpoint, est_cost_usd FROM x_api_calls ORDER BY id"
    ).fetchall()
    conn.close()
    costs = {r["endpoint"]: r["est_cost_usd"] for r in rows}
    # provider: 4×$0.0015 + 2×$0.15 = $0.306  |  official: 2×$5 = $10.
    assert costs["provider/list_tweets"] == pytest.approx(0.306, abs=1e-9)
    assert costs["lists/tweets"] == pytest.approx(10.0, abs=1e-9)


def test_health_x_source_key(monkeypatch):
    monkeypatch.setenv("X_PROVIDER_API_KEY", "k")
    monkeypatch.delenv("X_FETCH_BACKEND", raising=False)
    from modules.version_info import _x_source_safe
    assert _x_source_safe() == "twitterapi_io"
    monkeypatch.delenv("X_PROVIDER_API_KEY", raising=False)
    assert _x_source_safe() == "official"
