"""CriptoYa AR FX (R-INTEL30 Phase 1 #9).

Sole free real-time API for ALL parallel AR FX rates: blue, MEP, CCL, oficial,
mayorista, ahorro, tarjeta, cripto-USD arb across 20+ exchanges.

Endpoint: https://criptoya.com/api/dolar         (all FX)
          https://criptoya.com/api/USDT/ars/0.1  (USDT vs ARS arb across exchanges)
No key. Refresh ~30s.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

DOLAR_URL = "https://criptoya.com/api/dolar"
USDT_URL = "https://criptoya.com/api/USDT/ars/0.1"
HTTP_TIMEOUT = 8.0


async def fetch_fx() -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(DOLAR_URL)
            r.raise_for_status()
            data = r.json()
        # Expected format: {oficial: {ask, bid}, blue: {...}, mep: {al30: {ci, ...}}, ccl: {...}}
        if not isinstance(data, dict):
            return {"fx": {}, "_error": f"unexpected_type:{type(data).__name__}"}
        flat: dict[str, float] = {}
        for k, v in data.items():
            if isinstance(v, dict):
                # Try direct ask/price, then nested ci/24hs
                for price_key in ("ask", "price", "venta"):
                    if price_key in v and isinstance(v[price_key], (int, float)):
                        flat[k] = float(v[price_key])
                        break
                else:
                    # nested al30/gd30 → ci
                    for nested_k, nested_v in v.items():
                        if isinstance(nested_v, dict):
                            for price_key in ("ci", "ask", "price"):
                                if price_key in nested_v and isinstance(nested_v[price_key], (int, float)):
                                    flat[f"{k}_{nested_k}"] = float(nested_v[price_key])
                                    break
                            if k in flat:
                                break
        return {"fx": flat, "raw": data, "_error": None}
    except Exception as e:
        log.warning("criptoya fx fail: %s", e)
        return {"fx": {}, "_error": str(e)}


async def fetch_usdt_arb() -> dict[str, Any]:
    """USDT cross-exchange ARS arb — measures the effective cripto-USD price."""
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(USDT_URL)
            r.raise_for_status()
            data = r.json()
        if not isinstance(data, dict):
            return {"exchanges": {}, "_error": f"unexpected_type:{type(data).__name__}"}
        # data = {"binance": {"ask": 1234, "bid": 1230, ...}, ...}
        out: dict[str, dict[str, float]] = {}
        for ex, info in data.items():
            if isinstance(info, dict):
                ask = info.get("ask") or info.get("totalAsk")
                bid = info.get("bid") or info.get("totalBid")
                if isinstance(ask, (int, float)) and isinstance(bid, (int, float)):
                    out[ex] = {"ask": float(ask), "bid": float(bid)}
        return {"exchanges": out, "_error": None}
    except Exception as e:
        log.warning("criptoya usdt_arb fail: %s", e)
        return {"exchanges": {}, "_error": str(e)}


async def fetch_all() -> dict[str, Any]:
    fx_t = asyncio.create_task(fetch_fx())
    arb_t = asyncio.create_task(fetch_usdt_arb())
    fx = await fx_t
    arb = await arb_t
    return {"fx": fx, "arb": arb}


def format_for_telegram(data: dict[str, Any]) -> str:
    fx = (data.get("fx") or {}).get("fx") or {}
    arb = (data.get("arb") or {}).get("exchanges") or {}
    err_fx = (data.get("fx") or {}).get("_error")
    err_arb = (data.get("arb") or {}).get("_error")

    lines = ["🇦🇷 *CriptoYa — Brecha Cambiaria*"]
    if err_fx:
        lines.append(f"  ⚠️ FX err: {err_fx[:60]}")
    else:
        # Pick canonical: oficial, blue, mep_al30, ccl_al30, tarjeta, mayorista
        order = [
            ("oficial", "Oficial"),
            ("mayorista", "Mayorista (A3500)"),
            ("blue", "Blue"),
            ("mep", "MEP"),
            ("mep_al30", "MEP AL30"),
            ("ccl", "CCL"),
            ("ccl_al30", "CCL AL30"),
            ("tarjeta", "Tarjeta"),
            ("ahorro", "Ahorro"),
            ("cripto", "Cripto"),
        ]
        for k, label in order:
            v = fx.get(k)
            if isinstance(v, (int, float)):
                lines.append(f"  • {label}: ${v:,.0f}")
        # Brecha = (blue or ccl) / oficial - 1
        oficial = fx.get("mayorista") or fx.get("oficial")
        for k_alt, label in [("blue", "blue"), ("ccl", "CCL"), ("mep", "MEP")]:
            alt = fx.get(k_alt) or fx.get(f"{k_alt}_al30")
            if oficial and alt:
                brecha = (alt / oficial - 1) * 100
                lines.append(f"  • Brecha {label} vs oficial: {brecha:+.1f}%")

    if err_arb:
        lines.append(f"  ⚠️ USDT arb err: {err_arb[:60]}")
    elif arb:
        # avg cripto-USDT price
        bids = [v["bid"] for v in arb.values() if "bid" in v]
        asks = [v["ask"] for v in arb.values() if "ask" in v]
        if bids and asks:
            avg_bid = sum(bids) / len(bids)
            avg_ask = sum(asks) / len(asks)
            lines.append(f"  • USDT/ARS arb ({len(arb)} ex.): bid ${avg_bid:,.0f} / ask ${avg_ask:,.0f}")
    return "\n".join(lines)
