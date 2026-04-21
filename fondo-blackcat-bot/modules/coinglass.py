"""CoinGlass v4 basket OI + funding rate snapshot.

New in Round 7. Central endpoint for Open Interest + Funding across the
basket the fund cares about (majors + SHORT basket). Results flow into
/reporte and into an automatic crowded-short alert.

Env:
    COINGLASS_API_KEY — required; free tier ≈ 30 req/min, 10k req/day.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

COINGLASS_API_KEY = os.getenv("COINGLASS_API_KEY", "").strip()
BASE = "https://open-api-v4.coinglass.com/api"
TIMEOUT = 15.0

BASKET = ["BTC", "ETH", "HYPE", "WLD", "STRK", "AVAX", "ZRO", "ENA"]

# A funding rate below this threshold on a SHORT in our basket is a
# squeeze-risk signal (shorts heavily crowded, funding paying longs).
CROWDED_SHORT_FUNDING_THRESHOLD = -0.0003  # -0.03%


async def _get(client: httpx.AsyncClient, path: str, params: dict) -> dict:
    headers = {"CG-API-KEY": COINGLASS_API_KEY, "accept": "application/json"}
    resp = await client.get(f"{BASE}{path}", params=params, headers=headers, timeout=TIMEOUT)
    if resp.status_code != 200:
        log.warning("Coinglass %s status=%d body=%s", path, resp.status_code, resp.text[:180])
        return {}
    try:
        return resp.json() or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("Coinglass %s parse: %s", path, exc)
        return {}


def _extract_oi_usd(payload: dict) -> float | None:
    d = payload.get("data")
    if isinstance(d, dict):
        for k in ("openInterestUSD", "openInterestUsd", "openInterest", "value"):
            v = d.get(k)
            if v is not None:
                try:
                    return float(v)
                except Exception:  # noqa: BLE001
                    continue
    if isinstance(d, list) and d:
        row = d[0] if isinstance(d[0], dict) else {}
        for k in ("openInterestUSD", "openInterestUsd", "openInterest"):
            v = row.get(k)
            if v is not None:
                try:
                    return float(v)
                except Exception:  # noqa: BLE001
                    continue
    return None


def _extract_funding(payload: dict) -> float | None:
    d = payload.get("data")
    if isinstance(d, dict):
        for k in ("fundingRate", "funding_rate", "rate", "value"):
            v = d.get(k)
            if v is not None:
                try:
                    return float(v)
                except Exception:  # noqa: BLE001
                    continue
    if isinstance(d, list) and d:
        row = d[0] if isinstance(d[0], dict) else {}
        for k in ("fundingRate", "funding_rate", "rate"):
            v = row.get(k)
            if v is not None:
                try:
                    return float(v)
                except Exception:  # noqa: BLE001
                    continue
    return None


async def get_oi_funding(symbol: str) -> dict[str, Any]:
    """Fetch {oi_usd, funding} for a single symbol via Coinglass v4."""
    if not COINGLASS_API_KEY:
        return {"symbol": symbol, "oi_usd": None, "funding": None, "error": "no_api_key"}
    try:
        async with httpx.AsyncClient() as client:
            oi_payload, fr_payload = await asyncio.gather(
                _get(client, "/futures/openInterest", {"symbol": symbol}),
                _get(client, "/futures/fundingRate", {"symbol": symbol}),
            )
        return {
            "symbol": symbol,
            "oi_usd": _extract_oi_usd(oi_payload),
            "funding": _extract_funding(fr_payload),
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("Coinglass %s failed: %s", symbol, exc)
        return {"symbol": symbol, "oi_usd": None, "funding": None, "error": str(exc)}


async def get_basket_oi_funding(symbols: list[str] | None = None) -> dict[str, Any]:
    """Concurrent OI+funding fetch across the fund's basket."""
    syms = symbols or BASKET
    if not COINGLASS_API_KEY:
        return {
            "available": False,
            "reason": "no_api_key",
            "basket": [],
        }

    sem = asyncio.Semaphore(6)

    async def _one(sym: str) -> dict[str, Any]:
        async with sem:
            return await get_oi_funding(sym)

    results = await asyncio.gather(*[_one(s) for s in syms], return_exceptions=True)
    rows: list[dict[str, Any]] = []
    for r in results:
        if isinstance(r, Exception):
            continue
        rows.append(r)

    # Flag crowded shorts in the fund's SHORT basket (WLD/STRK/AVAX/ZRO/ENA)
    short_basket = {"WLD", "STRK", "AVAX", "ZRO", "ENA"}
    crowded: list[dict[str, Any]] = []
    for r in rows:
        if r["symbol"] in short_basket and r.get("funding") is not None:
            if r["funding"] < CROWDED_SHORT_FUNDING_THRESHOLD:
                crowded.append(r)

    return {
        "available": True,
        "basket": rows,
        "crowded_shorts": crowded,
    }


def format_basket_section(data: dict[str, Any]) -> str:
    """Render the basket summary for /reporte."""
    if not data or not data.get("available"):
        reason = (data or {}).get("reason", "unknown")
        return f"\n📊 OI / FUNDING (Coinglass): no disponible ({reason})\n"

    basket = data.get("basket") or []
    if not basket:
        return "\n📊 OI / FUNDING (Coinglass): sin datos\n"

    lines = ["", "📊 OI / FUNDING (Coinglass)"]
    for r in basket:
        sym = r["symbol"]
        oi = r.get("oi_usd")
        fr = r.get("funding")
        if oi is None and fr is None:
            lines.append(f"  {sym:<5} n/d")
            continue
        oi_str = f"${oi/1e9:.2f}B" if oi and oi > 1e9 else (f"${oi/1e6:.1f}M" if oi else "n/d")
        fr_str = f"{fr*100:+.4f}%" if fr is not None else "n/d"
        lines.append(f"  {sym:<5} OI {oi_str:<10} fund {fr_str}")

    crowded = data.get("crowded_shorts") or []
    if crowded:
        lines.append("")
        lines.append("⚠️ CROWDED SHORTS (funding muy negativo = risk squeeze):")
        for r in crowded:
            lines.append(
                f"  {r['symbol']:<5} funding {r['funding']*100:+.4f}% — "
                "SHORTS pagando LONGS, vigilar"
            )

    return "\n".join(lines) + "\n"
