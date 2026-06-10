"""Hyperliquid Official Info API — expansion module (R-INTEL30 Phase 1 #1).

Adds first-party data the existing bot does NOT already pull:
    - perpDexs                : HIP-3 deployer registry (Trade[XYZ], hyENA, Dreamcash, etc.)
    - predictedFundings        : next-cycle funding for every perp
    - clearinghouseState (vault): vault state (HLP, AF buyback math support)

Endpoint: POST https://api.hyperliquid.xyz/info
Rate limit: 1,200 weight/min (REST). No key.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

API_URL = "https://api.hyperliquid.xyz/info"
HTTP_TIMEOUT = 8.0


async def _post(payload: dict[str, Any]) -> Any:
    # R-BOT-DEFINITIVE WI-4: route through the SHARED rate-limited + cached HL
    # client (kills the 429s on perpDexs / predictedFundings during /reporte).
    try:
        from modules.hl_client import post_info
        return await post_info(payload)
    except ImportError:  # pragma: no cover — isolated import contexts
        pass
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        r = await client.post(API_URL, json=payload, headers={"Content-Type": "application/json"})
        r.raise_for_status()
        return r.json()


async def fetch_perp_dexs() -> dict[str, Any]:
    """HIP-3 deployer registry. Returns {dexs: [...], _error: None}."""
    try:
        data = await _post({"type": "perpDexs"})
        if not isinstance(data, list):
            return {"dexs": [], "_error": f"unexpected_type:{type(data).__name__}"}
        # Filter to only dicts with name field (skip null-padding entries some endpoints return)
        clean = [d for d in data if isinstance(d, dict) and d.get("name")]
        return {"dexs": clean, "_error": None}
    except Exception as e:
        log.warning("hl_info perp_dexs fail: %s", e)
        return {"dexs": [], "_error": str(e)}


async def fetch_predicted_fundings() -> dict[str, Any]:
    """Next-cycle predicted funding rates. Returns {fundings: {coin: rate_pct_8h}, _error}."""
    try:
        data = await _post({"type": "predictedFundings"})
        # Format: list of [coin, [["venue", {"fundingRate": "0.00001234"}], ...]]
        # Or: dict {coin: {venue: {fundingRate}}} — handle both
        out: dict[str, dict[str, float]] = {}
        if isinstance(data, list):
            for entry in data:
                if not (isinstance(entry, list) and len(entry) >= 2):
                    continue
                coin, venues = entry[0], entry[1]
                if not isinstance(venues, list):
                    continue
                ven_map = {}
                for ven_entry in venues:
                    if isinstance(ven_entry, list) and len(ven_entry) >= 2:
                        v, info = ven_entry
                        if isinstance(info, dict) and "fundingRate" in info:
                            try:
                                ven_map[v] = float(info["fundingRate"]) * 100  # to %
                            except (TypeError, ValueError):
                                pass
                if ven_map:
                    out[coin] = ven_map
        elif isinstance(data, dict):
            for coin, venues in data.items():
                if isinstance(venues, dict):
                    ven_map = {}
                    for v, info in venues.items():
                        if isinstance(info, dict) and "fundingRate" in info:
                            try:
                                ven_map[v] = float(info["fundingRate"]) * 100
                            except (TypeError, ValueError):
                                pass
                    if ven_map:
                        out[coin] = ven_map
        return {"fundings": out, "_error": None}
    except Exception as e:
        log.warning("hl_info predicted_fundings fail: %s", e)
        return {"fundings": {}, "_error": str(e)}


async def fetch_vault_state(vault_addr: str) -> dict[str, Any]:
    """Vault state via clearinghouseState — used for HLP/AF buyback support."""
    try:
        data = await _post({"type": "clearinghouseState", "user": vault_addr})
        if not isinstance(data, dict):
            return {"state": {}, "_error": f"unexpected_type:{type(data).__name__}"}
        return {"state": data, "_error": None}
    except Exception as e:
        log.warning("hl_info vault_state fail: %s", e)
        return {"state": {}, "_error": str(e)}


async def fetch_all() -> dict[str, Any]:
    """Fan out for /reporte enrichment."""
    perp_task = asyncio.create_task(fetch_perp_dexs())
    fund_task = asyncio.create_task(fetch_predicted_fundings())
    perp = await perp_task
    fund = await fund_task
    return {"perp_dexs": perp, "predicted_fundings": fund}


def format_for_telegram(data: dict[str, Any]) -> str:
    pd_data = data.get("perp_dexs") or {}
    fd_data = data.get("predicted_fundings") or {}

    lines = ["🟣 *HL Info API — HIP-3 + Predicted Fundings*"]

    dexs = pd_data.get("dexs") or []
    if pd_data.get("_error"):
        # WI-9e: ONE short line on failure — no error fragments.
        lines.append("  ⚠️ perpDexs: fuente no disponible este run")
    elif dexs:
        lines.append(f"  • HIP-3 deployers activos: *{len(dexs)}*")
        for d in dexs[:8]:
            nm = d.get("name", "?")
            full = d.get("fullName", "")[:40]
            lines.append(f"    – `{nm}` {full}")
        if len(dexs) > 8:
            lines.append(f"    … +{len(dexs) - 8} más")
    else:
        lines.append("  • HIP-3 deployers: (vacío)")

    fundings = fd_data.get("fundings") or {}
    if fd_data.get("_error"):
        lines.append("  ⚠️ predictedFundings: fuente no disponible este run")
    elif fundings:
        # show top 5 most extreme fundings on HL venue
        hl_funds = []
        for coin, venues in fundings.items():
            hl_rate = venues.get("HlPerp") or venues.get("HL")
            if hl_rate is not None:
                hl_funds.append((coin, hl_rate))
        hl_funds.sort(key=lambda x: abs(x[1]), reverse=True)
        if hl_funds:
            lines.append(f"  • Predicted fundings 8h (top 5 abs HL):")
            for coin, rate in hl_funds[:5]:
                sign = "+" if rate >= 0 else ""
                lines.append(f"    – {coin}: {sign}{rate:.4f}%")
    return "\n".join(lines)
