"""FRED — St. Louis Fed (R-INTEL30 Phase 1 #4).

Backbone for ALL US macro: SOFR, FEDFUNDS, CPI, DGS10, T10Y2Y, VIX (VIXCLS),
DXY (DTWEXBGS), WALCL (Fed B/S), RRPONTSYD (RRP), TGA proxy.

Endpoint: https://api.stlouisfed.org/fred/series/observations
Free key (env: FRED_API_KEY). 120 req/min, 100k obs/req.
Module degrades gracefully if FRED_API_KEY not set.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

API_KEY = os.getenv("FRED_API_KEY", "").strip()
BASE = "https://api.stlouisfed.org/fred/series/observations"
HTTP_TIMEOUT = 10.0

TRACKED = {
    "DGS10":      "10Y Treasury Yield (%)",
    "T10Y2Y":     "10Y-2Y Spread (%)",
    "DGS2":       "2Y Treasury (%)",
    "VIXCLS":     "VIX",
    "DTWEXBGS":   "DXY (broad)",
    "WALCL":      "Fed Balance Sheet ($M)",
    "RRPONTSYD":  "ON RRP ($M)",
    "SOFR":       "SOFR (%)",
    "FEDFUNDS":   "Fed Funds (%)",
}


async def fetch_series(series_id: str) -> dict[str, Any]:
    """Fetch latest observation of a FRED series."""
    if not API_KEY:
        return {"id": series_id, "name": TRACKED.get(series_id, series_id), "_error": "no_api_key"}
    try:
        params = {
            "series_id": series_id,
            "api_key": API_KEY,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 5,  # last 5 to find a non-NaN
        }
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(BASE, params=params)
            r.raise_for_status()
            data = r.json()
        obs = data.get("observations") or []
        # Find the latest non-NaN observation
        for o in obs:
            try:
                val = float(o.get("value"))
                return {
                    "id": series_id,
                    "name": TRACKED.get(series_id, series_id),
                    "fecha": o.get("date"),
                    "valor": val,
                    "_error": None,
                }
            except (TypeError, ValueError):
                continue
        return {"id": series_id, "name": TRACKED.get(series_id, series_id), "_error": "all_nan"}
    except Exception as e:
        log.warning("fred %s fail: %s", series_id, e)
        return {"id": series_id, "name": TRACKED.get(series_id, series_id), "_error": str(e)}


async def fetch_all() -> dict[str, Any]:
    if not API_KEY:
        return {"series": [], "_global_error": "FRED_API_KEY not set"}
    tasks = [fetch_series(sid) for sid in TRACKED]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out: list[dict[str, Any]] = []
    for r in results:
        if isinstance(r, Exception):
            out.append({"_error": str(r)})
        else:
            out.append(r)
    return {"series": out, "_global_error": None}


def format_for_telegram(data: dict[str, Any]) -> str:
    lines = ["🇺🇸 *FRED — Macro EE.UU.*"]
    if data.get("_global_error"):
        lines.append(f"  ⚠️ {data['_global_error']}")
        lines.append("  → Set FRED_API_KEY env var (free key at fred.stlouisfed.org)")
        return "\n".join(lines)
    series = data.get("series") or []
    by_id = {s.get("id"): s for s in series if isinstance(s, dict)}
    priority = ["VIXCLS", "DGS10", "T10Y2Y", "DGS2", "SOFR", "FEDFUNDS", "WALCL", "RRPONTSYD", "DTWEXBGS"]
    rendered = 0
    for sid in priority:
        s = by_id.get(sid)
        if not s or s.get("_error"):
            continue
        val = s.get("valor")
        name = s.get("name", sid)
        fecha = s.get("fecha", "")
        if isinstance(val, (int, float)):
            if abs(val) > 1000:
                lines.append(f"  • {name}: {val:,.0f} ({fecha})")
            else:
                lines.append(f"  • {name}: {val:.3f} ({fecha})")
            rendered += 1
    if rendered == 0:
        errs = [s.get("_error", "?")[:40] for s in series if isinstance(s, dict) and s.get("_error")]
        if errs:
            lines.append(f"  ⚠️ todas fallaron — {errs[0]}")
    return "\n".join(lines)
