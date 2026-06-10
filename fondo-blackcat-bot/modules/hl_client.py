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
    """Async HL info POST through the shared limiter + TTL cache."""
    ttl = _TTL_SEC if ttl is None else float(ttl)
    key = _key(payload)
    ptype = str(payload.get("type") or "?")

    cached, _age = _cache_get(key, ttl)
    if cached is not None:
        return cached

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
        for attempt in range(_MAX_RETRIES):
            wait = _bucket.try_acquire()
            if wait > 0:
                await asyncio.sleep(wait + random.uniform(0, 0.05))
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    resp = await client.post(
                        INFO_URL, json=payload,
                        headers={"User-Agent": _UA, "Content-Type": "application/json"},
                    )
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_exc = RuntimeError(f"HTTP {resp.status_code}")
                    delay = _backoff_delay(attempt)
                    log.warning(
                        "hl_client %s -> %s (attempt %d/%d, retry in %.1fs)",
                        ptype, resp.status_code, attempt + 1, _MAX_RETRIES, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                resp.raise_for_status()
                data = resp.json()
                _cache_put(key, data)
                _stale_served.pop(ptype, None)
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
    """Sync mirror for urllib-based callers. Same cache + bucket."""
    ttl = _TTL_SEC if ttl is None else float(ttl)
    key = _key(payload)
    ptype = str(payload.get("type") or "?")

    cached, _age = _cache_get(key, ttl)
    if cached is not None:
        return cached

    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        wait = _bucket.try_acquire()
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.05))
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
            return data
        except urllib.error.HTTPError as exc:  # noqa: PERF203
            last_exc = exc
            if exc.code == 429 or exc.code >= 500:
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
