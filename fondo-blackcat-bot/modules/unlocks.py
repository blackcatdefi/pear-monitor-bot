"""Token unlock data with fallback cascade.

Primary: DefiLlama emissions API (https://api.llama.fi/emissions)
Fallback 1: Tokenomist API (https://tokenomist.ai/api)
Fallback 2: Scrape defillama.com/unlocks
If all fail: return error dict with details.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from config import ALT_SHORT_BASKET
from utils.http import get_json

log = logging.getLogger(__name__)

EMISSIONS_URL = "https://api.llama.fi/emissions"
TOKENOMIST_URL = "https://tokenomist.ai/api/unlocks"
DEFILLAMA_SCRAPE_URL = "https://defillama.com/unlocks"
MIN_USD_THRESHOLD = 2_000_000  # $2M
WINDOW_DAYS = 14
PRIORITY_TOKENS = {t.upper() for t in (*ALT_SHORT_BASKET, "HYPE")}


def _parse_defillama_events(data: list, now: int, horizon: int) -> list[dict[str, Any]]:
    """Parse DefiLlama emissions API response into unlock events."""
    upcoming: list[dict[str, Any]] = []
    for proto in data:
        try:
            symbol = (proto.get("token") or proto.get("name") or "").upper()
            price = proto.get("tPrice") or proto.get("price") or 0
            float_pct_per_event = proto.get("floatPercentPerEvent")
            events = proto.get("events") or []
            for ev in events:
                ts = ev.get("timestamp")
                if not ts or ts < now or ts > horizon:
                    continue
                tokens = float(ev.get("noOfTokens", 0) or 0)
                if isinstance(tokens, list):
                    tokens = sum(float(t or 0) for t in tokens)
                value_usd = tokens * float(price or 0)
                is_priority = symbol in PRIORITY_TOKENS
                if value_usd < MIN_USD_THRESHOLD and not is_priority:
                    continue
                upcoming.append({
                    "symbol": symbol,
                    "name": proto.get("name"),
                    "timestamp": ts,
                    "tokens": tokens,
                    "value_usd": value_usd,
                    "float_pct": float_pct_per_event,
                    "category": proto.get("category"),
                    "type": ev.get("description") or proto.get("category"),
                    "priority": is_priority,
                })
        except Exception:  # noqa: BLE001
            continue
    return upcoming


def _parse_tokenomist_events(data: list | dict, now: int, horizon: int) -> list[dict[str, Any]]:
    """Parse Tokenomist API response into unlock events."""
    upcoming: list[dict[str, Any]] = []
    items = data if isinstance(data, list) else data.get("data", data.get("unlocks", []))
    if not isinstance(items, list):
        return upcoming
    for item in items:
        try:
            symbol = (item.get("symbol") or item.get("token") or "").upper()
            ts = item.get("unlock_date") or item.get("timestamp") or item.get("date")
            if isinstance(ts, str):
                # Try parsing ISO date string
                import datetime
                try:
                    dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    ts = int(dt.timestamp())
                except (ValueError, TypeError):
                    continue
            if not ts or ts < now or ts > horizon:
                continue
            value_usd = float(item.get("value_usd") or item.get("usd_value") or item.get("value") or 0)
            tokens = float(item.get("tokens") or item.get("amount") or item.get("noOfTokens") or 0)
            is_priority = symbol in PRIORITY_TOKENS
            if value_usd < MIN_USD_THRESHOLD and not is_priority:
                continue
            upcoming.append({
                "symbol": symbol,
                "name": item.get("name") or item.get("project") or symbol,
                "timestamp": ts,
                "tokens": tokens,
                "value_usd": value_usd,
                "float_pct": item.get("float_pct") or item.get("percent_of_supply"),
                "category": item.get("category") or "unlock",
                "type": item.get("type") or item.get("description") or "token_unlock",
                "priority": is_priority,
            })
        except Exception:  # noqa: BLE001
            continue
    return upcoming


async def _try_defillama_api(now: int, horizon: int) -> list[dict[str, Any]] | None:
    """Attempt 1: DefiLlama emissions API."""
    try:
        data = await get_json(EMISSIONS_URL)
        if not isinstance(data, list):
            log.warning("DefiLlama: unexpected response shape")
            return None
        result = _parse_defillama_events(data, now, horizon)
        if result:
            log.info("DefiLlama API: got %d unlock events", len(result))
        return result
    except Exception as exc:  # noqa: BLE001
        log.warning("DefiLlama API failed: %s", exc)
        return None


async def _try_tokenomist_api(now: int, horizon: int) -> list[dict[str, Any]] | None:
    """Attempt 2: Tokenomist API."""
    try:
        data = await get_json(TOKENOMIST_URL)
        result = _parse_tokenomist_events(data, now, horizon)
        if result:
            log.info("Tokenomist API: got %d unlock events", len(result))
        return result
    except Exception as exc:  # noqa: BLE001
        log.warning("Tokenomist API failed: %s", exc)
        return None


async def _try_scrape_defillama(now: int, horizon: int) -> list[dict[str, Any]] | None:
    """Attempt 3: Scrape defillama.com/unlocks page for basic data."""
    try:
        import httpx
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; FondoBlackCat/1.0)"},
        ) as client:
            resp = await client.get(DEFILLAMA_SCRAPE_URL)
            resp.raise_for_status()
            html = resp.text

        # Try to extract __NEXT_DATA__ JSON from Next.js page
        match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if not match:
            log.warning("Scrape: no __NEXT_DATA__ found")
            return None

        import json
        next_data = json.loads(match.group(1))
        props = next_data.get("props", {}).get("pageProps", {})
        protocols = props.get("protocols") or props.get("data") or []
        if not protocols:
            log.warning("Scrape: no protocols in __NEXT_DATA__")
            return None

        result = _parse_defillama_events(protocols, now, horizon)
        if result:
            log.info("Scrape defillama.com: got %d unlock events", len(result))
        return result
    except Exception as exc:  # noqa: BLE001
        log.warning("Scrape defillama.com failed: %s", exc)
        return None


async def fetch_unlocks() -> dict[str, Any]:
    """Return upcoming unlocks with fallback cascade.

    1. DefiLlama emissions API
    2. Tokenomist API
    3. Scrape defillama.com/unlocks
    4. Error dict if all fail
    """
    now = int(time.time())
    horizon = now + WINDOW_DAYS * 86400
    errors: list[str] = []

    # Attempt 1: DefiLlama API
    result = await _try_defillama_api(now, horizon)
    if result is not None:
        result.sort(key=lambda x: x["timestamp"])
        return {"status": "ok", "source": "defillama_api", "data": result}
    errors.append("DefiLlama API failed")

    # Attempt 2: Tokenomist API
    result = await _try_tokenomist_api(now, horizon)
    if result is not None:
        result.sort(key=lambda x: x["timestamp"])
        return {"status": "ok", "source": "tokenomist_api", "data": result}
    errors.append("Tokenomist API failed")

    # Attempt 3: Scrape defillama.com
    result = await _try_scrape_defillama(now, horizon)
    if result is not None:
        result.sort(key=lambda x: x["timestamp"])
        return {"status": "ok", "source": "defillama_scrape", "data": result}
    errors.append("defillama.com scrape failed")

    # All failed
    log.error("All unlock sources failed: %s", "; ".join(errors))
    return {
        "status": "error",
        "error": "All sources failed",
        "details": errors,
    }
