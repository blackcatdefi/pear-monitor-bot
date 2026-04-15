"""Token unlocks within the next N days.

Primary source: DefiLlama's unlocks endpoint which returns the events field
per protocol. We filter by:
  - value >= MIN_USD (default $2M)
  - unlock date within next `days` window
  - prioritize tokens in SHORT_BASKET + HYPE

Graceful fallback: if the endpoint fails, returns an empty list with error.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from config import CORE_TOKENS, SHORT_BASKET

log = logging.getLogger(__name__)

URL_LIST = "https://api.llama.fi/emissions"  # returns all unlock events
URL_EMISSIONS_ALT = "https://defillama-datasets.llama.fi/emissions/emissionsTimeline.json"

MIN_USD = 2_000_000


async def fetch_unlocks(days: int = 7) -> dict[str, Any]:
    now = int(time.time())
    cutoff = now + days * 24 * 3600
    events: list[dict[str, Any]] = []
    error: str | None = None

    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
        data = None
        for url in (URL_LIST, URL_EMISSIONS_ALT):
            try:
                r = await client.get(url)
                r.raise_for_status()
                data = r.json()
                break
            except Exception as e:  # noqa: BLE001
                log.warning("unlocks GET %s failed: %s", url, e)
                error = str(e)

    if not data:
        return {"events": [], "error": error or "no data"}

    # DefiLlama's /emissions returns list of {name, symbol, events: [...]}
    iter_items = data if isinstance(data, list) else data.get("protocols") or []
    for item in iter_items:
        if not isinstance(item, dict):
            continue
        symbol = (item.get("symbol") or item.get("token") or "").upper()
        name = item.get("name") or symbol
        evs = item.get("events") or item.get("unlocks") or []
        for ev in evs:
            try:
                ts = int(ev.get("timestamp") or ev.get("time") or 0)
                if ts <= 0 or ts < now or ts > cutoff:
                    continue
                value_usd = ev.get("unlockUsd") or ev.get("valueUsd") or ev.get("value")
                value_usd = float(value_usd) if value_usd else 0.0
                pct_float = ev.get("circulatingSupplyPct") or ev.get("floatPct") or ev.get("percent")
                description = ev.get("description") or ev.get("type")
                if value_usd < MIN_USD and symbol not in SHORT_BASKET and symbol not in CORE_TOKENS:
                    continue
                events.append({
                    "symbol": symbol,
                    "name": name,
                    "timestamp": ts,
                    "value_usd": value_usd,
                    "pct_float": pct_float,
                    "type": description,
                    "priority": (
                        "SHORT_BASKET" if symbol in SHORT_BASKET
                        else ("CORE" if symbol in CORE_TOKENS else "GENERAL")
                    ),
                })
            except (TypeError, ValueError):
                continue

    # Sort: priority buckets then time asc
    prio_rank = {"SHORT_BASKET": 0, "CORE": 1, "GENERAL": 2}
    events.sort(key=lambda e: (prio_rank.get(e["priority"], 9), e["timestamp"]))
    return {"events": events, "error": error}


def format_unlocks(unlocks: dict[str, Any]) -> str:
    evs = unlocks.get("events") or []
    if not evs:
        if unlocks.get("error"):
            return f"Unlocks: no disponible ({unlocks['error']})"
        return "Unlocks: sin eventos relevantes próximos 7d"
    lines = ["🔓 UNLOCKS (7d)"]
    for e in evs[:15]:
        from datetime import datetime, timezone
        when = datetime.fromtimestamp(e["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        pct = f" · {e['pct_float']*100:.1f}% float" if isinstance(e.get("pct_float"), (int, float)) else ""
        tag = "🔴" if e["priority"] == "SHORT_BASKET" else ("🟡" if e["priority"] == "CORE" else "·")
        lines.append(f"  {tag} {e['symbol']} — ${e['value_usd']/1e6:,.1f}M {when}{pct}")
    return "\n".join(lines)
