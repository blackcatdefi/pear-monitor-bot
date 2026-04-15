"""Market data aggregator.

Sources used (all optional — each wrapped in try/except so the report keeps
building even if one API is down):
  - alternative.me                 → Fear & Greed
  - api.coingecko.com              → prices, global market cap, dominance
  - api.llama.fi                   → TVL per protocol, fees, stablecoins
  - open-api.coinglass.com         → OI, liquidations, funding, L/S ratio
  - HyperLiquid /info (allMids)    → perp prices for HYPE/NVDA/TSLA/etc.

Returns a flat dict with best-effort data. Missing fields are set to None.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from config import COINGLASS_API_KEY, HYPERLIQUID_RPC

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(15.0, connect=10.0)
CG_BASE = "https://api.coingecko.com/api/v3"
LLAMA_BASE = "https://api.llama.fi"
FNG_URL = "https://api.alternative.me/fng/"
CG_IDS = "bitcoin,ethereum,hyperliquid,solana,pax-gold,tether-gold"


async def _safe_get_json(client: httpx.AsyncClient, url: str, **kwargs) -> Any:
    try:
        r = await client.get(url, timeout=TIMEOUT, **kwargs)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("GET %s failed: %s", url, e)
        return None


async def _safe_post_json(client: httpx.AsyncClient, url: str, **kwargs) -> Any:
    try:
        r = await client.post(url, timeout=TIMEOUT, **kwargs)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("POST %s failed: %s", url, e)
        return None


async def fetch_fear_greed(client: httpx.AsyncClient, limit: int = 30) -> dict[str, Any]:
    data = await _safe_get_json(client, FNG_URL, params={"limit": limit})
    out: dict[str, Any] = {"current": None, "classification": None, "yesterday": None, "week": None, "month": None}
    if not data or "data" not in data:
        return out
    rows = data["data"]
    try:
        def _v(i):
            return int(rows[i]["value"]) if i < len(rows) else None
        out["current"] = _v(0)
        out["classification"] = rows[0].get("value_classification")
        out["yesterday"] = _v(1)
        out["week"] = _v(7)
        out["month"] = _v(29)
    except (TypeError, ValueError, KeyError):
        pass
    return out


async def fetch_coingecko(client: httpx.AsyncClient) -> dict[str, Any]:
    prices = await _safe_get_json(
        client,
        f"{CG_BASE}/simple/price",
        params={
            "ids": CG_IDS,
            "vs_currencies": "usd",
            "include_24hr_change": "true",
            "include_market_cap": "true",
        },
    )
    global_data = await _safe_get_json(client, f"{CG_BASE}/global")
    out: dict[str, Any] = {"prices": prices or {}, "global": None}
    if global_data and isinstance(global_data, dict):
        g = global_data.get("data") or {}
        out["global"] = {
            "total_mcap_usd": (g.get("total_market_cap") or {}).get("usd"),
            "total_volume_usd": (g.get("total_volume") or {}).get("usd"),
            "btc_dominance": (g.get("market_cap_percentage") or {}).get("btc"),
            "eth_dominance": (g.get("market_cap_percentage") or {}).get("eth"),
            "mcap_change_24h_pct": g.get("market_cap_change_percentage_24h_usd"),
        }
    return out


async def fetch_hyperliquid_mids(client: httpx.AsyncClient) -> dict[str, float]:
    url = f"{HYPERLIQUID_RPC.rstrip('/')}/info"
    data = await _safe_post_json(client, url, json={"type": "allMids"})
    if not data or not isinstance(data, dict):
        return {}
    out = {}
    for k, v in data.items():
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            continue
    return out


async def fetch_defillama(client: httpx.AsyncClient) -> dict[str, Any]:
    protocols = await _safe_get_json(client, f"{LLAMA_BASE}/v2/protocols")
    fees = await _safe_get_json(client, f"{LLAMA_BASE}/overview/fees")
    stables = await _safe_get_json(client, "https://stablecoins.llama.fi/stablecoins")
    out: dict[str, Any] = {"top_tvl": [], "top_fees_24h": [], "stables_total": None}
    if isinstance(protocols, list):
        sorted_p = sorted(protocols, key=lambda p: (p.get("tvl") or 0), reverse=True)[:10]
        out["top_tvl"] = [
            {"name": p.get("name"), "tvl": p.get("tvl"), "change_1d": p.get("change_1d")}
            for p in sorted_p
        ]
    if isinstance(fees, dict):
        p = fees.get("protocols") or []
        sorted_f = sorted(p, key=lambda x: (x.get("total24h") or 0), reverse=True)[:10]
        out["top_fees_24h"] = [
            {"name": x.get("name"), "fees_24h": x.get("total24h"), "revenue_24h": x.get("dailyRevenue")}
            for x in sorted_f
        ]
    if isinstance(stables, dict):
        peggeds = stables.get("peggedAssets") or []
        total = 0.0
        for s in peggeds:
            circ = (s.get("circulating") or {}).get("peggedUSD") or 0
            try:
                total += float(circ)
            except (TypeError, ValueError):
                continue
        out["stables_total"] = total
    return out


async def fetch_coinglass(client: httpx.AsyncClient) -> dict[str, Any]:
    """Best-effort CoinGlass data. Requires COINGLASS_API_KEY."""
    out: dict[str, Any] = {"oi_total": None, "liquidations_24h": None, "funding": None, "longshort": None}
    if not COINGLASS_API_KEY:
        return out
    headers = {"accept": "application/json", "CG-API-KEY": COINGLASS_API_KEY, "coinglassSecret": COINGLASS_API_KEY}
    # Endpoints v3 (public) — we keep these wrapped; if they fail we skip.
    endpoints = {
        "oi_total": "https://open-api-v3.coinglass.com/api/futures/openInterest/v2/history",
        "liquidations_24h": "https://open-api-v3.coinglass.com/api/futures/liquidation/v2/history",
        "funding": "https://open-api-v3.coinglass.com/api/futures/fundingRate/v2/history",
        "longshort": "https://open-api-v3.coinglass.com/api/futures/longShortRatio/v2/history",
    }
    for key, url in endpoints.items():
        data = await _safe_get_json(client, url, headers=headers, params={"symbol": "BTC", "interval": "h1", "limit": 1})
        out[key] = data
    return out


async def fetch_market_data() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": "FondoBlackCat/1.0"}) as client:
        fng, cg, hl_mids, llama, cglass = await asyncio.gather(
            fetch_fear_greed(client),
            fetch_coingecko(client),
            fetch_hyperliquid_mids(client),
            fetch_defillama(client),
            fetch_coinglass(client),
        )

    prices = cg.get("prices") or {}
    btc = prices.get("bitcoin") or {}
    eth = prices.get("ethereum") or {}
    hype_cg = prices.get("hyperliquid") or {}
    gold = prices.get("pax-gold") or {}

    # HyperLiquid perp mids for the WAR TRADE legs
    hl_tracked = {}
    for sym in ("HYPE", "BTC", "ETH", "SOL", "NVDA", "TSLA", "HOOD", "SPY", "USA500", "OIL", "BRENT", "GOLD", "SILVER", "XAU", "XAG"):
        if sym in hl_mids:
            hl_tracked[sym] = hl_mids[sym]

    return {
        "fear_greed": fng,
        "btc": {
            "price": btc.get("usd"),
            "change_24h": btc.get("usd_24h_change"),
            "mcap": btc.get("usd_market_cap"),
        },
        "eth": {
            "price": eth.get("usd"),
            "change_24h": eth.get("usd_24h_change"),
        },
        "hype_cg": {
            "price": hype_cg.get("usd"),
            "change_24h": hype_cg.get("usd_24h_change"),
        },
        "gold_paxg": {
            "price": gold.get("usd"),
            "change_24h": gold.get("usd_24h_change"),
        },
        "global": cg.get("global") or {},
        "hl_perps": hl_tracked,
        "defillama": llama,
        "coinglass": cglass,
    }


def format_market_quick(market: dict[str, Any]) -> str:
    btc = market.get("btc") or {}
    eth = market.get("eth") or {}
    g = market.get("global") or {}
    fng = market.get("fear_greed") or {}
    hype_cg = market.get("hype_cg") or {}
    lines = ["🌐 MERCADO"]
    if btc.get("price") is not None:
        lines.append(f"  BTC ${btc['price']:,.0f}  ({btc.get('change_24h') or 0:+.2f}% 24h)")
    if eth.get("price") is not None:
        lines.append(f"  ETH ${eth['price']:,.0f}  ({eth.get('change_24h') or 0:+.2f}% 24h)")
    if hype_cg.get("price") is not None:
        lines.append(f"  HYPE ${hype_cg['price']:,.2f}  ({hype_cg.get('change_24h') or 0:+.2f}% 24h)")
    if g.get("total_mcap_usd"):
        lines.append(f"  MCap total: ${g['total_mcap_usd']/1e12:.2f}T  | BTC.D {g.get('btc_dominance', 0):.1f}%")
    if fng.get("current") is not None:
        lines.append(f"  Fear & Greed: {fng['current']} ({fng.get('classification')})")
    return "\n".join(lines)
