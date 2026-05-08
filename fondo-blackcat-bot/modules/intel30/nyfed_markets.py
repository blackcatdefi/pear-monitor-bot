"""NY Fed Markets API — SOFR + RRP + reference rates (R-PERFECT Sub-2 #2).

Base: https://markets.newyorkfed.org/api/...
No key required.

SOFR: rates/secured/sofr/last/{N}.json — N latest observations
EFFR: rates/unsecured/effr/last/{N}.json
OBFR: rates/unsecured/obfr/last/{N}.json

These rates feed into rate_monitor.py for HF threshold validation.
"""
from __future__ import annotations

import logging
from typing import Any

from modules.intel30._intel_base import LIVE, get_json, log_call

log = logging.getLogger(__name__)

BASE = "https://markets.newyorkfed.org/api"
SOURCE = "nyfed_markets"


async def fetch_one_rate(slug: str, label: str) -> dict[str, Any]:
    """slug = 'rates/secured/sofr/last/1.json' style path."""
    data, meta = await get_json(SOURCE, f"{BASE}/{slug}", timeout=10.0)
    if not data or not isinstance(data, dict):
        return {"label": label, "_error": meta.get("reason", "fetch_failed")}
    rates = data.get("refRates") or []
    if not rates:
        return {"label": label, "_error": "empty"}
    row = rates[0]
    try:
        return {
            "label": label,
            "valor": float(row.get("percentRate", 0)),
            "fecha": row.get("effectiveDate"),
            "volume_billion": row.get("volumeInBillions"),
            "_error": None,
        }
    except (TypeError, ValueError) as e:
        return {"label": label, "_error": str(e)[:50]}


async def fetch_all() -> dict[str, Any]:
    series = []
    for slug, label in [
        ("rates/secured/sofr/last/1.json", "SOFR"),
        ("rates/unsecured/effr/last/1.json", "EFFR"),
        ("rates/unsecured/obfr/last/1.json", "OBFR"),
    ]:
        s = await fetch_one_rate(slug, label)
        series.append(s)
    log_call(SOURCE, LIVE, 0, 0, 200, "")
    return {"series": series, "_global_error": None}


def format_for_telegram(data: dict[str, Any]) -> str:
    lines = ["🏦 *NY Fed Markets — Reference Rates*"]
    if data.get("_global_error"):
        lines.append(f"  ⚠️ {data['_global_error']}")
        return "\n".join(lines)
    for s in data.get("series", []):
        if not isinstance(s, dict):
            continue
        if s.get("_error"):
            lines.append(f"  • {s.get('label', '?')}: ⚠️ {s['_error']}")
            continue
        lab = s.get("label", "?")
        val = s.get("valor", 0.0)
        fecha = s.get("fecha", "")
        vol = s.get("volume_billion")
        line = f"  • {lab}: {val:.2f}% ({fecha})"
        if vol:
            line += f" vol ${vol}B"
        lines.append(line)
    return "\n".join(lines)
