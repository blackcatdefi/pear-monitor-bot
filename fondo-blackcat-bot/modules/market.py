"""Market data aggregator: CoinGecko, Fear&Greed, CoinGlass, DefiLlama.

Round 7 additions:
    - Integrates `modules.coinglass.get_basket_oi_funding()` for the fund's
      basket (BTC/ETH/HYPE + SHORT basket WLD/STRK/AVAX/ZRO/ENA).
    - Exposes `coinglass_basket` key in fetch_market_data() so the raw-data
      payload to the LLM carries OI + funding across the basket.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from config import COINGLASS_API_KEY
from utils.http import get_json
from modules.coinglass import get_basket_oi_funding

log = logging.getLogger(__name__)

# ─── In-memory cache (TTL seconds) ───────────────────────────────────────────
_CACHE: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str, ttl: float) -> Any | None:
    item = _CACHE.get(key)
    if not item:
        return None
    ts, value = item
    if time.time() - ts > ttl:
        return None
    return value


def _cache_set(key: str, value: Any) -> None:
    _CACHE[key] = (time.time(), value)


# ─── CoinGecko ──────────────────────────────────────────────────────────────
COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
COINGECKO_GLOBAL_URL = "https://api.coingecko.com/api/v3/global"

_GECKO_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "HYPE": "hyperliquid",
    "GOLD": "pax-gold",
    "SILVER": "kinesis-silver",
    "PAXG": "pax-gold",
}


async def coingecko_prices() -> dict[str, Any]:
    cached = _cache_get("gecko_prices", 60)
    if cached is not None:
        return cached
    ids = ",".join(sorted(set(_GECKO_IDS.values())))
    params = {
        "ids": ids,
        "vs_currencies": "usd",
        "include_24hr_change": "true",
        "include_market_cap": "true",
    }
    try:
        data = await get_json(COINGECKO_PRICE_URL, params=params)
        out: dict[str, Any] = {}
        for sym, gid in _GECKO_IDS.items():
            entry = data.get(gid, {})
            if entry:
                out[sym] = {
                    "price_usd": entry.get("usd"),
                    "change_24h": entry.get("usd_24h_change"),
                    "market_cap": entry.get("usd_market_cap"),
                }
        _cache_set("gecko_prices", out)
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("CoinGecko prices failed: %s", exc)
        return {}


async def coingecko_global() -> dict[str, Any]:
    cached = _cache_get("gecko_global", 300)
    if cached is not None:
        return cached
    try:
        data = await get_json(COINGECKO_GLOBAL_URL)
        d = (data or {}).get("data", {}) or {}
        out = {
            "total_market_cap_usd": (d.get("total_market_cap") or {}).get("usd"),
            "total_volume_usd": (d.get("total_volume") or {}).get("usd"),
            "btc_dominance": (d.get("market_cap_percentage") or {}).get("btc"),
            "eth_dominance": (d.get("market_cap_percentage") or {}).get("eth"),
            "market_cap_change_24h": d.get("market_cap_change_percentage_24h_usd"),
        }
        _cache_set("gecko_global", out)
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("CoinGecko global failed: %s", exc)
        return {}


# ─── Fear & Greed ───────────────────────────────────────────────────────────
FNG_URL = "https://api.alternative.me/fng/?limit=30"


async def fear_greed() -> dict[str, Any]:
    cached = _cache_get("fng", 600)
    if cached is not None:
        return cached
    try:
        data = await get_json(FNG_URL)
        items = (data or {}).get("data", []) or []
        if not items:
            return {}
        latest = items[0]
        result = {
            "value": int(latest.get("value", 0)),
            "classification": latest.get("value_classification"),
            "timestamp": latest.get("timestamp"),
            "yesterday": int(items[1]["value"]) if len(items) > 1 else None,
            "week_ago": int(items[7]["value"]) if len(items) > 7 else None,
            "month_ago": int(items[29]["value"]) if len(items) > 29 else None,
        }
        _cache_set("fng", result)
        return result
    except Exception as exc:  # noqa: BLE001
        log.warning("Fear&Greed failed: %s", exc)
        return {}


# ─── CoinGlass (v3 legacy endpoints, BTC-only) ──────────────────────────────
COINGLASS_BASE = "https://open-api-v3.coinglass.com/api"


async def _coinglass_get(path: str, params: dict[str, Any] | None = None) -> Any:
    if not COINGLASS_API_KEY:
        return None
    headers = {"CG-API-KEY": COINGLASS_API_KEY, "accept": "application/json"}
    try:
        return await get_json(f"{COINGLASS_BASE}{path}", headers=headers, params=params or {})
    except Exception as exc:  # noqa: BLE001
        log.warning("CoinGlass %s failed: %s", path, exc)
        return None


async def coinglass_metrics() -> dict[str, Any]:
    """Best-effort CoinGlass aggregate (OI, liqs, funding, long/short)."""
    cached = _cache_get("coinglass", 300)
    if cached is not None:
        return cached
    if not COINGLASS_API_KEY:
        return {"available": False, "reason": "no_api_key"}
    out: dict[str, Any] = {"available": True}
    try:
        oi = await _coinglass_get("/futures/openInterest", {"symbol": "BTC"})
        liqs = await _coinglass_get("/futures/liquidation/v2/history", {"symbol": "BTC", "interval": "h1"})
        funding = await _coinglass_get("/futures/fundingRate", {"symbol": "BTC"})
        ls = await _coinglass_get("/futures/longShortRatio", {"symbol": "BTC", "interval": "h4"})
        out.update({
            "open_interest": oi,
            "liquidations_24h": liqs,
            "funding_btc": funding,
            "long_short_ratio_btc": ls,
        })
        _cache_set("coinglass", out)
    except Exception as exc:  # noqa: BLE001
        log.warning("CoinGlass aggregate failed: %s", exc)
        out["error"] = str(exc)
    return out


async def coinglass_basket() -> dict[str, Any]:
    """Round 7: OI + funding across the fund's basket via Coinglass v4."""
    cached = _cache_get("coinglass_basket", 300)
    if cached is not None:
        return cached
    try:
        result = await get_basket_oi_funding()
        _cache_set("coinglass_basket", result)
        return result
    except Exception as exc:  # noqa: BLE001
        log.warning("coinglass_basket failed: %s", exc)
        return {"available": False, "reason": str(exc), "basket": []}


