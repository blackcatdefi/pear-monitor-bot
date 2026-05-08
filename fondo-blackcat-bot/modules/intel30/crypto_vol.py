"""Crypto vol/liq trifecta — Coinalyze + Velo + Deribit DVOL (R-PERFECT Phase 3 #1).

  • Deribit DVOL: public, no key — primary IV index for BTC/ETH options
  • Coinalyze: COINALYZE_API_KEY env var (free tier signup) — agg liq + funding
  • Velo Data: VELO_API_KEY env var (paid) — institutional perp/options ladder

Surface: BTC/ETH DVOL (live no key), agg fundings (graceful), Velo basis (graceful).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from modules.intel30._intel_base import (
    GRACEFUL_NO_KEY,
    LIVE,
    get_json,
    log_call,
)

log = logging.getLogger(__name__)

SOURCE = "crypto_vol"

DERIBIT_BASE = "https://www.deribit.com/api/v2/public"
COINALYZE_BASE = "https://api.coinalyze.net/v1"
VELO_BASE = "https://api.velo.xyz/v1"

COINALYZE_KEY = os.getenv("COINALYZE_API_KEY", "").strip()
VELO_KEY = os.getenv("VELO_API_KEY", "").strip()


async def fetch_dvol(currency: str = "BTC") -> dict[str, Any]:
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - 7 * 86400 * 1000
    data, meta = await get_json(
        SOURCE, f"{DERIBIT_BASE}/get_volatility_index_data",
        params={
            "currency": currency,
            "start_timestamp": start_ms,
            "end_timestamp": end_ms,
            "resolution": "1D",
        },
        timeout=10.0,
    )
    if not data or not isinstance(data, dict):
        return {"label": f"{currency}_DVOL", "_error": meta.get("reason", "fetch_failed")}
    rows = (data.get("result") or {}).get("data") or []
    if not rows:
        return {"label": f"{currency}_DVOL", "_error": "empty"}
    last = rows[-1]
    # Deribit shape: [ts, open, high, low, close]
    try:
        return {
            "label": f"{currency}_DVOL",
            "valor": float(last[4]),
            "open": float(last[1]),
            "_error": None,
        }
    except (TypeError, ValueError, IndexError) as e:
        return {"label": f"{currency}_DVOL", "_error": f"parse: {e}"}


async def fetch_coinalyze() -> dict[str, Any]:
    if not COINALYZE_KEY:
        return {
            "label": "Coinalyze_funding",
            "_error": "COINALYZE_API_KEY not set",
            "_signup_url": "coinalyze.net/account/api",
        }
    data, meta = await get_json(
        SOURCE, f"{COINALYZE_BASE}/funding-rate",
        headers={"api_key": COINALYZE_KEY},
        params={"symbols": "BTCUSD_PERP.A,ETHUSD_PERP.A"},
        timeout=10.0,
    )
    if not data:
        return {"label": "Coinalyze_funding", "_error": meta.get("reason", "fetch_failed")}
    return {"label": "Coinalyze_funding", "_payload": data, "_error": None}


async def fetch_velo() -> dict[str, Any]:
    if not VELO_KEY:
        return {
            "label": "Velo_basis",
            "_error": "VELO_API_KEY not set (paid)",
            "_signup_url": "velodata.app",
        }
    data, meta = await get_json(
        SOURCE, f"{VELO_BASE}/basis",
        headers={"Authorization": f"Bearer {VELO_KEY}"},
        params={"asset": "BTC"},
        timeout=10.0,
    )
    if not data:
        return {"label": "Velo_basis", "_error": meta.get("reason", "fetch_failed")}
    return {"label": "Velo_basis", "_payload": data, "_error": None}


async def fetch_all() -> dict[str, Any]:
    btc_dvol = await fetch_dvol("BTC")
    eth_dvol = await fetch_dvol("ETH")
    coina = await fetch_coinalyze()
    velo = await fetch_velo()
    series = [btc_dvol, eth_dvol, coina, velo]
    live = sum(1 for s in series if not s.get("_error"))
    log_call(SOURCE, LIVE if live > 0 else "DEGRADED", 0, 0, 200, f"{live}/4 live")
    return {"series": series, "_global_error": None if live > 0 else "all 4 failed"}


def format_for_telegram(data: dict[str, Any]) -> str:
    lines = ["📈 *Crypto vol — DVOL+Coinalyze+Velo*"]
    if data.get("_global_error"):
        lines.append(f"  ⚠️ {data['_global_error']}")
        return "\n".join(lines)
    for s in data.get("series", []):
        if not isinstance(s, dict):
            continue
        lab = s.get("label", "?")
        if s.get("_error"):
            sig = s.get("_signup_url", "")
            lines.append(f"  • {lab}: ⚠️ {s['_error']}")
            if sig:
                lines.append(f"      → {sig}")
            continue
        if "DVOL" in lab and "valor" in s:
            lines.append(f"  • {lab}: {s['valor']:.2f}")
        else:
            lines.append(f"  • {lab}: ✓")
    return "\n".join(lines)
