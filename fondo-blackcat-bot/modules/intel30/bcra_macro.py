"""BCRA Banco Central de la República Argentina — Official API (R-INTEL30 Phase 1 #10).

Sole official source for AR macro: reservas internacionales (brutas/netas),
base monetaria, BADLAR, TAMAR, A3500.

Endpoint: https://api.bcra.gob.ar/estadisticas/v3.0/Monetarias
Variable list: https://api.bcra.gob.ar/estadisticas/v3.0/Monetarias
Specific series: /estadisticas/v3.0/Monetarias/{idVariable}
No key required. Daily ~6-7pm ART. Spanish.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE = "https://api.bcra.gob.ar/estadisticas/v3.0"
HTTP_TIMEOUT = 12.0

# Canonical variable IDs per BCRA catalog. Subject to change — module degrades gracefully.
TRACKED = {
    1: "Reservas Intl. (USD M)",
    15: "Base Monetaria ($M)",
    27: "Inflación mensual (%)",
    28: "Inflación interanual (%)",
    34: "TAMAR (%)",
    7: "BADLAR Bancos Privados (%)",
    5: "Tasa Política Monetaria (%)",
    4: "TC mayorista A3500 ($/USD)",
}


async def _get(url: str) -> Any:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "application/json",
    }
    # BCRA often serves with self-signed cert chain — verify=False as last resort fallback
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=headers) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.json()
    except (httpx.ConnectError, ssl_exc()) as e:
        log.info("bcra %s ssl-relax retry: %s", url, e)
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=headers, verify=False) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.json()


def ssl_exc():
    """Return ssl.SSLError class for hot-loading without import error if ssl missing."""
    try:
        import ssl
        return ssl.SSLError
    except ImportError:
        return Exception


async def fetch_variable(var_id: int) -> dict[str, Any]:
    """Fetch latest value of a single BCRA variable."""
    try:
        url = f"{BASE}/Monetarias/{var_id}?limit=1"
        data = await _get(url)
        # Format: {"results": [{"idVariable": 1, "fecha": "2026-05-07", "valor": 22500.5}]}
        results = data.get("results") if isinstance(data, dict) else None
        if results and len(results) > 0:
            entry = results[0]
            return {
                "id": var_id,
                "name": TRACKED.get(var_id, f"var_{var_id}"),
                "fecha": entry.get("fecha"),
                "valor": entry.get("valor"),
                "_error": None,
            }
        return {"id": var_id, "name": TRACKED.get(var_id, f"var_{var_id}"), "_error": "empty"}
    except Exception as e:
        log.warning("bcra var %s fail: %s", var_id, e)
        return {"id": var_id, "name": TRACKED.get(var_id, f"var_{var_id}"), "_error": str(e)}


async def fetch_all() -> dict[str, Any]:
    """Pull all tracked variables in parallel."""
    tasks = [fetch_variable(vid) for vid in TRACKED]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out: list[dict[str, Any]] = []
    for r in results:
        if isinstance(r, Exception):
            out.append({"_error": str(r)})
        else:
            out.append(r)
    return {"variables": out}


def format_for_telegram(data: dict[str, Any]) -> str:
    vars_ = data.get("variables") or []
    lines = ["🏦 *BCRA — Macro Argentina*"]
    if not vars_:
        return "\n".join(lines + ["  ⚠️ sin datos"])

    # Group: tasa de política first, BM/Reservas, then inflación
    priority = [5, 1, 15, 4, 27, 28, 34, 7]
    by_id = {v.get("id"): v for v in vars_ if isinstance(v, dict)}
    rendered = 0
    for pid in priority:
        v = by_id.get(pid)
        if not v or v.get("_error"):
            continue
        val = v.get("valor")
        name = v.get("name", "?")
        fecha = v.get("fecha", "")
        if isinstance(val, (int, float)):
            # Format M numbers (>1000) with commas; else %
            if abs(val) > 1000:
                lines.append(f"  • {name}: {val:,.0f} ({fecha})")
            else:
                lines.append(f"  • {name}: {val:.2f} ({fecha})")
            rendered += 1
    if rendered == 0:
        errs = [v.get("_error", "?")[:40] for v in vars_ if isinstance(v, dict) and v.get("_error")]
        lines.append(f"  ⚠️ todas las variables fallaron: {errs[0] if errs else '?'}")
    return "\n".join(lines)
