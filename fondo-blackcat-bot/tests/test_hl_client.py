"""R-BOT-DEFINITIVE WI-4 — shared HL client tests (rate limit + cache + stale)."""
from __future__ import annotations

import asyncio
import importlib

import pytest


@pytest.fixture
def hc(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import modules.hl_client as hl_client
    importlib.reload(hl_client)
    return hl_client


class _FakeResp:
    def __init__(self, status_code=200, data=None):
        self.status_code = status_code
        self._data = data if data is not None else {"ok": True}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _patch_httpx(monkeypatch, hc, responses):
    """responses = list of (status, data) consumed per request."""
    calls = {"n": 0}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            i = min(calls["n"], len(responses) - 1)
            calls["n"] += 1
            st, data = responses[i]
            return _FakeResp(st, data)

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    return calls


def test_in_run_dedupe_same_payload_one_request(hc, monkeypatch):
    calls = _patch_httpx(monkeypatch, hc, [(200, {"v": 1})])

    async def go():
        r1 = await hc.post_info({"type": "perpDexs"})
        r2 = await hc.post_info({"type": "perpDexs"})
        return r1, r2

    r1, r2 = asyncio.run(go())
    assert r1 == r2 == {"v": 1}
    assert calls["n"] == 1  # second call served from TTL cache


def test_different_payloads_not_shared(hc, monkeypatch):
    calls = _patch_httpx(monkeypatch, hc, [(200, {"v": 1}), (200, {"v": 2})])

    async def go():
        a = await hc.post_info({"type": "perpDexs"})
        b = await hc.post_info({"type": "predictedFundings"})
        return a, b

    a, b = asyncio.run(go())
    assert a != b
    assert calls["n"] == 2


def test_429_retried_with_backoff_then_success(hc, monkeypatch):
    monkeypatch.setattr(hc, "_backoff_delay", lambda attempt: 0.0)
    calls = _patch_httpx(monkeypatch, hc, [(429, {}), (429, {}), (200, {"ok": 1})])

    async def go():
        return await hc.post_info({"type": "metaAndAssetCtxs"})

    assert asyncio.run(go()) == {"ok": 1}
    assert calls["n"] == 3


def test_stale_serve_and_note(hc, monkeypatch):
    monkeypatch.setattr(hc, "_backoff_delay", lambda attempt: 0.0)
    # First: success → cached.
    _patch_httpx(monkeypatch, hc, [(200, {"fresh": True})])

    async def first():
        return await hc.post_info({"type": "metaAndAssetCtxs"})

    asyncio.run(first())
    # Age the cache beyond TTL, then make every request fail.
    key = hc._key({"type": "metaAndAssetCtxs"})
    with hc._cache_lock:
        ts, val = hc._cache[key]
        hc._cache[key] = (ts - 10_000, val)
    _patch_httpx(monkeypatch, hc, [(500, {})])

    async def second():
        return await hc.post_info({"type": "metaAndAssetCtxs"})

    out = asyncio.run(second())
    assert out == {"fresh": True}  # stale-served, never n/d
    note = hc.stale_note("metaAndAssetCtxs")
    assert note.startswith("cached ") and note.endswith("s ago")


def test_no_cache_no_success_raises(hc, monkeypatch):
    monkeypatch.setattr(hc, "_backoff_delay", lambda attempt: 0.0)
    _patch_httpx(monkeypatch, hc, [(500, {})])

    async def go():
        return await hc.post_info({"type": "spotMeta"})

    with pytest.raises(Exception):
        asyncio.run(go())


def test_token_bucket_limits_rate(hc):
    b = hc._TokenBucket(rate=5.0, burst=2.0)
    assert b.try_acquire() == 0.0
    assert b.try_acquire() == 0.0
    wait = b.try_acquire()
    assert wait > 0.0  # burst exhausted → must wait


def test_sync_path_shares_cache(hc, monkeypatch):
    _patch_httpx(monkeypatch, hc, [(200, {"shared": 1})])

    async def warm():
        return await hc.post_info({"type": "userVaultEquities", "user": "0xabc"})

    asyncio.run(warm())

    def _boom(*a, **k):
        raise AssertionError("sync path must hit the shared cache, not the network")

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    out = hc.post_info_sync({"type": "userVaultEquities", "user": "0xabc"})
    assert out == {"shared": 1}


def test_post_json_routes_hl_info_through_client(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import modules.hl_client as hl_client
    importlib.reload(hl_client)
    seen = {}

    async def fake_post_info(payload, **kw):
        seen["payload"] = payload
        return {"routed": True}

    monkeypatch.setattr(hl_client, "post_info", fake_post_info)
    from utils.http import post_json

    async def go():
        return await post_json("https://api.hyperliquid.xyz/info", {"type": "perpDexs"})

    out = asyncio.run(go())
    assert out == {"routed": True}
    assert seen["payload"] == {"type": "perpDexs"}