# ─── DefiLlama ──────────────────────────────────────────────────────────────
LLAMA_PROTOCOLS = "https://api.llama.fi/protocols"
LLAMA_FEES = "https://api.llama.fi/overview/fees?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true"
LLAMA_STABLES = "https://stablecoins.llama.fi/stablecoins?includePrices=true"


async def defillama_top_fees(top_n: int = 10) -> list[dict[str, Any]]:
    cached = _cache_get(f"llama_fees_{top_n}", 1800)
    if cached is not None:
        return cached
    try:
        data = await get_json(LLAMA_FEES)
        protos = (data or {}).get("protocols", []) or []
        protos = sorted(protos, key=lambda p: p.get("total24h") or 0, reverse=True)[:top_n]
        out = [
            {
                "name": p.get("name"),
                "category": p.get("category"),
                "fees_24h": p.get("total24h"),
                "fees_7d": p.get("total7d"),
                "revenue_24h": p.get("revenue24h"),
            }
            for p in protos
        ]
        _cache_set(f"llama_fees_{top_n}", out)
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("DefiLlama fees failed: %s", exc)
        return []


async def defillama_stablecoin_supply() -> dict[str, Any]:
    cached = _cache_get("llama_stables", 1800)
    if cached is not None:
        return cached
    try:
        data = await get_json(LLAMA_STABLES)
        coins = (data or {}).get("peggedAssets", []) or []
        total = 0.0
        usdt = usdc = 0.0
        for c in coins:
            circ = (c.get("circulating") or {}).get("peggedUSD") or 0
            total += circ
            sym = (c.get("symbol") or "").upper()
            if sym == "USDT":
                usdt = circ
            elif sym == "USDC":
                usdc = circ
        out = {"total_supply_usd": total, "usdt": usdt, "usdc": usdc}
        _cache_set("llama_stables", out)
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("DefiLlama stables failed: %s", exc)
        return {}


# ─── Aggregate ──────────────────────────────────────────────────────────────
async def fetch_market_data() -> dict[str, Any]:
    prices, glob, fng, cg, cg_basket, fees, stables = await asyncio.gather(
        coingecko_prices(),
        coingecko_global(),
        fear_greed(),
        coinglass_metrics(),
        coinglass_basket(),
        defillama_top_fees(),
        defillama_stablecoin_supply(),
    )
    return {
        "status": "ok",
        "data": {
            "prices": prices,
            "global": glob,
            "fear_greed": fng,
            "coinglass": cg,
            "coinglass_basket": cg_basket,
            "top_fees": fees,
            "stablecoins": stables,
        },
    }
