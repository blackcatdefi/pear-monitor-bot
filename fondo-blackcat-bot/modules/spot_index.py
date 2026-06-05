"""HyperLiquid spot-index → ticker resolver.

HL spot orders, balances and fills reference non-canonical pairs by an
opaque index name like ``@107`` instead of the human ticker. The pair
``@107`` is ``tokens=[150, 0]`` in ``spotMeta`` → base token index 150 →
token name ``HYPE``. Without this map the report renders the 4 HYPE spot
limit buys (46/50/55/60) as the unidentified string ``@107``
("coin no identificada").

This module builds and caches a ``pair-name → base-ticker`` map from the
``spotMeta`` info endpoint and exposes a pure resolver so every spot order,
balance and fill resolves by name. Canonical pairs (``PURR/USDC``) and
plain perp tickers (``BTC``, ``HYPE``) pass through unchanged.

R-AUDIT-P0 (2026-06-04).
"""
from __future__ import annotations

import logging
import time
from typing import Any

from config import HYPERLIQUID_API, WALLET_FETCH_TIMEOUT
from utils.http import post_json

log = logging.getLogger(__name__)

INFO_URL = f"{HYPERLIQUID_API}/info"

# Refresh cadence: spotMeta changes rarely (new listings), so a 1h TTL is
# ample and keeps the report path latency-free after the first warm-up.
_TTL_SECS = 3600
_cache: dict[str, Any] = {"map": {}, "ts": 0.0}


def build_spot_index_map(spot_meta: dict[str, Any] | None) -> dict[str, str]:
    """Build ``pair-name → base-ticker`` from a raw ``spotMeta`` payload.

    ``spotMeta`` = ``{"tokens": [{"index": int, "name": str}, ...],
    "universe": [{"name": "@107", "tokens": [baseIdx, quoteIdx]}, ...]}``.
    The base ticker is the name of the token at ``tokens[0]``. Never raises:
    malformed entries are skipped.
    """
    if not isinstance(spot_meta, dict):
        return {}
    idx_to_name: dict[int, str] = {}
    tokens = spot_meta.get("tokens")
    universe = spot_meta.get("universe")
    if not isinstance(tokens, list):
        tokens = []
    if not isinstance(universe, list):
        universe = []
    for t in tokens:
        if not isinstance(t, dict):
            continue
        idx = t.get("index")
        name = t.get("name")
        if isinstance(idx, int) and name:
            idx_to_name[idx] = str(name)
    out: dict[str, str] = {}
    for u in universe:
        if not isinstance(u, dict):
            continue
        name = u.get("name")
        toks = u.get("tokens") or []
        if not name or not isinstance(toks, list) or not toks:
            continue
        base_idx = toks[0]
        base_name = idx_to_name.get(base_idx) if isinstance(base_idx, int) else None
        if base_name:
            out[str(name)] = base_name
    return out


def resolve_spot_coin(coin: Any, mapping: dict[str, str] | None = None) -> str:
    """Resolve a coin field to its human ticker.

    ``@107`` → ``HYPE`` via the map; canonical pairs and plain tickers
    (``BTC``, ``HYPE``, ``USDC``) return unchanged. ``mapping=None`` uses the
    module cache. An unknown ``@N`` that the map can't resolve is returned
    verbatim (never crashes, never invents a name).
    """
    if coin is None:
        return "?"
    s = str(coin)
    m = mapping if mapping is not None else _cache.get("map") or {}
    if s in m:
        return m[s]
    return s


async def refresh_spot_index_map(force: bool = False) -> dict[str, str]:
    """Fetch ``spotMeta`` and refresh the cached map. Returns the map.

    Honours the TTL unless ``force``. On any failure keeps the last-known
    map (degrades gracefully — a stale map is still better than raw ``@N``).
    """
    now = time.time()
    if not force and _cache["map"] and (now - _cache["ts"]) < _TTL_SECS:
        return _cache["map"]
    try:
        spot_meta = await post_json(
            INFO_URL, {"type": "spotMeta"}, timeout=WALLET_FETCH_TIMEOUT
        )
        new_map = build_spot_index_map(spot_meta)
        if new_map:
            _cache["map"] = new_map
            _cache["ts"] = now
            log.info("spot_index: refreshed map (%d pairs)", len(new_map))
    except Exception as exc:  # noqa: BLE001
        log.warning("spot_index: refresh failed (%s) — keeping last map", exc)
    return _cache["map"]


async def ensure_spot_index_map() -> dict[str, str]:
    """Ensure the cached map is fresh enough; refresh if stale/empty."""
    now = time.time()
    if _cache["map"] and (now - _cache["ts"]) < _TTL_SECS:
        return _cache["map"]
    return await refresh_spot_index_map(force=False)
