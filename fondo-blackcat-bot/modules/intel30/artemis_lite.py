"""Artemis Terminal Lite — public asset metrics (R-PERFECT Sub-3 #2).

Endpoint pattern: https://api.artemis.xyz/asset/{symbol}/metrics
ARTEMIS_API_KEY required. Free tier signup: app.artemis.xyz/account.

Tracks: HYPE/SOL/BTC/ETH daily fees + active addresses + revenue.
Module degrades gracefully if key missing.
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

API_KEY = os.getenv("ARTEMIS_API_KEY", "").strip()
BASE = "https://api.artemis.xyz"
SOURCE = "artemis_lite"

TRACKED_ASSETS = ["bitcoin", "ethereum", "solana", "hyperliquid"]


async def fetch_one(asset: str) -> dict[str, Any]:
    url = f"{BASE}/asset/{asset}/metrics"
    headers = {"X-API-Key": API_KEY}
    data, meta = await get_json(SOURCE, url, headers=headers, timeout=10.0)
    if not data or not isinstance(data, dict):
        return {"label": asset, "_error": meta.get("reason", "fetch_failed")}
    metrics = data.get("data") or data.get("metrics") or {}
    daily_fees = metrics.get("fees") or metrics.get("daily_fees") or 0
    daa = metrics.get("dau") or metrics.get("daily_active_users") or 0
    rev = metrics.get("revenue") or 0
    try:
        return {
            "label": asset,
            "fees": float(daily_fees) if daily_fees else 0.0,
            "dau": int(daa) if daa else 0,
            "revenue": float(rev) if rev else 0.0,
            "_error": None,
        }
    except (TypeError, ValueError) as e:
        return {"label": asset, "_error": str(e)[:50]}


async def fetch_all() -> dict[str, Any]:
    if not API_KEY:
        return graceful_no_key_payload(
            SOURCE,
            "https://app.artemis.xyz/account",
            "ARTEMIS_API_KEY",
        )
    series = []
    for asset in TRACKED_ASSETS:
        try:
            s = await fetch_one(asset)
            series.append(s)
        except Exception as e:  # noqa: BLE001
            series.append({"label": asset, "_error": str(e)[:60]})
    log_call(SOURCE, LIVE, 0, 0, 200, f"{len(series)} assets")
    return {"series": series, "_global_error": None}


def format_for_telegram(data: dict[str, Any]) -> str:
    lines = ["🛰 *Artemis Terminal — chain metrics*"]
    if data.get("_status") == GRACEFUL_NO_KEY:
        lines.append("  ⚠️ ARTEMIS_API_KEY not set")
        lines.append("  → app.artemis.xyz/account")
        return "\n".join(lines)
    if data.get("_global_error"):
        lines.append(f"  ⚠️ {data['_global_error']}")
        return "\n".join(lines)
    for s in data.get("series", []):
        if not isinstance(s, dict) or s.get("_error"):
            continue
        lab = s.get("label", "?")
        fees = s.get("fees", 0)
        dau = s.get("dau", 0)
        lines.append(f"  • {lab}: fees ${fees:,.0f} · DAU {dau:,}")
    return "\n".join(lines)
