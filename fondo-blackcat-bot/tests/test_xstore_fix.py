"""R-XSTORE-FIX (2026-07-27) — acceptance fixtures.

Production incident: after the Jul-23 backfill (482 tweets), every incremental
fetch returned "+0 new" forever. Root cause (verified live): the
/2/lists/{id}/tweets endpoint 400-rejects `since_id` and `exclude` — allowed
params are only [id, max_results, pagination_token, post.fields]. The 400 was
swallowed into a degrade-to-store path that /xrefresh rendered as "+0 new
posts fetched (~$0.00)".

Locked-in contracts:
  1. since_id round-trips as a STRING with ids above 2^53 (no float/JSON
     precision loss).
  2. Client-side boundary: pagination walks next_token and stops at the first
     tweet id ≤ since_id; only newer tweets are returned.
  3. A live failure sets `live_error` and /xrefresh renders
     "fetch error: <reason>" — NEVER "+0 new". "0 new, fetch OK" is a
     distinct, explicit message.
  4. Every fetch attempt (success or error) lands in the persistent
     x_fetch_log with since_id sent, pages walked, posts returned.
  5. Static: no silent-swallow (`except: pass`) in the X fetch path, and the
     rejected params are never reintroduced.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _api_tweet(tid: int, hours_ago: float, author: str = "u1"):
    return {
        "id": str(tid),
        "author_id": author,
        "text": f"t{tid}",
        "created_at": _iso(hours_ago),
        "public_metrics": {"like_count": 1},
    }


@pytest.fixture()
def fresh_store(tmp_path, monkeypatch):
    from modules import x_store, intel_memory
    db = tmp_path / "intel_memory.db"
    monkeypatch.setattr(x_store, "DB_PATH", str(db))
    monkeypatch.setattr(intel_memory, "DB_PATH", str(db), raising=False)
    return x_store


def _patch_live(monkeypatch):
    from modules import x_intel as _xi
    monkeypatch.setattr(_xi, "X_LIVE_ENABLED", True)
    monkeypatch.setattr(_xi, "X_API_BEARER_TOKEN", "dummy")
    monkeypatch.setattr(_xi, "X_LIST_ID", "123")
    monkeypatch.setattr(_xi, "X_EXTRA_HANDLES", [])
    monkeypatch.setattr(_xi, "record_x_api_call", lambda *a, **k: None)
    monkeypatch.setattr(_xi, "_track_call", lambda *a, **k: None)
    monkeypatch.setattr(_xi, "save_x_timeline_payload", lambda *a, **k: None)
    return _xi


class _Resp:
    def __init__(self, payload, status=200, text=""):
        self._payload = payload
        self.status_code = status
        self.text = text
    def json(self):
        return self._payload


def _client_for(pages, captured=None):
    """Fake httpx.AsyncClient serving a fixed sequence of page payloads."""
    calls = {"i": 0}

    class _Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *e): return False
        async def get(self, url, params=None, headers=None):
            if captured is not None:
                captured.append(dict(params or {}))
            i = min(calls["i"], len(pages) - 1)
            calls["i"] += 1
            return pages[i]

    return _Client


# ── 1. since_id STRING round-trip above 2^53 ────────────────────────────────

def test_since_id_string_roundtrip_above_2e53(fresh_store):
    xs = fresh_store
    big = str(2**63 - 25)  # 9223372036854775783 — far beyond float precision
    xs.set_since_id(big)
    got = xs.get_since_id()
    assert got == big and isinstance(got, str)
    # A float round-trip WOULD corrupt it — prove the store didn't take it:
    assert str(int(float(big))) != big  # float is lossy here…
    assert int(got) == 2**63 - 25       # …but the store is exact.
    # Never regress, exact int comparison (not lexicographic/float):
    xs.set_since_id(str(2**63 - 30))
    assert xs.get_since_id() == big


# ── 2. client-side boundary + pagination ────────────────────────────────────

@pytest.mark.asyncio
async def test_boundary_stops_pagination_and_filters_old_ids(fresh_store, monkeypatch):
    _xi = _patch_live(monkeypatch)
    boundary = 10_000_000_000_000_000_000  # > 2^63? no: keep < 2^63; use big id
    boundary = 9_100_000_000_000_000_001
    page1 = _Resp({
        "data": [_api_tweet(boundary + 30, 0.5), _api_tweet(boundary + 20, 1.0)],
        "includes": {"users": [{"id": "u1", "username": "acc1", "name": "A"}]},
        "meta": {"next_token": "tok2"},
    })
    page2 = _Resp({
        "data": [_api_tweet(boundary + 10, 2.0), _api_tweet(boundary, 3.0),
                 _api_tweet(boundary - 10, 4.0)],
        "includes": {"users": [{"id": "u1", "username": "acc1", "name": "A"}]},
        "meta": {"next_token": "tok3"},
    })
    captured: list[dict] = []
    monkeypatch.setattr(_xi.httpx, "AsyncClient", _client_for([page1, page2], captured))

    tweets, diag = await _xi.fetch_timeline_via_list(
        hours=48, caller="test", since_id=str(boundary)
    )
    assert diag is None
    ids = [int(t["id"]) for t in tweets]
    # Only ids STRICTLY newer than the boundary are returned:
    assert ids == [boundary + 30, boundary + 20, boundary + 10]
    # next_token was walked (page 2 requested), then boundary stopped page 3:
    assert len(captured) == 2
    assert captured[1].get("pagination_token") == "tok2"


@pytest.mark.asyncio
async def test_pagination_walks_next_token_until_exhausted(fresh_store, monkeypatch):
    _xi = _patch_live(monkeypatch)
    base = 9_100_000_000_000_000_000
    page1 = _Resp({
        "data": [_api_tweet(base + 3, 0.5)],
        "includes": {"users": [{"id": "u1", "username": "acc1"}]},
        "meta": {"next_token": "tok2"},
    })
    page2 = _Resp({
        "data": [_api_tweet(base + 2, 1.0)],
        "includes": {"users": [{"id": "u1", "username": "acc1"}]},
        "meta": {},  # exhausted
    })
    captured: list[dict] = []
    monkeypatch.setattr(_xi.httpx, "AsyncClient", _client_for([page1, page2], captured))
    tweets, diag = await _xi.fetch_timeline_via_list(hours=48, caller="test", since_id=None)
    assert diag is None
    assert len(tweets) == 2 and len(captured) == 2


# ── 3. error surfacing: live failure NEVER renders as "+0 new" ──────────────

@pytest.mark.asyncio
async def test_forced_error_sets_live_error_and_renders_fetch_error(fresh_store, monkeypatch):
    from modules import x_store as xs
    _xi = _patch_live(monkeypatch)
    # Pre-seed the store so the degrade path has a window to serve.
    xs.upsert_tweets([{
        "id": "9100000000000000001", "username": "acc1", "text": "old",
        "created_at": _iso(2.0), "metrics": {}, "url": "https://x.com/acc1/status/9100000000000000001",
    }])
    xs.set_since_id("9100000000000000001")
    err_resp = _Resp({"title": "Invalid Request"}, status=400,
                     text='{"errors":[{"message":"The query parameter [since_id] is not one of ..."}]}')
    monkeypatch.setattr(_xi.httpx, "AsyncClient", _client_for([err_resp]))

    payload = await _xi.fetch_x_intel(hours=48, caller="xrefresh")
    assert payload.get("live_error")
    msg = _xi.render_xrefresh_result(payload)
    assert "fetch error" in msg.lower()
    assert "+0 new" not in msg
    # The failure is persisted in the fetch log with the since_id sent:
    flog = xs.recent_fetch_log(1)
    assert flog and flog[0]["error"] and flog[0]["since_id_sent"] == "9100000000000000001"


def test_zero_new_fetch_ok_is_distinct_from_error():
    from modules import x_intel as _xi
    ok0 = _xi.render_xrefresh_result({"status": "ok", "fetched_new": 0, "total": 482})
    assert "fetch OK" in ok0 and "error" not in ok0.lower()
    okn = _xi.render_xrefresh_result({"status": "ok", "fetched_new": 7, "total": 489})
    assert "+7 new" in okn


@pytest.mark.asyncio
async def test_successful_fetch_logged_persistently(fresh_store, monkeypatch):
    from modules import x_store as xs
    _xi = _patch_live(monkeypatch)
    base = 9_100_000_000_000_000_000
    page = _Resp({
        "data": [_api_tweet(base + 5, 0.5)],
        "includes": {"users": [{"id": "u1", "username": "acc1"}]},
        "meta": {},
    })
    monkeypatch.setattr(_xi.httpx, "AsyncClient", _client_for([page]))
    payload = await _xi.fetch_x_intel(hours=48, caller="reporte")
    assert payload.get("status") == "ok" and payload.get("fetched_new") == 1
    assert xs.get_since_id() == str(base + 5)
    flog = xs.recent_fetch_log(1)
    assert flog and flog[0]["error"] is None
    assert flog[0]["posts_returned"] == 1 and flog[0]["new_stored"] == 1
    assert flog[0]["pages"] == 1


# ── 5. static guards ────────────────────────────────────────────────────────

def _fetch_path_sources() -> str:
    return "\n".join(
        (REPO / "modules" / f).read_text(encoding="utf-8")
        for f in ("x_intel.py", "x_store.py")
    )


def test_static_no_silent_swallow_in_fetch_path():
    """No `except ...: pass` (or `except: ...pass` one-liner) may exist in the
    X fetch path — every failure must be logged/surfaced, never swallowed."""
    src = _fetch_path_sources()
    silent = re.findall(r"except[^\n]*:\s*(?:#[^\n]*)?\n\s+pass\b", src)
    silent += re.findall(r"except[^\n]*:\s*pass\b", src)
    assert not silent, f"silent except/pass in X fetch path: {silent}"


def test_static_rejected_params_never_sent():
    """The list endpoint 400-rejects since_id/exclude — they must never be
    reintroduced as request params (verified live 2026-07-27)."""
    src = (REPO / "modules" / "x_intel.py").read_text(encoding="utf-8")
    assert 'params["since_id"]' not in src
    assert "params['since_id']" not in src
    assert 'params["exclude"]' not in src
    assert "params['exclude']" not in src


def test_static_xrefresh_uses_error_aware_renderer():
    src = (REPO / "bot.py").read_text(encoding="utf-8")
    assert "render_xrefresh_result" in src
