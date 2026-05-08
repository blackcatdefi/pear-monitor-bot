"""CFTC Commitments of Traders — Friday 3:30pm ET (R-PERFECT Sub-2 #3).

Source: https://publicreporting.cftc.gov/resource/{dataset}.json (Socrata API)
Datasets:
  - 6dca-aqww: TFF (Traders in Financial Futures) for financials
  - jun7-fc8e: legacy COT all markets

No key required. Free Socrata throttle (~1000 req/hr/IP).
Surface: BTC futures positioning week-over-week (when included), gold, S&P.
"""
from __future__ import annotations

import logging
from typing import Any

from modules.intel30._intel_base import LIVE, get_json, log_call

log = logging.getLogger(__name__)

BASE = "https://publicreporting.cftc.gov/resource"
SOURCE = "cftc_cot"

# Trackable contracts (TFF dataset)
TRACKED_NAMES = [
    "BITCOIN",       # CME BTC
    "MICRO BITCOIN",
    "E-MINI S&P 500",
    "ETHER",         # CME ETH
    "GOLD",
    "U.S. DOLLAR INDEX",
]


async def fetch_latest_per_contract(name: str) -> dict[str, Any]:
    url = f"{BASE}/6dca-aqww.json"
    # Socrata SoQL: filter by partial name
    where = f"upper(market_and_exchange_names) like '%{name.upper()}%'"
    data, meta = await get_json(
        SOURCE, url,
        params={
            "$where": where,
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": 1,
        },
        timeout=10.0,
    )
    if not data or not isinstance(data, list) or not data:
        return {"label": name, "_error": meta.get("reason", "empty")}
    row = data[0]
    try:
        return {
            "label": name,
            "fecha": (row.get("report_date_as_yyyy_mm_dd") or "")[:10],
            "long_dealer": _f(row.get("dealer_positions_long_all")),
            "short_dealer": _f(row.get("dealer_positions_short_all")),
            "long_levfunds": _f(row.get("lev_money_positions_long")),
            "short_levfunds": _f(row.get("lev_money_positions_short")),
            "_error": None,
        }
    except Exception as e:  # noqa: BLE001
        return {"label": name, "_error": str(e)[:50]}


def _f(v: Any) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


async def fetch_all() -> dict[str, Any]:
    series = []
    for name in TRACKED_NAMES:
        s = await fetch_latest_per_contract(name)
        series.append(s)
    log_call(SOURCE, LIVE, 0, 0, 200, f"{len(TRACKED_NAMES)} contracts")
    return {"series": series, "_global_error": None}


def format_for_telegram(data: dict[str, Any]) -> str:
    lines = ["📑 *CFTC COT — TFF positioning*"]
    if data.get("_global_error"):
        lines.append(f"  ⚠️ {data['_global_error']}")
        return "\n".join(lines)
    rendered = 0
    for s in data.get("series", []):
        if not isinstance(s, dict) or s.get("_error"):
            continue
        lab = s.get("label", "?")
        net_lev = s.get("long_levfunds", 0) - s.get("short_levfunds", 0)
        net_dealer = s.get("long_dealer", 0) - s.get("short_dealer", 0)
        fecha = s.get("fecha", "")
        lines.append(f"  • {lab} ({fecha}):")
        lines.append(f"      lev_funds net: {net_lev:+,.0f}  · dealer net: {net_dealer:+,.0f}")
        rendered += 1
    if rendered == 0:
        lines.append("  ⚠️ no rows")
    return "\n".join(lines)
