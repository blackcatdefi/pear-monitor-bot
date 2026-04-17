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
MIN_USD_THRESHOLD = 2_000_000  # $2M
WINDOW_DAYS = 14
PRIORITY_TOKENS = {t.upper() for t in (*ALT_SHORT_BASKET, "HYPE")}


async def fetch_unlocks() -> dict[str, Any]:
    """Return upcoming unlocks within next WINDOW_DAYS, filtered by size + priority."""
    try:
        data = await get_json(EMISSIONS_URL)
    except Exception as exc:  # noqa: BLE001
        log.warning("DefiLlama emissions failed: %s", exc)
        return {"status": "error", "error": str(exc)}

    if not isinstance(data, list):
        return {"status": "error", "error": "unexpected response shape"}

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
            log.debug("Skipping malformed unlock entry: %s", exc)
            continue

    upcoming.sort(key=lambda x: x["timestamp"])
    return {"status": "ok", "data": upcoming}
