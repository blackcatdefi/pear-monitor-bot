"""
Market data aggregator.

Fuentes (todas free o con fallback):
- CoinGecko (precios, market cap, 24h/7d change, dominance)
- Fear & Greed Index (alternative.me)
- CoinGlass (funding, OI, liquidations, long/short) — opcional API key
- DefiLlama (TVL protocols, fees, stablecoins)
- HyperLiquid API (oil/gold/silver/equities si hay perps listados)

Cada función falla grácil → None o dict vacío. El reporte se genera igual.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from config import COINGLASS_API_KEY

log = logging.getLogger(__name__)

COINGECKO = "https://api.coingecko.com/api/v3"
FNG_URL = "https://api.alternative.me/fng/?limit=30"
LLAMA_PROTOCOLS = "https://api.llama.fi/v2/protocols"
LLAMA_FEES = "https://api.llama.fi/overview/fees"
LLAMA_STABLES = "https://stablecoins.llama.fi/stablecoins"
COINGLASS_BASE = "https://open-api-v4.coinglass.com/api"


async def _safe_get(client: httpx.AsyncClient, url: str, *, headers: dict | None = None,
                    params: dict | None = None, timeout: float = 15.0) -> Any:
    try:
        resp = await client.get(url, headers=headers, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:  # noqa: BLE001
        log.warning("GET %s failed: %s", url, e)
        return None


# ---------- CoinGecko ----------

async def fetch_coingecko_prices(client: httpx.AsyncClient) -> dict:
    ids = "bitcoin,ethereum,hyperliquid,solana,pax-gold,tether-gold"
    data = await _safe_get(
        client,
        f"{COINGECKO}/simple/price",
        params={
            "ids": ids,
            "vs_currencies": "usd",
            "include_24hr_change": "true",
            "include_7d_change": "true",
            "include_market_cap": "true",
        },
    )
    return data or {}


async def fetch_coingecko_global(client: httpx.AsyncClient) -> dict:
    data = await _safe_get(client, f"{COINGECKO}/global")
    return (data or {}).get("data") or {}


# ---------- Fear & Greed ----------

async def fetch_fear_greed(client: httpx.AsyncClient) -> dict:
    data = await _safe_get(client, FNG_URL)
    if not data or not data.get("data"):
        return {}
    points = data["data"]
    out = {"today": points[0]}
    if len(points) > 1:
        out["yesterday"] = points[1]
    if len(points) > 7:
        out["week_ago"] = points[7]
    if len(points) > 29:
        out["month_ago"] = points[29]
    return out


# ---------- CoinGlass (best effort) ----------

async def fetch_coinglass(client: httpx.AsyncClient) -> dict:
    """CoinGlass API v4. Si no hay API key o falla → dict vacío."""
    if not COINGLASS_API_KEY:
        return {}
    headers = {"CG-API-KEY": COINGLASS_API_KEY, "accept": "application/json"}
    out: dict[str, Any] = {}

    # Open interest BTC (ejemplo, la API real puede variar)
    oi = await _safe_get(
        client,
        f"{COINGLASS_BASE}/futures/open-interest/aggregated-history",
        headers=headers,
        params={"symbol": "BTC", "interval": "1d", "limit": 2},
    )
    out["btc_oi"] = oi

    # Liquidations 24h
    liq = await _safe_get(
        client,
        f"{COINGLASS_BASE}/futures/liquidation/aggregated-history",
        headers=headers,
        params={"symbol": "BTC", "interval": "1d", "limit": 2},
    )
    out["btc_liq"] = liq

    # Long/short ratio
    ls = await _safe_get(
        client,
        f"{COINGLASS_BASE}/futures/global-long-short-account-ratio/history",
        headers=headers,
        params={"symbol": "BTC", "interval": "1d", "limit": 2},
    )
    out["btc_long_short"] = ls

    # Funding rate
    fr = await _safe_get(
        client,
        f"{COINGLASS_BASE}/futures/funding-rate/oi-weight-history",
        headers=headers,
        params={"symbol": "BTC", "interval": "1d", "limit": 2},
    )
    out["btc_funding"] = fr

    return out


# ---------- DefiLlama ----------

async def fetch_llama_top_protocols(client: httpx.AsyncClient, limit: int = 10) -> list:
    data = await _safe_get(client, LLAMA_PROTOCOLS)
    if not data:
        return []
    # Orden por TVL y filtrar campos de interés
    ranked = sorted(data, key=lambda p: p.get("tvl") or 0, reverse=True)[:limit]
    return [
        {
            "name": p.get("name"),
            "symbol": p.get("symbol"),
            "tvl": p.get("tvl"),
            "chain": p.get("chain"),
            "change_1d": p.get("change_1d"),
            "change_7d": p.get("change_7d"),
        }
        for p in ranked
    ]


async def fetch_llama_fees(client: httpx.AsyncClient, limit: int = 10) -> list:
    data = await _safe_get(client, LLAMA_FEES)
    if not data or not data.get("protocols"):
        return []
    protos = sorted(
        data["protocols"], key=lambda p: p.get("total24h") or 0, reverse=True
    )[:limit]
    return [
        {
            "name": p.get("name"),
            "fees_24h": p.get("total24h"),
            "fees_7d": p.get("total7d"),
            "revenue_24h": p.get("dailyRevenue"),
        }
        for p in protos
    ]


async def fetch_llama_stables(client: httpx.AsyncClient) -> dict:
    data = await _safe_get(client, LLAMA_STABLES)
    if not data or not data.get("peggedAssets"):
        return {}
    total = sum((a.get("circulating", {}) or {}).get("peggedUSD", 0)
                for a in data["peggedAssets"])
    top = sorted(
        data["peggedAssets"],
        key=lambda a: (a.get("circulating", {}) or {}).get("peggedUSD", 0),
        reverse=True,
    )[:5]
    return {
        "total_usd": total,
        "top": [
            {
                "name": a.get("name"),
                "symbol": a.get("symbol"),
                "circulating": (a.get("circulating", {}) or {}).get("peggedUSD"),
            }
            for a in top
        ],
    }


# ---------- HyperLiquid equity perps (oil/gold/equities) ----------

async def fetch_hl_equity_prices(funding_ctx: dict | None) -> dict:
    """Extrae precios de commodities/equities del HyperLiquid metaAndAssetCtxs."""
    if not funding_ctx:
        return {}
    targets = ["BRENT", "OIL", "GOLD", "XAU", "SILVER", "XAG",
               "SPY", "USA500", "TSLA", "NVDA", "HOOD", "HYPE", "BTC", "ETH"]
    out = {}
    for coin in targets:
        if coin in funding_ctx:
            ctx = funding_ctx[coin]
            prev = ctx.get("prev_day_px") or 0
            mark = ctx.get("mark_px") or 0
            change = ((mark - prev) / prev * 100) if prev else None
            out[coin] = {
                "price": mark,
                "change_24h_pct": change,
                "funding": ctx.get("funding"),
                "open_interest": ctx.get("open_interest"),
            }
    return out


# ---------- Aggregator ----------

async def fetch_market_data(funding_ctx: dict | None = None) -> dict:
    """Snapshot completo de mercado. Cada sub-fuente falla grácil."""
    async with httpx.AsyncClient() as client:
        prices, global_data, fng, coinglass, protocols, fees, stables = await asyncio.gather(
            fetch_coingecko_prices(client),
            fetch_coingecko_global(client),
            fetch_fear_greed(client),
            fetch_coinglass(client),
            fetch_llama_top_protocols(client),
            fetch_llama_fees(client),
            fetch_llama_stables(client),
        )

    hl_equity = await fetch_hl_equity_prices(funding_ctx)

    return {
        "coingecko_prices": prices,
        "coingecko_global": global_data,
        "fear_greed": fng,
        "coinglass": coinglass,
        "top_protocols_tvl": protocols,
        "top_protocols_fees": fees,
        "stablecoins": stables,
        "hl_equity": hl_equity,
    }


def format_market_summary(market: dict) -> str:
    """Formato corto para reporte rápido (no el LLM)."""
    lines = ["📈 *MARKET SNAPSHOT*"]
    cg = market.get("coingecko_prices") or {}
    btc = cg.get("bitcoin") or {}
    eth = cg.get("ethereum") or {}
    hype = cg.get("hyperliquid") or {}
    if btc:
        lines.append(
            f"BTC: ${btc.get('usd', 0):,.0f} "
            f"({btc.get('usd_24h_change', 0):+.2f}% 24h)"
        )
    if eth:
        lines.append(
            f"ETH: ${eth.get('usd', 0):,.0f} "
            f"({eth.get('usd_24h_change', 0):+.2f}% 24h)"
        )
    if hype:
        lines.append(
            f"HYPE: ${hype.get('usd', 0):.2f} "
            f"({hype.get('usd_24h_change', 0):+.2f}% 24h)"
        )

    fng = market.get("fear_greed") or {}
    if fng.get("today"):
        t = fng["today"]
        lines.append(f"F&G: {t.get('value')} ({t.get('value_classification')})")

    gl = market.get("coingecko_global") or {}
    if gl:
        lines.append(f"BTC dominance: {gl.get('market_cap_percentage', {}).get('btc', 0):.2f}%")

    return "\n".join(lines)
