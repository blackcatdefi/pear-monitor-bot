"""Token unlock data via DefiLlama emissions/unlocks API.

Endpoint discovery: https://api.llama.fi/emissions returns list of protocols with
upcoming unlock events. We filter by USD value and basket relevance.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from config import ALT_SHORT_BASKET
from utils.http import get_json

log = logging.getLogger(__name__)

EMISSIONS_URL = "https://api.llama.fi/emissions"
TOKENOMIST_URL = "https://api.tokenomist.ai/v2/unlocks"
MIN_USD_THRESHOLD = 2_000_000  # $2M
WINDOW_DAYS = 14
PRIORITY_TOKENS = {t.upper() for t in (*ALT_SHORT_BASKET, "HYPE")}


def _parse_defillama(data: list) -> list[dict[str, Any]]:
    now = int(time.time())
    horizon = now + WINDOW_DAYS * 86400
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
        except Exception as exc:  # noqa: BLE001
            log.debug("Skipping malformed DefiLlama entry: %s", exc)
            continue
    upcoming.sort(key=lambda x: x["timestamp"])
    return upcoming


def _parse_tokenomist(data: Any) -> list[dict[str, Any]]:
    from datetime import datetime, timezone  # noqa: PLC0415
    if isinstance(data, dict):
        items: list = data.get("data") or data.get("unlocks") or []
    elif isinstance(data, list):
        items = data
    else:
        return []

    now = int(time.time())
    horizon = now + WINDOW_DAYS * 86400
    upcoming: list[dict[str, Any]] = []
    for item in items:
        try:
            symbol = (item.get("symbol") or item.get("token") or "").upper()
            ts = item.get("unlock_timestamp") or item.get("timestamp") or item.get("date")
            if isinstance(ts, str):
                try:
                    ts = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
                except Exception:  # noqa: BLE001
                    ts = None
            if not ts or ts < now or ts > horizon:
                continue
            tokens = float(item.get("tokens") or item.get("amount") or 0)
            value_usd = float(
                item.get("unlock_usd") or item.get("value_usd") or item.get("usd") or 0
            )
            is_priority = symbol in PRIORITY_TOKENS
            if value_usd < MIN_USD_THRESHOLD and not is_priority:
                continue
            upcoming.append({
                "symbol": symbol,
                "name": item.get("name") or symbol,
                "timestamp": ts,
                "tokens": tokens,
                "value_usd": value_usd,
                "float_pct": item.get("float_pct") or item.get("percentage"),
                "category": item.get("category") or item.get("type"),
                "type": item.get("category") or item.get("type"),
                "priority": is_priority,
            })
        except Exception as exc:  # noqa: BLE001
            log.debug("Skipping malformed Tokenomist entry: %s", exc)
            continue
    upcoming.sort(key=lambda x: x["timestamp"])
    return upcoming


async def fetch_unlocks() -> dict[str, Any]:
    """Return upcoming unlocks within next WINDOW_DAYS, filtered by size + priority.

    Sources (in order): DefiLlama emissions → Tokenomist API.
    Returns {"status": "unavailable", ...} only when all sources fail.
    """
    # 1. DefiLlama
    try:
        data = await get_json(EMISSIONS_URL)
        if isinstance(data, list) and data:
            return {"status": "ok", "data": _parse_defillama(data)}
        log.warning("DefiLlama emissions: empty or non-list response")
    except Exception as exc:  # noqa: BLE001
        log.warning("DefiLlama emissions failed: %s", exc)

    # 2. Tokenomist fallback
    try:
        data = await get_json(TOKENOMIST_URL)
        upcoming = _parse_tokenomist(data)
        return {"status": "ok", "data": upcoming, "source": "tokenomist"}
    except Exception as exc:  # noqa: BLE001
        log.warning("Tokenomist unlocks failed: %s", exc)

    # 3. All sources exhausted
    return {"status": "unavailable", "error": "all sources failed"}
