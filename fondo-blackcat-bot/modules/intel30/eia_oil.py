"""EIA Open Data API — Oil/Gas (R-INTEL30 Phase 1 #7).

Weekly Petroleum Status Report (WPSR), released Wednesdays 10:30 ET.
Markets move on this print (especially crude oil inventories).

Endpoint: https://api.eia.gov/v2/petroleum/stoc/wstk/data
Free key (env: EIA_API_KEY). 5,000 rows/req max.
Module degrades gracefully if EIA_API_KEY not set.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

API_KEY = os.getenv("EIA_API_KEY", "").strip()
BASE = "https://api.eia.gov/v2"
HTTP_TIMEOUT = 12.0

# Crude oil stocks weekly (excluding SPR), Cushing, gasoline, distillate
WPSR_SERIES = {
    "WCESTUS1": "Crude Oil Stocks (kbbl)",
    "W_EPC0_SAX_NUS_MBBL": "Crude (alt id)",
    "WCRSTUS1": "Crude SPR",
    "WGTSTUS1": "Total Gasoline (kbbl)",
    "WDISTUS1": "Total Distillate (kbbl)",
}


async def fetch_wpsr() -> dict[str, Any]:
    """Latest WPSR observation across the canonical series IDs."""
    if not API_KEY:
        return {"series": [], "_global_error": "EIA_API_KEY not set"}
    out: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        for sid, label in WPSR_SERIES.items():
            try:
                # EIA v2 generic series fetcher
                params = {
                    "api_key": API_KEY,
                    "frequency": "weekly",
                    "data[0]": "value",
                    "facets[series][]": sid,
                    "sort[0][column]": "period",
                    "sort[0][direction]": "desc",
                    "length": 1,
                }
                r = await client.get(f"{BASE}/petroleum/stoc/wstk/data", params=params)
                if r.status_code != 200:
                    out.append({"id": sid, "label": label, "_error": f"http_{r.status_code}"})
                    continue
                resp = r.json()
                rows = (resp.get("response") or {}).get("data") or []
                if not rows:
                    out.append({"id": sid, "label": label, "_error": "empty"})
                    continue
                row = rows[0]
                v = row.get("value")
                period = row.get("period")
                try:
                    val = float(v)
                except (TypeError, ValueError):
                    out.append({"id": sid, "label": label, "_error": "nan"})
                    continue
                out.append({"id": sid, "label": label, "fecha": period, "valor": val, "_error": None})
            except Exception as e:
                out.append({"id": sid, "label": label, "_error": str(e)})
    return {"series": out, "_global_error": None}


async def fetch_all() -> dict[str, Any]:
    return await fetch_wpsr()


def format_for_telegram(data: dict[str, Any]) -> str:
    lines = ["🛢 *EIA — WPSR Crude/Gas*"]
    if data.get("_global_error"):
        lines.append(f"  ⚠️ {data['_global_error']}")
        lines.append("  → Set EIA_API_KEY env var (free at api.eia.gov)")
        return "\n".join(lines)
    series = data.get("series") or []
    rendered = 0
    for s in series:
        if not isinstance(s, dict) or s.get("_error"):
            continue
        label = s.get("label", "?")
        val = s.get("valor")
        fecha = s.get("fecha", "")
        if isinstance(val, (int, float)):
            if abs(val) > 10000:
                lines.append(f"  • {label}: {val/1000:,.1f}M ({fecha})")
            else:
                lines.append(f"  • {label}: {val:,.0f} ({fecha})")
            rendered += 1
    if rendered == 0:
        errs = [s.get("_error", "?")[:40] for s in series if isinstance(s, dict) and s.get("_error")]
        lines.append(f"  ⚠️ todas fallaron — {errs[0] if errs else '?'}")
    return "\n".join(lines)
