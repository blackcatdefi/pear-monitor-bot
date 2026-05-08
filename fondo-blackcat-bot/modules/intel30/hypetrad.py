"""HypeTrad — HL pro-trader leaderboard scraper (R-PERFECT Sub-1 #4).

Source surface (research-pending): hypetrad.com or hypetrad.io exposes a leaderboard
of profitable HL perp traders. As of 2026-05-08 the page is a Next.js SPA — JSON BFF
endpoint not publicly documented.

This module probes a small set of candidate URLs in parallel and degrades to
SPA_DEGRADED when none of them return JSON. Adds source_status so /selftest can
distinguish "live + 0 results" from "endpoint moved".

Once HypeTrad publishes a stable API, replace _CANDIDATES with the canonical URL.
"""
from __future__ import annotations

import logging
from typing import Any

from modules.intel30._intel_base import (
    DEGRADED,
    LIVE,
    get_json,
    get_text,
    log_call,
    set_source_state,
)

log = logging.getLogger(__name__)

SOURCE = "hypetrad"

# Candidate endpoints to probe (cheapest first)
_JSON_CANDIDATES = [
    "https://api.hypetrad.com/leaderboard?limit=10",
    "https://hypetrad.com/api/leaderboard?limit=10",
    "https://hypetrad.io/api/leaderboard?limit=10",
]
_HTML_CANDIDATES = [
    "https://hypetrad.com/",
    "https://hypetrad.io/",
]


async def fetch_all() -> dict[str, Any]:
    # Try JSON endpoints first
    for url in _JSON_CANDIDATES:
        data, meta = await get_json(SOURCE, url, timeout=8.0, retries=0)
        if data and isinstance(data, (list, dict)):
            log_call(SOURCE, LIVE, meta["latency_ms"], meta["bytes"], 200, url)
            return {"series": _normalise(data), "_global_error": None, "_endpoint": url}

    # Fallback: HTML head probe → mark SPA_DEGRADED
    for url in _HTML_CANDIDATES:
        text, meta = await get_text(SOURCE, url, timeout=8.0)
        if text and "<html" in text[:500].lower():
            log_call(SOURCE, DEGRADED, meta["latency_ms"], meta["bytes"], 200, "spa_html_only")
            set_source_state(SOURCE, DEGRADED)
            return {
                "_global_error": "spa_html_only — no public JSON endpoint",
                "_link": url,
                "_status": DEGRADED,
                "series": [],
            }

    return {"_global_error": "all candidates failed", "series": []}


def _normalise(data: Any) -> list[dict[str, Any]]:
    """Best-effort normalisation of unknown JSON shape into label/valor rows."""
    if isinstance(data, list):
        rows = data[:10]
    elif isinstance(data, dict):
        for key in ("data", "leaderboard", "traders", "results"):
            if isinstance(data.get(key), list):
                rows = data[key][:10]
                break
        else:
            rows = []
    else:
        rows = []
    out: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        addr = r.get("address") or r.get("trader") or r.get("user") or "?"
        pnl = r.get("pnl") or r.get("realised_pnl") or r.get("total_pnl") or r.get("equity") or 0
        try:
            pnl_f = float(pnl)
        except (TypeError, ValueError):
            pnl_f = 0.0
        out.append({"label": str(addr)[:8], "valor": pnl_f, "_error": None})
    return out


def format_for_telegram(data: dict[str, Any]) -> str:
    lines = ["📈 *HypeTrad — top traders*"]
    if data.get("_global_error"):
        lines.append(f"  ⚠️ {data['_global_error']}")
        if data.get("_link"):
            lines.append(f"  → {data['_link']}")
        return "\n".join(lines)
    rows = data.get("series", []) or []
    if not rows:
        lines.append("  ⚠️ no data")
        return "\n".join(lines)
    for r in rows[:10]:
        if not isinstance(r, dict) or r.get("_error"):
            continue
        addr = r.get("label", "?")
        pnl = r.get("valor", 0.0)
        try:
            pnl_str = f"${pnl:+,.0f}"
        except (ValueError, TypeError):
            pnl_str = str(pnl)
        lines.append(f"  • {addr}…: {pnl_str}")
    return "\n".join(lines)
