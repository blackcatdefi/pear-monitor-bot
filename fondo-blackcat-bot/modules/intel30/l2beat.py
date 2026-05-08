"""L2Beat Scaling Summary API (R-PERFECT Sub-3 #1).

API: https://api.l2beat.com/api/scaling/summary  (auth required since 2026)

L2BEAT_API_KEY env var. Free signup: l2beat.com/api-access (request via form).
Module degrades gracefully if missing.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from modules.intel30._intel_base import (
    GRACEFUL_NO_KEY,
    LIVE,
    get_json,
    graceful_no_key_payload,
    log_call,
)

log = logging.getLogger(__name__)

API_KEY = os.getenv("L2BEAT_API_KEY", "").strip()
BASE = "https://api.l2beat.com/api"
SOURCE = "l2beat"


async def fetch_all() -> dict[str, Any]:
    if not API_KEY:
        return graceful_no_key_payload(
            SOURCE,
            "https://l2beat.com/api-access",
            "L2BEAT_API_KEY",
        )
    data, meta = await get_json(
        SOURCE, f"{BASE}/scaling/summary",
        params={"apiKey": API_KEY},
        timeout=12.0,
    )
    if not data or not isinstance(data, dict):
        return {"_global_error": meta.get("reason", "fetch_failed"), "series": []}
    projects = data.get("projects") or {}
    if not isinstance(projects, (list, dict)):
        return {"_global_error": "shape_changed", "series": []}
    if isinstance(projects, dict):
        projects = list(projects.values())
    # Sort by tvs/tvl descending; take top 10
    rows: list[dict[str, Any]] = []
    for p in projects:
        if not isinstance(p, dict):
            continue
        tvs = p.get("tvs") or p.get("tvl") or {}
        breakdown = tvs.get("breakdown") if isinstance(tvs, dict) else None
        total = (breakdown or {}).get("total") or tvs.get("total") if isinstance(tvs, dict) else None
        try:
            total_f = float(total) if total else 0.0
        except (TypeError, ValueError):
            total_f = 0.0
        rows.append({
            "label": p.get("name") or p.get("id") or "?",
            "valor": total_f,
            "_error": None,
        })
    rows.sort(key=lambda r: r.get("valor", 0), reverse=True)
    log_call(SOURCE, LIVE, 0, 0, 200, f"top {len(rows[:10])}")
    return {"series": rows[:10], "_global_error": None}


def format_for_telegram(data: dict[str, Any]) -> str:
    lines = ["📐 *L2Beat — Top L2 TVS*"]
    if data.get("_status") == GRACEFUL_NO_KEY:
        lines.append("  ⚠️ L2BEAT_API_KEY not set")
        lines.append("  → l2beat.com/api-access")
        return "\n".join(lines)
    if data.get("_global_error"):
        lines.append(f"  ⚠️ {data['_global_error']}")
        return "\n".join(lines)
    for s in data.get("series", []):
        if not isinstance(s, dict) or s.get("_error"):
            continue
        lab = s.get("label", "?")
        v = s.get("valor", 0)
        if v >= 1e9:
            lines.append(f"  • {lab}: ${v/1e9:.2f}B")
        elif v >= 1e6:
            lines.append(f"  • {lab}: ${v/1e6:.1f}M")
        else:
            lines.append(f"  • {lab}: ${v:,.0f}")
    return "\n".join(lines)
