"""Token unlock data with cascading sources.

Round 7 cascade:
    1. SQLite cache (intel_memory.unlock_schedule, 6h TTL)
    2. DefiLlama emissions (existing, free)
    3. DropsTab (public page scrape per priority token)
    4. Tokenomist (if API key or public endpoint works)

Cached results are written back for a 6h window so /reporte doesn't hammer
public services on every call.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from config import ALT_SHORT_BASKET
from utils.http import get_json
from modules import intel_memory

log = logging.getLogger(__name__)

EMISSIONS_URL = "https://api.llama.fi/emissions"
TOKENOMIST_URL = "https://api.tokenomist.ai/v2/unlocks"
MIN_USD_THRESHOLD = 2_000_000  # $2M
WINDOW_DAYS = 14

# Priority tokens we always track regardless of $ threshold.
PRIORITY_TOKENS = {t.upper() for t in (
    *ALT_SHORT_BASKET,
    "HYPE", "ARB", "OP", "EIGEN", "SCR", "ZETA", "SUI",
)}


# ─── Parsers (existing + new) ───────────────────────────────────────────────

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
                    "source": "defillama",
                })
        except Exception as exc:  # noqa: BLE001
            log.debug("Skipping malformed DefiLlama entry: %s", exc)
            continue
    upcoming.sort(key=lambda x: x["timestamp"])
    return upcoming


def _parse_tokenomist(data: Any) -> list[dict[str, Any]]:
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
                "source": "tokenomist",
            })
        except Exception as exc:  # noqa: BLE001
            log.debug("Skipping malformed Tokenomist entry: %s", exc)
            continue
    upcoming.sort(key=lambda x: x["timestamp"])
    return upcoming


# ─── DropsTab scraping (priority tokens only) ────────────────────────────────

_DROPSTAB_JSON_RE = re.compile(
    r'"nextUnlock"\s*:\s*(\{.*?\})', re.DOTALL
)
_NUM_RE = re.compile(r"[-+]?\d*\.?\d+")


def _parse_iso_or_epoch(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = int(value)
        # Heuristic: treat > 10^12 as ms.
        return v // 1000 if v > 10**12 else v
    if isinstance(value, str):
        try:
            v = int(float(value))
            return v // 1000 if v > 10**12 else v
        except Exception:  # noqa: BLE001
            pass
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except Exception:  # noqa: BLE001
            return None
    return None


async def _fetch_dropstab_token(
    client: httpx.AsyncClient, token: str
) -> dict[str, Any] | None:
    """Scrape DropsTab vesting page for one token. Best-effort — returns None on any failure."""
    url = f"https://dropstab.com/coins/{token.lower()}/vesting"
    try:
        resp = await client.get(url, timeout=20.0, follow_redirects=True)
    except Exception as exc:  # noqa: BLE001
        log.debug("DropsTab %s exception: %s", token, exc)
        return None
    if resp.status_code != 200 or not resp.text:
        log.debug("DropsTab %s status=%d", token, resp.status_code)
        return None

    body = resp.text
    # Prefer embedded Next.js __NEXT_DATA__ payload.
    m = re.search(r'__NEXT_DATA__"\s+type="application/json">(\{.*?\})</script>', body)
    if m:
        import json as _json
        try:
            data = _json.loads(m.group(1))
        except Exception:  # noqa: BLE001
            data = None
        if data:
            # Walk props for a 'nextUnlock' or 'vestingEvents' node.
            found = _walk_for_unlock(data)
            if found:
                found["source"] = "dropstab"
                found["symbol"] = token.upper()
                found["priority"] = True
                return found
    # Regex fallback
    m2 = _DROPSTAB_JSON_RE.search(body)
    if m2:
        try:
            import json as _json
            obj = _json.loads(m2.group(1))
            ts = _parse_iso_or_epoch(obj.get("date") or obj.get("timestamp"))
            if ts:
                return {
                    "symbol": token.upper(),
                    "timestamp": ts,
                    "tokens": float(obj.get("amount") or 0),
                    "value_usd": float(obj.get("usd") or 0),
                    "float_pct": float(obj.get("percentage") or 0),
                    "category": obj.get("category"),
                    "source": "dropstab",
                    "priority": True,
                }
        except Exception:  # noqa: BLE001
            pass
    return None


def _walk_for_unlock(node: Any) -> dict[str, Any] | None:
    if isinstance(node, dict):
        if "nextUnlock" in node and isinstance(node["nextUnlock"], dict):
            nu = node["nextUnlock"]
            ts = _parse_iso_or_epoch(nu.get("date") or nu.get("timestamp"))
            if ts:
                return {
                    "timestamp": ts,
                    "tokens": float(nu.get("amount") or 0),
                    "value_usd": float(nu.get("usd") or 0),
                    "float_pct": float(nu.get("percentage") or 0),
                    "category": nu.get("category"),
                }
        for v in node.values():
            r = _walk_for_unlock(v)
            if r:
                return r
    elif isinstance(node, list):
        for it in node:
            r = _walk_for_unlock(it)
            if r:
                return r
    return None


async def _fetch_dropstab_many(tokens: list[str]) -> list[dict[str, Any]]:
    now = int(time.time())
    horizon = now + WINDOW_DAYS * 86400
    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0 FondoBlackCatBot/1.0"},
    ) as client:
        out: list[dict[str, Any]] = []
        for t in tokens:
            row = await _fetch_dropstab_token(client, t)
            if not row:
                continue
            ts = row.get("timestamp")
            if not ts or ts < now or ts > horizon:
                continue
            out.append(row)
        return out


# ─── Public entry point ──────────────────────────────────────────────────────

async def fetch_unlocks() -> dict[str, Any]:
    """Return upcoming unlocks within next WINDOW_DAYS.

    Cascade: cache → DefiLlama → DropsTab → Tokenomist.
    """
    # 0. Cache first
    cached = intel_memory.get_cached_unlocks(window_days=WINDOW_DAYS, max_age_hours=6)
    if cached:
        log.info("unlocks: serving %d events from SQLite cache", len(cached))
        items = [
            {
                "symbol": r["token"],
                "name": r["token"],
                "timestamp": r["next_unlock_ts"],
                "tokens": r.get("amount_tokens") or 0,
                "value_usd": r.get("value_usd") or 0,
                "float_pct": r.get("pct_supply"),
                "category": r.get("category"),
                "type": r.get("category"),
                "priority": (r["token"] or "").upper() in PRIORITY_TOKENS,
                "source": r.get("source") or "cache",
            }
            for r in cached
        ]
        return {"status": "ok", "data": items, "source": "cache"}

    collected: list[dict[str, Any]] = []

    # 1. DefiLlama
    try:
        data = await get_json(EMISSIONS_URL)
        if isinstance(data, list) and data:
            parsed = _parse_defillama(data)
            if parsed:
                collected.extend(parsed)
                log.info("unlocks: DefiLlama returned %d events", len(parsed))
        else:
            log.warning("DefiLlama emissions: empty or non-list response")
    except Exception as exc:  # noqa: BLE001
        log.warning("DefiLlama emissions failed: %s", exc)

    # 2. DropsTab (priority tokens — only if DefiLlama missed them)
    covered = {e["symbol"] for e in collected}
    missing_priority = [t for t in PRIORITY_TOKENS if t not in covered]
    if missing_priority:
        try:
            dts = await _fetch_dropstab_many(missing_priority)
            if dts:
                collected.extend(dts)
                log.info("unlocks: DropsTab returned %d events", len(dts))
        except Exception as exc:  # noqa: BLE001
            log.warning("DropsTab scraping failed: %s", exc)

    # 3. Tokenomist fallback
    if not collected:
        try:
            data = await get_json(TOKENOMIST_URL)
            parsed = _parse_tokenomist(data)
            if parsed:
                collected.extend(parsed)
                log.info("unlocks: Tokenomist returned %d events", len(parsed))
        except Exception as exc:  # noqa: BLE001
            log.warning("Tokenomist unlocks failed: %s", exc)

    if not collected:
        return {"status": "unavailable", "error": "all sources failed"}

    collected.sort(key=lambda x: x.get("timestamp") or 0)

    # Persist to cache for 6h TTL on next run
    try:
        rows = [
            {
                "token": e.get("symbol") or e.get("name"),
                "next_unlock_ts": e.get("timestamp"),
                "amount_tokens": e.get("tokens"),
                "value_usd": e.get("value_usd"),
                "pct_supply": e.get("float_pct"),
                "category": e.get("category"),
                "source": e.get("source") or "mixed",
            }
            for e in collected
            if e.get("symbol") and e.get("timestamp")
        ]
        intel_memory.save_unlock_events(rows)
    except Exception as exc:  # noqa: BLE001
        log.warning("unlocks cache save failed: %s", exc)

    source_label = ",".join(sorted({e.get("source") or "?" for e in collected}))
    return {"status": "ok", "data": collected, "source": source_label}
