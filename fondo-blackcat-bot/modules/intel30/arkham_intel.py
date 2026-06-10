"""Arkham Intelligence (R-INTEL30 Phase 1 #6).

450K+ entity labels, 800M+ tags. Lazarus, Wintermute, Jump, gov wallets pre-tagged.
Complements Lookonchain with raw structured data.

Free tier: Web UI 100% free; API 20 req/sec standard, 1 req/sec heavy.
Endpoint base: https://api.arkm.com  (key required for auth)

Module degrades gracefully if ARKHAM_API_KEY env var not set.

Use case for the fund: monitor known whale entity transfers (e.g. exchange in/out flows)
that may signal market-moving events.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

API_KEY = os.getenv("ARKHAM_API_KEY", "").strip()
BASE = "https://api.arkm.com"
HTTP_TIMEOUT = 10.0

# Entities of interest (typical hedge-fund watchlist)
WATCHED_ENTITIES = [
    "wintermute",
    "jump-trading",
    "alameda-research",
    "binance",
    "coinbase",
]


async def fetch_entity_transfers(entity_id: str, limit: int = 5) -> dict[str, Any]:
    """Latest transfers in/out of a labeled entity."""
    if not API_KEY:
        return {"entity": entity_id, "_error": "no_api_key"}
    try:
        params = {"entity": entity_id, "limit": limit}
        headers = {"API-Key": API_KEY, "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=headers) as client:
            r = await client.get(f"{BASE}/transfers", params=params)
            r.raise_for_status()
            data = r.json()
        return {"entity": entity_id, "transfers": data, "_error": None}
    except Exception as e:
        log.warning("arkham %s fail: %s", entity_id, e)
        return {"entity": entity_id, "_error": str(e)}


async def fetch_all() -> dict[str, Any]:
    if not API_KEY:
        return {"entities": [], "_global_error": "ARKHAM_API_KEY not set"}
    tasks = [fetch_entity_transfers(e, limit=3) for e in WATCHED_ENTITIES]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out: list[dict[str, Any]] = []
    for r in results:
        if isinstance(r, Exception):
            out.append({"_error": str(r)})
        else:
            out.append(r)
    return {"entities": out, "_global_error": None}


def format_for_telegram(data: dict[str, Any]) -> str:
    lines = ["🐋 *Arkham — Whale/Entity Transfers*"]
    if data.get("_global_error"):
        # WI-9e: without ARKHAM_API_KEY the section is skipped SILENTLY (no
        # nag line); any other global failure degrades to one short line.
        if "ARKHAM_API_KEY" in str(data.get("_global_error")):
            return ""
        return "🐋 Arkham: fuente no disponible este run"
    entities = data.get("entities") or []
    rendered = 0
    for e in entities:
        if not isinstance(e, dict) or e.get("_error"):
            continue
        name = e.get("entity", "?")
        transfers = e.get("transfers")
        if isinstance(transfers, dict):
            txs = transfers.get("transfers") or transfers.get("data") or []
        elif isinstance(transfers, list):
            txs = transfers
        else:
            continue
        if not txs:
            continue
        lines.append(f"  • {name} (last {len(txs)}):")
        for tx in txs[:3]:
            if isinstance(tx, dict):
                amt = tx.get("amountUSD") or tx.get("usdValue") or tx.get("amount")
                token = tx.get("symbol") or tx.get("tokenSymbol") or "?"
                ts = tx.get("blockTime") or tx.get("timestamp", "")
                if isinstance(amt, (int, float)):
                    lines.append(f"    – {token} ${amt:,.0f} @ {ts}")
                else:
                    lines.append(f"    – {token} {amt} @ {ts}")
        rendered += 1
    if rendered == 0:
        # WI-9e: ONE short line, no stack fragments.
        return "🐋 Arkham: fuente no disponible este run"
    return "\n".join(lines)
