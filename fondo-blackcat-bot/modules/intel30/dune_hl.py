"""Dune Analytics — top 5 HyperLiquid dashboards (R-PERFECT Sub-1 #3).

API: https://api.dune.com/api/v1/query/{queryId}/results
Free tier: 25 reqs/day, 1000 rows/result. Sufficient for 5 query polls/day.

DUNE_API_KEY required. Module degrades gracefully if missing.
Curated query IDs for HyperLiquid intel are configurable via env DUNE_HL_QUERY_IDS
(comma-separated). Falls back to 0 queries if unset, returning "no queries configured".
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

API_KEY = os.getenv("DUNE_API_KEY", "").strip()
QUERY_IDS_RAW = os.getenv("DUNE_HL_QUERY_IDS", "").strip()
SOURCE = "dune_hl"
BASE = "https://api.dune.com/api/v1"


def _query_ids() -> list[str]:
    if not QUERY_IDS_RAW:
        return []
    return [q.strip() for q in QUERY_IDS_RAW.split(",") if q.strip().isdigit()][:5]


async def fetch_one(qid: str) -> dict[str, Any]:
    url = f"{BASE}/query/{qid}/results"
    data, meta = await get_json(
        SOURCE, url,
        headers={"X-Dune-API-Key": API_KEY},
        params={"limit": 5},
        timeout=15.0,
    )
    if not data:
        return {"qid": qid, "_error": meta.get("reason", "unknown")}
    rows = (data.get("result") or {}).get("rows") or []
    return {"qid": qid, "rows_count": len(rows), "_first_row": rows[0] if rows else None, "_error": None}


async def fetch_all() -> dict[str, Any]:
    if not API_KEY:
        return graceful_no_key_payload(
            SOURCE,
            "https://dune.com/settings/api",
            "DUNE_API_KEY",
        )
    qids = _query_ids()
    if not qids:
        log_call(SOURCE, "DEGRADED", 0, 0, 0, "DUNE_HL_QUERY_IDS empty")
        return {
            "_global_error": "no queries configured",
            "_signup_url": "set DUNE_HL_QUERY_IDS=id1,id2,id3 in env",
            "series": [],
        }
    series: list[dict[str, Any]] = []
    for qid in qids:
        try:
            row = await fetch_one(qid)
            series.append(row)
        except Exception as e:  # noqa: BLE001
            series.append({"qid": qid, "_error": str(e)[:80]})
    log_call(SOURCE, LIVE, 0, 0, 200, f"{len(series)} queries")
    return {"series": series, "_global_error": None}


def format_for_telegram(data: dict[str, Any]) -> str:
    lines = ["📊 *Dune — HL dashboards*"]
    if data.get("_status") == GRACEFUL_NO_KEY:
        lines.append("  ⚠️ DUNE_API_KEY not set")
        lines.append("  → free: dune.com/settings/api")
        return "\n".join(lines)
    if data.get("_global_error"):
        lines.append(f"  ⚠️ {data['_global_error']}")
        if data.get("_signup_url"):
            lines.append(f"  → {data['_signup_url']}")
        return "\n".join(lines)
    rows = data.get("series", []) or []
    if not rows:
        lines.append("  ⚠️ no data")
        return "\n".join(lines)
    for r in rows:
        if not isinstance(r, dict):
            continue
        qid = r.get("qid", "?")
        if r.get("_error"):
            lines.append(f"  • q{qid}: ⚠️ {r['_error']}")
            continue
        cnt = r.get("rows_count", 0)
        lines.append(f"  • q{qid}: {cnt} rows")
    return "\n".join(lines)
