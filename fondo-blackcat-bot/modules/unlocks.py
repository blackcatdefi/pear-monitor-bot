"""
Token unlocks próximos 7 días.

Fuente primaria: DefiLlama unlocks API.
Filtrado:
- Solo unlocks >$2M
- Foco en basket SHORT (WLD, STRK, EIGEN, SCR, ZETA) + HYPE
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from config import BASKET_SHORT

log = logging.getLogger(__name__)

LLAMA_UNLOCKS = "https://api.llama.fi/emissions"
MIN_UNLOCK_USD = 2_000_000


async def _safe_get(client: httpx.AsyncClient, url: str) -> Any:
    try:
        resp = await client.get(url, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:  # noqa: BLE001
        log.warning("GET %s failed: %s", url, e)
        return None


async def fetch_unlocks_raw(client: httpx.AsyncClient) -> list:
    data = await _safe_get(client, LLAMA_UNLOCKS)
    if not data:
        return []
    return data if isinstance(data, list) else data.get("data", [])


def _symbol(token: dict) -> str:
    return (token.get("symbol") or token.get("name") or "").upper()


async def fetch_upcoming_unlocks(days: int = 7) -> list[dict]:
    """Retorna unlocks en los próximos `days` días con valor > MIN_UNLOCK_USD."""
    async with httpx.AsyncClient() as client:
        tokens = await fetch_unlocks_raw(client)

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=days)
    results: list[dict] = []

    for token in tokens or []:
        symbol = _symbol(token)
        price = token.get("tPrice") or token.get("price") or 0
        # Los events pueden venir en diferentes keys según schema de Llama
        events = token.get("events") or token.get("unlocksHistorical") or []
        for ev in events:
            ts = ev.get("timestamp") or ev.get("date")
            if not ts:
                continue
            # timestamp puede ser unix seconds o ms
            if ts > 10**12:
                ts = ts / 1000
            try:
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            except (ValueError, OSError):
                continue
            if dt < now or dt > horizon:
                continue
            amount = ev.get("noOfTokens") or ev.get("amount") or 0
            if isinstance(amount, list):
                amount = sum(amount)
            value_usd = (amount or 0) * (price or 0)
            if value_usd < MIN_UNLOCK_USD:
                continue
            results.append({
                "symbol": symbol,
                "date": dt.isoformat(),
                "amount": amount,
                "value_usd": value_usd,
                "category": ev.get("description") or ev.get("category") or "unlock",
                "in_basket_short": symbol in BASKET_SHORT,
                "is_hype": symbol == "HYPE",
            })

    # Sort: HYPE primero, luego basket short, luego por valor desc
    results.sort(key=lambda r: (not r["is_hype"], not r["in_basket_short"], -r["value_usd"]))
    return results


def format_unlocks_summary(unlocks: list[dict]) -> str:
    if not unlocks:
        return "🔓 Unlocks 7d: ninguno >$2M"
    lines = ["🔓 *UNLOCKS 7d (>$2M)*"]
    for u in unlocks[:15]:
        tag = ""
        if u["is_hype"]:
            tag = "⭐ HYPE "
        elif u["in_basket_short"]:
            tag = "🎯 SHORT "
        date = u["date"][:10]
        lines.append(
            f"{tag}{u['symbol']}: ${u['value_usd']/1e6:.1f}M — {date} ({u['category']})"
        )
    return "\n".join(lines)
