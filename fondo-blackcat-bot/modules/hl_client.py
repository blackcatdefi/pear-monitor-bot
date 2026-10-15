"""R-BOT-DEFINITIVE WI-4 — shared HyperLiquid info-API client.

Live bug it kills (2026-06-10): 429s on ``perpDexs`` / ``predictedFundings``
during /reporte → per-position funding printed n/d, OI/funding n/d in the
same run. Each module opened its own client with no shared rate limiting and
no cache, so one /reporte hit the same endpoints repeatedly.

Design (one client, every HL info call routed through it):
  * Token-bucket rate limiter (default 5 req/s, burst 10) shared process-wide.
  * Exponential backoff WITH JITTER on 429/5xx (the old fixed backoff caused
    synchronized retries).
  * TTL cache (default 90s, env ``HL_CLIENT_TTL_SEC`` 60-120) keyed by the
    canonical payload JSON → a single /reporte never issues the same request
    twice (in-run dedupe included: concurrent identical calls share one
    in-flight request).
  * Stale-serve: when a fetch ultimately fails but an EXPIRED cache entry
    exists, serve it and record its age so renderers can label
    "cached Xs ago" instead of n/d (``stale_note``).
  * Sync mirror (``post_info_sync``) for the urllib-based modules
    (hl_prices, hl_borrow_lend, vault_deposits) sharing the SAME cache.

NEVER raises from the stale-serving path; raises only when there is neither
fresh data nor any cached value (callers keep their own degrade logic).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import threading
import time
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

try:
    from config import HYPERLIQUID_API
except Exception:  # noqa: BLE001
    HYPERLIQUID_API = "https://api.hyperliquid.xyz"

INFO_URL = f"{HYPERLIQUID_API}/info"

_TTL_SEC = float(os.getenv("HL_CLIENT_TTL_SEC", "90") or 90)
_RATE_PER_SEC = float(os.getenv("HL_CLIENT_RATE_PER_SEC", "5") or 5)
_BURST = float(os.getenv("HL_CLIENT_BURST", "10") or 10)
_MAX_RETRIES = int(os.getenv("HL_CLIENT_MAX_RETRIES", "4") or 4)
_TIMEOUT = float(os.getenv("HL_CLIENT_TIMEOUT", "15") or 15)

# ─── R-RONDA-OCT #2: the candleSnapshot 429/500 storm ───────────────────────
# Root cause, in three parts, all visible in the 2026-10-15 13:50 UTC logs:
#   (a) HL meters /info by WEIGHT, not by request: 1200 weight/min per IP.
#       ``candleSnapshot`` (like most documented types) weighs 20, so the
#       ceiling is ~60 candle calls a minute. The universal screener sweeps
#       ~180 names at 5 req/s: a 429 was guaranteed by arithmetic, and after
#       the 429s HL answered 500 for the same type for a while.
#   (b) every candle payload carried ``endTime=now_ms``, so the 90s TTL cache
#       NEVER hit — the unlock monitor and the screener fetched the same
#       4h series twice per 30-min job (compute_snapshot + advance_universe
#       _state), and /reporte a third time.
#   (c) a 429 was retried 4 times INSIDE the call with sub-16s backoff, and
#       the screener's own per-asset retry loop sat on top of that, so one
#       throttled minute produced hundreds of attempts and nothing persisted
#       between calls. Same bug class as the CoinGecko storm of R-LIVE-FIX:
#       a backoff that forgets itself when the call returns is a pause.
# Fix: a WEIGHT budget per minute shared by both paths; a per-HOST cooldown
# on 429 (shared with utils.http: Retry-After, else exponential with a cap)
# during which NO request touches the network (stale cache or
# HostCoolingDown); a per-TYPE cooldown after a 5xx storm; zero in-call
# retries on 429; and a shared candle cache keyed on (coin, interval) that
# serves any shorter window from the longest series fetched.
_WEIGHT_PER_MIN = float(os.getenv("HL_CLIENT_WEIGHT_PER_MIN", "1000") or 1000)
# HL docs: weight 2 for l2Book/allMids/clearinghouseState/orderStatus/
# spotClearinghouseState/exchangeStatus; 60 for userRole; 20 for the rest.
_WEIGHT_LIGHT = {
    "l2Book", "allMids", "clearinghouseState", "orderStatus",
    "spotClearinghouseState", "exchangeStatus",
}
_WEIGHT_HEAVY = {"userRole": 60}
_COOLDOWN_BASE_SEC = float(os.getenv("HL_CLIENT_COOLDOWN_BASE_SEC", "30") or 30)
_COOLDOWN_MAX_SEC = float(os.getenv("HL_CLIENT_COOLDOWN_MAX_SEC", "300") or 300)
_TYPE_COOLDOWN_SEC = float(os.getenv("HL_CLIENT_TYPE_COOLDOWN_SEC", "120") or 120)
_MAX_RETRIES_5XX = int(os.getenv("HL_CLIENT_MAX_RETRIES_5XX", "2") or 2)
_CANDLE_TTL_SEC = float(os.getenv("HL_CANDLE_CACHE_TTL_SEC", "600") or 600)
_INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000, "3d": 259_200_000,
    "1w": 604_800_000, "1M": 2_592_000_000,
}
# {payload_type: epoch_until} — a 5xx storm on ONE type must not blind the
# whole host (the portfolio reads must keep flowing).
_TYPE_COOLDOWN: dict[str, float] = {}

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Cache: {key: (fetched_at_epoch, value)} — shared by sync + async paths.
_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()
# Stale-serve registry: {payload_type: age_seconds_at_serve_time}.
_stale_served: dict[str, float] = {}
# In-flight dedupe for the async path: {key: asyncio.Future}.
_inflight: dict[str, "asyncio.Future"] = {}


class _TokenBucket:
    """Thread-safe token bucket (used by both sync and async paths)."""

    def __init__(self, rate: float, burst: float) -> None:
        self.rate = max(rate, 0.1)
        self.capacity = max(burst, 1.0)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def try_acquire(self) -> float:
        """Take a token if available; else return seconds to wait (>0)."""
        with self._lock:
            self._refill()
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return 0.0
            return (1.0 - self.tokens) / self.rate


_bucket = _TokenBucket(_RATE_PER_SEC, _BURST)


class _WeightBucket:
    """Weight budget per minute (HL's real meter). Thread-safe."""

    def __init__(self, per_min: float) -> None:
        self.capacity = max(per_min, 20.0)
        self.rate = self.capacity / 60.0
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def try_acquire(self, weight: float) -> float:
        """Take ``weight`` if available; else return seconds to wait (>0)."""
        with self._lock:
            self._refill()
            if self.tokens >= weight:
                self.tokens -= weight
                return 0.0
            return (weight - self.tokens) / self.rate


_weight_bucket = _WeightBucket(_WEIGHT_PER_MIN)


def request_weight(payload_type: str) -> int:
    if payload_type in _WEIGHT_LIGHT:
        return 2
    return int(_WEIGHT_HEAVY.get(payload_type, 20))


def _acquire_budget(payload_type: str) -> float:
    """Seconds to wait before this request fits BOTH the req/s bucket and the
    weight/min budget (0 when it fits now)."""
    w = _bucket.try_acquire()
    if w > 0:
        return w
    return _weight_bucket.try_acquire(request_weight(payload_type))


class _Resp429:
    """Adapter so utils.http can read Retry-After from either transport."""

    def __init__(self, status_code: int, headers: Any) -> None:
        self.status_code = status_code
        self.headers = headers or {}


class _Exc429(Exception):
    def __init__(self, status_code: int, headers: Any) -> None:
        super().__init__(f"HTTP {status_code}")
        self.response = _Resp429(status_code, headers)


def _note_host_429(headers: Any) -> float:
    """Register a 429 against the HL host in the SHARED per-host cooldown
    (utils.http) and return the penalty applied. HL base/cap are shorter than
    CoinGecko's because HL's window is one minute."""
    from utils import http as _uh
    penalty = _uh._note_429(INFO_URL, _Exc429(429, headers))
    # utils.http applies CoinGecko's 60s·2^n/900s; HL's window is 60s, so
    # re-clamp to HL's own base/cap unless the server asked for more.
    st = _uh._HOST_COOLDOWN.get(_uh._host_of(INFO_URL))
    if st is not None:
        strikes = int(st.get("strikes", 1))
        asked = _uh._retry_after_sec(_Exc429(429, headers))
        if asked is None or asked <= 0:
            penalty = min(_COOLDOWN_BASE_SEC * (2 ** max(0, strikes - 1)), _COOLDOWN_MAX_SEC)
            st["until"] = time.time() + penalty
    log.warning("hl_client: 429 -> host cooldown %.0fs (no network until then)", penalty)
    return penalty


def _note_host_ok() -> None:
    from utils import http as _uh
    _uh._note_success(INFO_URL)


def cooldown_remaining(payload_type: str | None = None) -> float:
    """Seconds before the HL host (and ``payload_type``, if given) may be hit
    again. 0 when free."""
    from utils import http as _uh
    rem = _uh.cooldown_remaining(INFO_URL)
    if payload_type:
        rem = max(rem, _TYPE_COOLDOWN.get(payload_type, 0.0) - time.time())
    return max(0.0, rem)


def cooling_down(payload_type: str | None = None) -> bool:
    return cooldown_remaining(payload_type) > 0


def _note_type_5xx(payload_type: str) -> None:
    _TYPE_COOLDOWN[payload_type] = time.time() + _TYPE_COOLDOWN_SEC
    log.warning("hl_client: %s -> 5xx storm, type cooldown %.0fs",
                payload_type, _TYPE_COOLDOWN_SEC)


def _cooldown_error(payload_type: str) -> Exception:
    from utils.http import HostCoolingDown, cooldown_strikes
    return HostCoolingDown(
        f"api.hyperliquid.xyz/{payload_type}", cooldown_remaining(payload_type),
        cooldown_strikes(INFO_URL))


def reset_cooldowns() -> None:
    """Tests only."""
    _TYPE_COOLDOWN.clear()
    try:
        from utils import http as _uh
        _uh.reset_cooldowns()
    except Exception:  # noqa: BLE001 — tests only
        pass


def _key(payload: dict[str, Any]) -> str:
    try:
        return json.dumps(payload, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        return repr(payload)


def _cache_get(key: str, ttl: float) -> tuple[Any | None, float | None]:
    """Return (value, age) if cached within ttl, else (None, age_or_None)."""
    with _cache_lock:
        ent = _cache.get(key)
    if ent is None:
        return None, None
    age = time.time() - ent[0]
    if age <= ttl:
        return ent[1], age
    return None, age


def _cache_put(key: str, value: Any) -> None:
    with _cache_lock:
        _cache[key] = (time.time(), value)
        # Bound memory: drop the oldest entries past 600.
        if len(_cache) > 600:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
                _cache.pop(k, None)


def _serve_stale(key: str, payload_type: str) -> Any | None:
    with _cache_lock:
        ent = _cache.get(key)
    if ent is None:
        return None
    age = time.time() - ent[0]
    _stale_served[payload_type] = age
    log.warning("hl_client: serving STALE cache for %s (age %.0fs)", payload_type, age)
    return ent[1]


def stale_note(payload_type: str) -> str:
    """'cached Xs ago' label when the last serve for this type was stale."""
    age = _stale_served.get(payload_type)
    if age is None:
        return ""
    return f"cached {int(age)}s ago"


def clear_stale_notes() -> None:
    _stale_served.clear()


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff with full jitter."""
    base = min(2.0 ** attempt, 16.0)
    return random.uniform(0.5, base)


async def post_info(payload: dict[str, Any], *, ttl: float | None = None) -> Any:
    """Async HL info POST through the shared limiter + TTL cache.

    Cooldown doctrine (R-RONDA-OCT #2): while the host is cooling down after a
    429, or the payload type after a 5xx storm, NOTHING touches the network —
    an expired cache entry is served as stale, otherwise ``HostCoolingDown``
    is raised at once. A 429 is never retried inside the call.
    """
    ttl = _TTL_SEC if ttl is None else float(ttl)
    key = _key(payload)
    ptype = str(payload.get("type") or "?")

    cached, _age = _cache_get(key, ttl)
    if cached is not None:
        return cached

    if cooling_down(ptype):
        stale = _serve_stale(key, ptype)
        if stale is not None:
            return stale
        raise _cooldown_error(ptype)

    # In-flight dedupe: identical concurrent calls share one request.
    loop = asyncio.get_event_loop()
    existing = _inflight.get(key)
    if existing is not None and not existing.done():
        return await asyncio.shield(existing)
    fut: asyncio.Future = loop.create_future()
    _inflight[key] = fut

    try:
        import httpx
        last_exc: Exception | None = None
        attempts_5xx = 0
        for attempt in range(_MAX_RETRIES):
            wait = _acquire_budget(ptype)
            if wait > 0:
                await asyncio.sleep(wait + random.uniform(0, 0.05))
            if cooling_down(ptype):
                break
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    resp = await client.post(
                        INFO_URL, json=payload,
                        headers={"User-Agent": _UA, "Content-Type": "application/json"},
                    )
                if resp.status_code == 429:
                    last_exc = RuntimeError("HTTP 429")
                    _note_host_429(getattr(resp, "headers", None))
                    break
                if resp.status_code >= 500:
                    last_exc = RuntimeError(f"HTTP {resp.status_code}")
                    attempts_5xx += 1
                    if attempts_5xx >= _MAX_RETRIES_5XX:
                        _note_type_5xx(ptype)
                        break
                    delay = _backoff_delay(attempt)
                    log.warning(
                        "hl_client %s -> %s (attempt %d/%d, retry in %.1fs)",
                        ptype, resp.status_code, attempts_5xx, _MAX_RETRIES_5XX, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                resp.raise_for_status()
                data = resp.json()
                _cache_put(key, data)
                _stale_served.pop(ptype, None)
                _note_host_ok()
                _TYPE_COOLDOWN.pop(ptype, None)
                if not fut.done():
                    fut.set_result(data)
                return data
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(_backoff_delay(attempt))
        stale = _serve_stale(key, ptype)
        if stale is not None:
            if not fut.done():
                fut.set_result(stale)
            return stale
        err = last_exc or RuntimeError("hl_client: fetch failed")
        if not fut.done():
            fut.set_exception(err)
            # Consume the exception so the future never warns if unawaited.
            fut.exception()
        raise err
    finally:
        if _inflight.get(key) is fut:
            _inflight.pop(key, None)


def post_info_sync(payload: dict[str, Any], *, ttl: float | None = None) -> Any:
    """Sync mirror for urllib-based callers. Same cache + bucket + cooldown."""
    ttl = _TTL_SEC if ttl is None else float(ttl)
    key = _key(payload)
    ptype = str(payload.get("type") or "?")

    cached, _age = _cache_get(key, ttl)
    if cached is not None:
        return cached

    if cooling_down(ptype):
        stale = _serve_stale(key, ptype)
        if stale is not None:
            return stale
        raise _cooldown_error(ptype)

    last_exc: Exception | None = None
    attempts_5xx = 0
    for attempt in range(_MAX_RETRIES):
        wait = _acquire_budget(ptype)
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.05))
        if cooling_down(ptype):
            break
        try:
            body = json.dumps(payload).encode()
            req = urllib.request.Request(
                INFO_URL, data=body, method="POST",
                headers={"Content-Type": "application/json", "User-Agent": _UA},
            )
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
                data = json.load(r)
            _cache_put(key, data)
            _stale_served.pop(ptype, None)
            _note_host_ok()
            _TYPE_COOLDOWN.pop(ptype, None)
            return data
        except urllib.error.HTTPError as exc:  # noqa: PERF203
            last_exc = exc
            if exc.code == 429:
                _note_host_429(getattr(exc, "headers", None))
                break
            if exc.code >= 500:
                attempts_5xx += 1
                if attempts_5xx >= _MAX_RETRIES_5XX:
                    _note_type_5xx(ptype)
                    break
                time.sleep(_backoff_delay(attempt))
                continue
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < _MAX_RETRIES - 1:
                time.sleep(_backoff_delay(attempt))
    stale = _serve_stale(key, ptype)
    if stale is not None:
        return stale
    raise last_exc or RuntimeError("hl_client: sync fetch failed")


# ─── R-RONDA-OCT #2: shared candle cache ────────────────────────────────────
# {(COIN, interval): (fetched_at, [candles oldest→newest])}. Any request for
# ``bars`` ≤ len(series) within the TTL is a slice of the cached series; a
# longer request refetches and REPLACES the entry, so the two sweeps of the
# 30-min job (watchlist 184 bars, universe 48 bars) and /reporte share one
# fetch per name.
_candle_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_candle_lock = threading.Lock()


def candle_cache_stats() -> dict[str, Any]:
    with _candle_lock:
        return {"series": len(_candle_cache),
                "ttl_sec": _CANDLE_TTL_SEC,
                "host_cooldown_sec": round(cooldown_remaining(), 1),
                "type_cooldown_sec": round(cooldown_remaining("candleSnapshot"), 1)}


def clear_candle_cache() -> None:
    """Tests only."""
    with _candle_lock:
        _candle_cache.clear()


async def get_candles(coin: str, interval: str, bars: int, *,
                      ttl: float | None = None) -> list[dict[str, Any]]:
    """Last ``bars`` candles (oldest→newest) for ``coin``/``interval`` through
    the shared candle cache. Raises on failure (callers keep their own
    degrade logic — a cooldown raises ``HostCoolingDown`` without network).
    """
    coin = (coin or "").strip().upper()
    bars = max(1, int(bars))
    ttl = _CANDLE_TTL_SEC if ttl is None else float(ttl)
    ck = (coin, interval)
    now = time.time()
    with _candle_lock:
        ent = _candle_cache.get(ck)
    if ent is not None and (now - ent[0]) <= ttl and len(ent[1]) >= bars:
        return list(ent[1][-bars:])

    step = _INTERVAL_MS.get(interval)
    if not step:
        raise ValueError(f"hl_client.get_candles: intervalo desconocido {interval!r}")
    # Fetch at least what is already cached so a short request never shrinks
    # the series another caller relies on.
    want = max(bars, len(ent[1]) if ent is not None else 0)
    # endTime bucketed to the minute so concurrent callers share the
    # in-flight request and the payload cache (endTime=now_ms never hit).
    end_ms = (int(now * 1000) // 60_000) * 60_000 + 60_000
    start_ms = end_ms - (want + 2) * step
    data = await post_info({
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": interval,
                "startTime": start_ms, "endTime": end_ms},
    }, ttl=ttl)
    if not isinstance(data, list):
        raise ValueError(f"hl_client.get_candles: shape inesperado para {coin} {interval}")
    series = [c for c in data if isinstance(c, dict)]
    with _candle_lock:
        _candle_cache[ck] = (time.time(), series)
        if len(_candle_cache) > 600:
            for k in sorted(_candle_cache, key=lambda k: _candle_cache[k][0])[:100]:
                _candle_cache.pop(k, None)
    return list(series[-bars:])
