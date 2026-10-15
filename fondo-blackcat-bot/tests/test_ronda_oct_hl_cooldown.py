"""R-RONDA-OCT #2 — candleSnapshot 429/500 storm in modules.hl_client.

Doctrine (same as the CoinGecko fix of R-LIVE-FIX, applied to HL):
  * a 429 is NEVER retried inside the call: the host enters a cooldown;
  * while the host (or the payload type, after a 5xx storm) is cooling down
    NO request touches the network — stale cache or ``HostCoolingDown``;
  * the budget is HL's real meter (weight per minute), so a candle sweep
    cannot be scheduled faster than the API allows;
  * candles go through ONE shared cache keyed on (coin, interval): any
    shorter window is a slice of the longest series fetched;
  * the universal screener stops sweeping a cooling-down host instead of
    retrying every name against it.

Every test here is mutation-checked by scripts/mutation_check_ronda_oct.py.
"""
from __future__ import annotations

import asyncio
import importlib
import time

import pytest


@pytest.fixture
def hc(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import modules.hl_client as hl_client
    importlib.reload(hl_client)
    hl_client.reset_cooldowns()
    hl_client.clear_candle_cache()
    monkeypatch.setattr(hl_client, "_backoff_delay", lambda attempt: 0.0)
    yield hl_client
    hl_client.reset_cooldowns()
    hl_client.clear_candle_cache()


class _FakeResp:
    def __init__(self, status_code=200, data=None, headers=None):
        self.status_code = status_code
        self._data = data if data is not None else {"ok": True}
        self.headers = headers or {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _patch_httpx(monkeypatch, responses):
    """responses = list of (status, data[, headers]) consumed per request."""
    calls = {"n": 0, "payloads": []}

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
            calls["payloads"].append(json)
            item = responses[i]
            st, data = item[0], item[1]
            hdr = item[2] if len(item) > 2 else None
            return _FakeResp(st, data, hdr)

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    return calls


def _run(coro):
    return asyncio.run(coro)


# ─── 429 → cooldown, no network ─────────────────────────────────────────────

def test_429_puts_host_in_cooldown_and_blocks_the_next_call(hc, monkeypatch):
    calls = _patch_httpx(monkeypatch, [(429, {}), (200, {"ok": 1})])
    with pytest.raises(Exception):
        _run(hc.post_info({"type": "candleSnapshot", "req": {"coin": "BTC"}}))
    assert calls["n"] == 1
    assert hc.cooldown_remaining() > 0
    # A DIFFERENT payload on the same host must not touch the network either.
    from utils.http import HostCoolingDown
    with pytest.raises(HostCoolingDown):
        _run(hc.post_info({"type": "spotMeta"}))
    assert calls["n"] == 1


def test_429_honours_retry_after_header(hc, monkeypatch):
    _patch_httpx(monkeypatch, [(429, {}, {"Retry-After": "7"})])
    with pytest.raises(Exception):
        _run(hc.post_info({"type": "candleSnapshot", "req": {"coin": "ETH"}}))
    rem = hc.cooldown_remaining()
    assert 5.0 < rem <= 7.5


def test_429_without_retry_after_uses_hl_base_and_escalates(hc, monkeypatch):
    _patch_httpx(monkeypatch, [(429, {})])
    with pytest.raises(Exception):
        _run(hc.post_info({"type": "candleSnapshot", "req": {"coin": "ETH"}}))
    first = hc.cooldown_remaining()
    assert hc._COOLDOWN_BASE_SEC - 1 < first <= hc._COOLDOWN_BASE_SEC
    # Second strike (simulate the cooldown expiring, then another 429).
    from utils import http as _uh
    _uh._HOST_COOLDOWN[_uh._host_of(hc.INFO_URL)]["until"] = time.time() - 1
    with pytest.raises(Exception):
        _run(hc.post_info({"type": "candleSnapshot", "req": {"coin": "SOL"}}))
    second = hc.cooldown_remaining()
    assert second > first * 1.5          # exponential, not flat
    assert second <= hc._COOLDOWN_MAX_SEC


def test_cooldown_serves_stale_cache_without_network(hc, monkeypatch):
    calls = _patch_httpx(monkeypatch, [(200, {"fresh": True})])
    _run(hc.post_info({"type": "metaAndAssetCtxs"}))
    key = hc._key({"type": "metaAndAssetCtxs"})
    with hc._cache_lock:
        ts, val = hc._cache[key]
        hc._cache[key] = (ts - 10_000, val)          # expired
    _patch_httpx(monkeypatch, [(429, {})])
    with pytest.raises(Exception):
        _run(hc.post_info({"type": "spotMeta"}))     # trips the cooldown
    calls2 = _patch_httpx(monkeypatch, [(200, {"must": "not be fetched"})])
    out = _run(hc.post_info({"type": "metaAndAssetCtxs"}))
    assert out == {"fresh": True}
    assert calls2["n"] == 0
    assert hc.stale_note("metaAndAssetCtxs").startswith("cached ")


def test_success_clears_the_host_cooldown(hc, monkeypatch):
    _patch_httpx(monkeypatch, [(429, {})])
    with pytest.raises(Exception):
        _run(hc.post_info({"type": "spotMeta"}))
    from utils import http as _uh
    _uh._HOST_COOLDOWN[_uh._host_of(hc.INFO_URL)]["until"] = time.time() - 1
    _patch_httpx(monkeypatch, [(200, {"ok": 1})])
    assert _run(hc.post_info({"type": "spotMeta", "x": 1})) == {"ok": 1}
    assert hc.cooldown_remaining() == 0.0
    from utils.http import cooldown_strikes
    assert cooldown_strikes(hc.INFO_URL) == 0      # strike counter reset, not just expired


# ─── 5xx storm → per-TYPE cooldown, host stays usable ──────────────────────

def test_5xx_storm_cools_only_that_payload_type(hc, monkeypatch):
    calls = _patch_httpx(monkeypatch, [(500, {})] * 10)
    with pytest.raises(Exception):
        _run(hc.post_info({"type": "candleSnapshot", "req": {"coin": "BTC"}}))
    assert calls["n"] == hc._MAX_RETRIES_5XX          # bounded, not 4+
    assert hc.cooling_down("candleSnapshot")
    assert not hc.cooling_down()                        # host itself is free
    # Another candle call: no network.
    from utils.http import HostCoolingDown
    with pytest.raises(HostCoolingDown):
        _run(hc.post_info({"type": "candleSnapshot", "req": {"coin": "ETH"}}))
    assert calls["n"] == hc._MAX_RETRIES_5XX
    # A different type still reaches the network.
    calls2 = _patch_httpx(monkeypatch, [(200, {"ok": 1})])
    assert _run(hc.post_info({"type": "spotMeta"})) == {"ok": 1}
    assert calls2["n"] == 1


def test_sync_path_shares_cooldown_and_never_retries_429(hc, monkeypatch):
    import urllib.error

    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(hc.INFO_URL, 429, "Too Many", {}, None)

    monkeypatch.setattr(hc.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(Exception):
        hc.post_info_sync({"type": "spotMeta"})
    assert calls["n"] == 1
    assert hc.cooling_down()
    from utils.http import HostCoolingDown
    with pytest.raises(HostCoolingDown):
        hc.post_info_sync({"type": "allMids"})
    assert calls["n"] == 1


# ─── weight budget ───────────────────────────────────────────────────────────

def test_request_weight_follows_hl_meter():
    import modules.hl_client as h
    assert h.request_weight("candleSnapshot") == 20
    assert h.request_weight("spotClearinghouseState") == 2
    assert h.request_weight("allMids") == 2
    assert h.request_weight("userRole") == 60


def test_weight_bucket_blocks_a_candle_sweep_past_the_minute_budget(hc):
    b = hc._WeightBucket(per_min=100.0)
    # 5 heavy calls fit (100), the 6th must wait.
    for _ in range(5):
        assert b.try_acquire(20) == 0.0
    assert b.try_acquire(20) > 0.0


def test_budget_wait_is_applied_before_the_request(hc, monkeypatch):
    waits = []

    async def fake_sleep(sec):
        waits.append(sec)

    monkeypatch.setattr(hc.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(hc, "_acquire_budget", lambda ptype: 3.0)
    _patch_httpx(monkeypatch, [(200, {"ok": 1})])
    assert _run(hc.post_info({"type": "candleSnapshot", "req": {"coin": "BTC"}})) == {"ok": 1}
    assert waits and waits[0] >= 3.0


# ─── shared candle cache ────────────────────────────────────────────────────

def _candles(n):
    return [{"t": i, "c": str(100 + i), "l": str(90 + i)} for i in range(n)]


def test_get_candles_one_fetch_serves_shorter_windows(hc, monkeypatch):
    calls = _patch_httpx(monkeypatch, [(200, _candles(190))])
    big = _run(hc.get_candles("BTC", "4h", 184))
    small = _run(hc.get_candles("BTC", "4h", 48))
    assert len(big) == 184 and len(small) == 48
    assert small == big[-48:]
    assert calls["n"] == 1                       # the second window was a slice


def test_get_candles_longer_request_refetches_and_keeps_series(hc, monkeypatch):
    calls = _patch_httpx(monkeypatch, [(200, _candles(50)), (200, _candles(190))])
    _run(hc.get_candles("ETH", "4h", 48))
    out = _run(hc.get_candles("ETH", "4h", 184))
    assert len(out) == 184
    assert calls["n"] == 2
    # the payload asked for at least the longer window
    req = calls["payloads"][-1]["req"]
    assert (req["endTime"] - req["startTime"]) >= 184 * hc._INTERVAL_MS["4h"]


def test_get_candles_expired_entry_refetches(hc, monkeypatch):
    calls = _patch_httpx(monkeypatch, [(200, _candles(50)), (200, _candles(50))])
    _run(hc.get_candles("SOL", "4h", 48))
    with hc._candle_lock:
        ts, series = hc._candle_cache[("SOL", "4h")]
        hc._candle_cache[("SOL", "4h")] = (ts - hc._CANDLE_TTL_SEC - 1, series)
    _run(hc.get_candles("SOL", "4h", 48))
    assert calls["n"] == 2


def test_get_candles_end_time_is_minute_bucketed(hc, monkeypatch):
    calls = _patch_httpx(monkeypatch, [(200, _candles(50))])
    _run(hc.get_candles("HYPE", "4h", 10))
    req = calls["payloads"][0]["req"]
    assert req["endTime"] % 60_000 == 0
    assert req["coin"] == "HYPE" and req["interval"] == "4h"


def test_get_candles_bad_shape_raises_not_empty(hc, monkeypatch):
    _patch_httpx(monkeypatch, [(200, {"not": "a list"})])
    with pytest.raises(ValueError):
        _run(hc.get_candles("BTC", "4h", 10))


# ─── callers go through the cache ──────────────────────────────────────────

def test_unlock_monitor_fetch_4h_closes_uses_shared_cache(monkeypatch):
    import modules.unlock_monitor as um
    import modules.hl_client as hc
    seen = {}

    async def fake_get_candles(coin, interval, bars, **kw):
        seen.update(coin=coin, interval=interval, bars=bars)
        return _candles(bars)

    monkeypatch.setattr(hc, "get_candles", fake_get_candles)
    monkeypatch.setattr(um, "HL_INFO_URL", "https://api.hyperliquid.xyz/info")
    closes = asyncio.run(um.fetch_4h_closes("btc", 42))
    assert seen == {"coin": "BTC", "interval": "4h", "bars": 46}
    assert len(closes) == 42 and closes[-1] == 145.0


def test_telemetry_low_7d_uses_shared_cache(monkeypatch):
    import modules.telemetry as tm
    import modules.hl_client as hc

    async def fake_get_candles(coin, interval, bars, **kw):
        assert (interval, bars) == ("4h", 42)
        return _candles(42)

    monkeypatch.setattr(hc, "get_candles", fake_get_candles)
    assert asyncio.run(tm.fetch_low_7d("BTC")) == 90.0


def test_farmdump_daily_closes_use_shared_cache(monkeypatch):
    import modules.farmdump_checks as fd
    import modules.hl_client as hc

    async def fake_ctxs():
        return {"BTC": {}}

    async def fake_get_candles(coin, interval, bars, **kw):
        assert (coin, interval) == ("BTC", "1d")
        return _candles(bars)

    monkeypatch.setattr(fd, "_hl_asset_ctxs", fake_ctxs)
    monkeypatch.setattr(fd, "HL_INFO_URL", "https://api.hyperliquid.xyz/info")
    monkeypatch.setattr(hc, "get_candles", fake_get_candles)
    out = asyncio.run(fd.fetch_hl_daily_closes("BTC", 5))
    assert len(out) == 5


def test_no_raw_candle_payloads_left_outside_hl_client():
    """Every candleSnapshot request must build through hl_client (cache +
    cooldown + budget). A raw payload elsewhere is only allowed behind the
    non-canonical HL_INFO_URL branch (tests)."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "modules"
    offenders = []
    for py in root.rglob("*.py"):
        if py.name in ("hl_client.py",):
            continue
        src = py.read_text(encoding="utf-8")
        for m in re.finditer(r'"type":\s*"candleSnapshot"', src):
            # must sit inside an `else:` branch guarded by HL_INFO_URL check
            head = src[max(0, m.start() - 600):m.start()]
            if "hyperliquid.xyz/info" not in head and "_btc_px_at" not in head:
                offenders.append(f"{py.relative_to(root)}:{src[:m.start()].count(chr(10)) + 1}")
    assert not offenders, offenders


# ─── screener stops sweeping a cooling-down host ───────────────────────────

def test_screener_skips_fetch_when_hl_is_cooling_down(monkeypatch):
    import modules.universal_screener as us
    monkeypatch.setattr(us, "_hl_cooling_down", lambda: True)
    fetched = {"n": 0}

    async def fake_fetch(coin, bars):
        fetched["n"] += 1
        return None

    monkeypatch.setattr(us, "fetch_4h_closes", fake_fetch)
    monkeypatch.setattr(us, "fetch_retries", lambda: 2)
    vi = us.VenueInfo(ticker="ETH", on_hl=True, on_var=False, hl_vol_usd=None,
                      var_vol_usd=None, hl_funding=None, hl_oi=None, var_funding_ann=None)
    k = us.constants()
    sem = asyncio.Semaphore(2)
    us._COOLDOWN_SKIPS["n"] = 0
    row = asyncio.run(us._evaluate_one(vi, k, {}, None, advance_state=False, sem=sem))
    assert fetched["n"] == 0
    assert us._COOLDOWN_SKIPS["n"] == 1
    assert row.data_ok is False


def test_screener_fetches_when_hl_is_free(monkeypatch):
    import modules.universal_screener as us
    monkeypatch.setattr(us, "_hl_cooling_down", lambda: False)
    fetched = {"n": 0}

    async def fake_fetch(coin, bars):
        fetched["n"] += 1
        return None

    monkeypatch.setattr(us, "fetch_4h_closes", fake_fetch)
    monkeypatch.setattr(us, "fetch_retries", lambda: 1)
    vi = us.VenueInfo(ticker="ETH", on_hl=True, on_var=False, hl_vol_usd=None,
                      var_vol_usd=None, hl_funding=None, hl_oi=None, var_funding_ann=None)
    sem = asyncio.Semaphore(2)
    us._COOLDOWN_SKIPS["n"] = 0
    asyncio.run(us._evaluate_one(vi, us.constants(), {}, None, advance_state=False, sem=sem))
    assert fetched["n"] == 2
    assert us._COOLDOWN_SKIPS["n"] == 0
