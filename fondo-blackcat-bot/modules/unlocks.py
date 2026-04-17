"""Token unlock data via DefiLlama emissions/unlocks API.

Endpoint discovery: https://api.llama.fi/emissions returns list of protocols
with upcoming unlock events.  We filter by USD value and basket relevance.
Falls back to Tokenomist API and then HTML scraping if DefiLlama returns
402/error or an EMPTY result set.
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
TOKENOMIST_URL = "https://tokenomist.ai/api/v1/unlocks"
DEFILLAMA_HTML_URL = "https://defillama.com/unlocks"

MIN_USD_THRESHOLD = 2_000_000  # $2M
WINDOW_DAYS = 14
PRIORITY_TOKENS = {t.upper() for t in (*ALT_SHORT_BASKET, "HYPE")}


def _parse_defillama_events(data: list, now: int, horizon: int) -> list[dict[str, Any]]:
    """Parse DefiLlama emissions API response into unlock entries."""
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
    return upcoming


async def _try_defillama(now: int, horizon: int) -> list[dict[str, Any]] | None:
    """Primary source: DefiLlama emissions API."""
    try:
        data = await get_json(EMISSIONS_URL)
        if not isinstance(data, list):
            log.warning("DefiLlama emissions: unexpected shape (type=%s)", type(data).__name__)
            return None
        result = _parse_defillama_events(data, now, horizon)
        log.info("DefiLlama emissions: %d raw protocols, %d matching unlocks", len(data), len(result))
        # Return None if empty so fallback triggers (empty ≠ success)
        if not result:
            log.warning("DefiLlama emissions: parsed OK but 0 unlocks matched filters — triggering fallback")
            return None
        return result
    except Exception as exc:  # noqa: BLE001
        log.warning("DefiLlama emissions failed: %s", exc)
        return None


async def _try_tokenomist(now: int, horizon: int) -> list[dict[str, Any]] | None:
    """Fallback #1: Tokenomist API."""
    try:
        data = await get_json(TOKENOMIST_URL)
        if not isinstance(data, (list, dict)):
            log.warning("Tokenomist: unexpected shape (type=%s)", type(data).__name__)
            return None
        # Tokenomist may return { "data": [...] } or just [...]
        items = data if isinstance(data, list) else data.get("data") or data.get("unlocks") or []
        if not isinstance(items, list):
            log.warning("Tokenomist: no iterable data found")
            return None

        upcoming: list[dict[str, Any]] = []
        for item in items:
            try:
                symbol = (item.get("symbol") or item.get("token") or item.get("name") or "").upper()
                ts = item.get("timestamp") or item.get("unlock_date") or item.get("date")
                if isinstance(ts, str):
                    # Try ISO date parsing
                    import datetime
                    try:
                        ts = int(datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
                    except Exception:
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
                    "name": item.get("name") or symbol,
                    "timestamp": ts,
                    "tokens": tokens,
                    "value_usd": value_usd,
                    "float_pct": item.get("float_pct") or item.get("pctOfFloat"),
                    "category": item.get("category") or item.get("type"),
                    "type": item.get("type") or item.get("category") or "unlock",
                    "priority": is_priority,
                })
            except Exception as exc:  # noqa: BLE001
                log.debug("Tokenomist: skipping entry: %s", exc)
                continue

        log.info("Tokenomist: %d items fetched, %d matching unlocks", len(items), len(upcoming))
        # Return None if empty so next fallback triggers
        if not upcoming:
            log.warning("Tokenomist: parsed OK but 0 unlocks matched — triggering next fallback")
            return None
        return upcoming
    except Exception as exc:  # noqa: BLE001
        log.warning("Tokenomist failed: %s", exc)
        return None


async def _try_defillama_scrape(now: int, horizon: int) -> list[dict[str, Any]] | None:
    """Fallback #2: Scrape defillama.com/unlocks HTML page."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(DEFILLAMA_HTML_URL, headers={
                "User-Agent": "Mozilla/5.0 (compatible; FondoBlackCatBot/1.0)"
            })
            resp.raise_for_status()
            html = resp.text

        # Try to extract __NEXT_DATA__ JSON from the page (Next.js SSR)
        import json
        match = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            html, re.DOTALL,
        )
        if match:
            try:
                next_data = json.loads(match.group(1))
                # Navigate to props.pageProps — structure varies
                page_props = next_data.get("props", {}).get("pageProps", {})
                emissions = page_props.get("emissions") or page_props.get("data") or []
                if isinstance(emissions, list) and emissions:
                    result = _parse_defillama_events(emissions, now, horizon)
                    log.info("DefiLlama HTML scrape OK: %d unlocks", len(result))
                    if result:
                        return result
            except json.JSONDecodeError:
                pass

        log.warning("DefiLlama HTML scrape: could not extract useful data from page")
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("DefiLlama HTML scrape failed: %s", exc)
        return None


async def fetch_unlocks() -> dict[str, Any]:
    """Return upcoming unlocks within next WINDOW_DAYS, filtered by size + priority.

    Tries sources in order: DefiLlama API → Tokenomist API → DefiLlama HTML scrape.
    A source returning an EMPTY list (0 matching unlocks) is treated as a miss
    and the next source is tried.
    """
    now = int(time.time())
    horizon = now + WINDOW_DAYS * 86400

    # Try each source in order
    for source_name, fetcher in [
        ("DefiLlama API", _try_defillama),
        ("Tokenomist API", _try_tokenomist),
        ("DefiLlama HTML", _try_defillama_scrape),
    ]:
        result = await fetcher(now, horizon)
        # result must be a non-empty list to count as success
        if result:
            result.sort(key=lambda x: x["timestamp"])
            return {"status": "ok", "source": source_name, "data": result}

    return {
        "status": "error",
        "error": "All unlock sources returned empty or failed (DefiLlama API, Tokenomist, DefiLlama HTML scrape)",
    }
