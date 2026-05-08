"""HypurrScan REST (R-INTEL30 Phase 1 #3).

Only public feed for HIP-1/HIP-3 Dutch auction prices + TWAP order tracker.
Documented Swagger.

Endpoint base: https://api.hypurrscan.io
Free, community-grade limits. Real-time.

Endpoints used:
    /ui/auctions          — current Dutch auction state
    /ui/twap/{addr}       — TWAP orders by address (optional; not used by default)
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE = "https://api.hypurrscan.io"
HTTP_TIMEOUT = 10.0


async def fetch_auctions() -> dict[str, Any]:
    """Latest HIP-1 Dutch auction state."""
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(f"{BASE}/ui/auctions")
            r.raise_for_status()
            data = r.json()
        # Format may vary; pass through
        return {"data": data, "_error": None}
    except Exception as e:
        log.warning("hypurrscan auctions fail: %s", e)
        return {"data": None, "_error": str(e)}


async def fetch_all() -> dict[str, Any]:
    return {"auctions": await fetch_auctions()}


def format_for_telegram(data: dict[str, Any]) -> str:
    lines = ["🪶 *HypurrScan — HIP-1 Auctions*"]
    auc = data.get("auctions") or {}
    if auc.get("_error"):
        lines.append(f"  ⚠️ {auc['_error'][:60]}")
        return "\n".join(lines)
    payload = auc.get("data")
    if not payload:
        lines.append("  • sin datos")
        return "\n".join(lines)

    # Common shapes:
    # 1) {"currentAuction": {...}, "history": [...]}
    # 2) [{"name":..., "price":..., "endTime":...}, ...]
    rendered = 0
    if isinstance(payload, dict):
        cur = payload.get("currentAuction") or payload.get("current") or payload.get("auction")
        if isinstance(cur, dict):
            name = cur.get("name") or cur.get("ticker") or "?"
            price = cur.get("currentPrice") or cur.get("price")
            start_price = cur.get("startPrice")
            end_price = cur.get("endPrice")
            lines.append(f"  • Active: `{name}` — px ${price}")
            if start_price is not None and end_price is not None:
                lines.append(f"    range: ${start_price} → ${end_price}")
            rendered = 1
        history = payload.get("history") or payload.get("recent")
        if isinstance(history, list) and history:
            lines.append(f"  • Recent auctions ({len(history)}):")
            for h in history[:5]:
                if isinstance(h, dict):
                    nm = h.get("name") or h.get("ticker") or "?"
                    fp = h.get("finalPrice") or h.get("price")
                    lines.append(f"    – `{nm}` final ${fp}")
            rendered += 1
    elif isinstance(payload, list):
        lines.append(f"  • {len(payload)} auctions returned")
        for p in payload[:6]:
            if isinstance(p, dict):
                nm = p.get("name") or p.get("ticker") or "?"
                pr = p.get("currentPrice") or p.get("price") or p.get("finalPrice")
                lines.append(f"    – `{nm}` ${pr}")
                rendered += 1
    if rendered == 0:
        lines.append("  • (formato inesperado, dump primer entry)")
        lines.append(f"  ```{str(payload)[:200]}```")
    return "\n".join(lines)
