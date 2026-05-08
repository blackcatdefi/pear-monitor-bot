"""Visa Onchain Analytics — stablecoin transaction volumes (R-PERFECT Sub-3 #3).

Public dashboard: https://usa.visa.com/solutions/crypto/onchain-analytics-dashboard.html
Backend JSON (research-pending): visaonchainanalytics.com/api/...

This module probes the Visa OCA backend at known + candidate endpoints.
Falls back to "data via web dashboard only" when none respond.
"""
from __future__ import annotations

import logging
from typing import Any

from modules.intel30._intel_base import DEGRADED, LIVE, get_json, get_text, log_call, set_source_state

log = logging.getLogger(__name__)

SOURCE = "visa_onchain"

_JSON_CANDIDATES = [
    "https://visaonchainanalytics.com/api/stablecoins/volume",
    "https://api.visaonchainanalytics.com/v1/stablecoins/volume",
]


async def fetch_all() -> dict[str, Any]:
    for url in _JSON_CANDIDATES:
        data, meta = await get_json(SOURCE, url, timeout=10.0, retries=0)
        if data and isinstance(data, (dict, list)):
            log_call(SOURCE, LIVE, meta["latency_ms"], meta["bytes"], 200, url)
            return _normalise(data, url)
    # Fallback HTML head
    text, meta = await get_text(
        SOURCE, "https://visaonchainanalytics.com/", timeout=10.0,
    )
    if text:
        log_call(SOURCE, DEGRADED, meta["latency_ms"], meta["bytes"], 200, "spa_html_only")
        set_source_state(SOURCE, DEGRADED)
        return {
            "_global_error": "spa_html_only — JSON backend not public",
            "_link": "https://visaonchainanalytics.com/",
            "_status": DEGRADED,
            "series": [],
        }
    return {"_global_error": "all probes failed", "series": []}


def _normalise(data: Any, src_url: str) -> dict[str, Any]:
    series: list[dict[str, Any]] = []
    if isinstance(data, list):
        items = data[:5]
    elif isinstance(data, dict):
        items = data.get("data") or data.get("series") or []
        if isinstance(items, list):
            items = items[:5]
        else:
            items = []
    else:
        items = []
    for it in items:
        if not isinstance(it, dict):
            continue
        label = it.get("name") or it.get("label") or "?"
        val = it.get("value") or it.get("volume_usd") or 0
        try:
            val_f = float(val)
        except (TypeError, ValueError):
            val_f = 0.0
        series.append({"label": str(label), "valor": val_f, "_error": None})
    return {"series": series, "_global_error": None, "_endpoint": src_url}


def format_for_telegram(data: dict[str, Any]) -> str:
    lines = ["💳 *Visa Onchain — stablecoin volumes*"]
    if data.get("_global_error"):
        lines.append(f"  ⚠️ {data['_global_error']}")
        if data.get("_link"):
            lines.append(f"  → {data['_link']}")
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
