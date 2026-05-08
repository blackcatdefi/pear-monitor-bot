"""BTC + ETH treasury aggregator (R-PERFECT Sub-3 #4).

Bundles 3 sources behind one module:
  • bitcointreasuries.net (corporate BTC holders)
  • bitcoinminingstock.io (miners + market cap)
  • strategicethreserve.xyz (corporate ETH holders)

All three are SPA dashboards as of 2026-05-08 — module probes likely JSON
backends and falls back to graceful link when not exposed.
"""
from __future__ import annotations

import logging
from typing import Any

from modules.intel30._intel_base import DEGRADED, LIVE, get_json, get_text, log_call, set_source_state

log = logging.getLogger(__name__)

SOURCE = "treasuries_bundle"

_PROBES = [
    ("bitcoin_treasuries", [
        "https://bitcointreasuries.net/api/treasuries.json",
        "https://api.bitcointreasuries.net/v1/treasuries",
    ], "https://bitcointreasuries.net/"),
    ("bitcoin_mining_stock", [
        "https://bitcoinminingstock.io/api/miners.json",
        "https://api.bitcoinminingstock.io/v1/miners",
    ], "https://bitcoinminingstock.io/"),
    ("eth_strategic_reserve", [
        "https://strategicethreserve.xyz/api/holdings.json",
        "https://api.strategicethreserve.xyz/v1/holdings",
    ], "https://strategicethreserve.xyz/"),
]


async def _try_probes(name: str, candidates: list[str], fallback_url: str) -> dict[str, Any]:
    for url in candidates:
        data, meta = await get_json(SOURCE, url, timeout=8.0, retries=0)
        if data:
            log_call(SOURCE, LIVE, meta["latency_ms"], meta["bytes"], 200, f"{name} live")
            count = len(data) if isinstance(data, list) else len(data.get("data", []) or [])
            return {"label": name, "valor": count, "_link": url, "_error": None}
    # HTML fallback
    text, meta = await get_text(SOURCE, fallback_url, timeout=8.0)
    if text:
        return {"label": name, "_link": fallback_url, "_error": "spa_html_only"}
    return {"label": name, "_error": "unreachable"}


async def fetch_all() -> dict[str, Any]:
    series = []
    any_live = False
    for name, candidates, fallback in _PROBES:
        s = await _try_probes(name, candidates, fallback)
        series.append(s)
        if not s.get("_error"):
            any_live = True
    if not any_live:
        set_source_state(SOURCE, DEGRADED)
        return {
            "_global_error": "all 3 dashboards SPA-only",
            "_status": DEGRADED,
            "series": series,
        }
    log_call(SOURCE, LIVE, 0, 0, 200, "bundle")
    return {"series": series, "_global_error": None}


def format_for_telegram(data: dict[str, Any]) -> str:
    lines = ["🏛 *Treasuries — BTC+ETH+Miners*"]
    if data.get("_global_error") and not data.get("series"):
        lines.append(f"  ⚠️ {data['_global_error']}")
        return "\n".join(lines)
    for s in data.get("series", []):
        if not isinstance(s, dict):
            continue
        lab = s.get("label", "?")
        if s.get("_error"):
            link = s.get("_link", "")
            lines.append(f"  • {lab}: ⚠️ {s['_error']} → {link}")
            continue
        valor = s.get("valor", 0)
        lines.append(f"  • {lab}: {valor} entries")
    return "\n".join(lines)
