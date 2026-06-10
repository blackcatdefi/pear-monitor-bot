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

BASE = "https://api.bcra.gob.ar/estadisticas/v4.0"  # v3.0 deprecated 2026-02-28
HTTP_TIMEOUT = 12.0

# Canonical variable IDs per BCRA catalog. Subject to change — module degrades gracefully.
# R-BOT-DEFINITIVE WI-9a (2026-06-10): the v4.0 catalog was re-verified LIVE:
#   * id 5 is "Tipo de cambio mayorista de referencia" (A3500, ~$1,446/USD) —
#     the old mapping labelled it "Tasa Política Monetaria" → the bogus
#     "Tasa Politica Monetaria 1,446%" line.
#   * The policy-rate series (160/161) is DISCONTINUED (last datum 2025-07-10);
#     the active reference rate is TAMAR (id 44, bancos privados).
#   * id 4 is "Tipo de cambio minorista (promedio vendedor)".
TRACKED = {
    1: "Reservas Intl. (USD M)",
    15: "Base Monetaria ($M)",
    27: "Inflación mensual (%)",
    28: "Inflación interanual (%)",
    7: "BADLAR Bancos Privados (%)",
    44: "Tasa TAMAR Bancos Privados (%, ref. política)",
    5: "TC mayorista A3500 ($/USD)",
    4: "TC minorista ($/USD)",
}

# Interest-rate series: sanity bounds 0-200% — anything outside renders n/d
# and is logged (a mis-mapped FX series can never print as a rate again).
RATE_IDS = frozenset({7, 44})
RATE_BOUNDS = (0.0, 200.0)


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
    """Fetch latest value of a single BCRA variable.

    v4.0 response shape (since 2026-02-28):
        {"results": [{"idVariable": 1, "detalle": [{"fecha": "...", "valor": 123.45}]}]}
    Older v3.0 (deprecated) had {"results": [{"fecha": "...", "valor": ...}]}.
    Code accepts both shapes for forward/backward compatibility.
    """
    try:
        url = f"{BASE}/Monetarias/{var_id}?limit=1"
        data = await _get(url)
        results = data.get("results") if isinstance(data, dict) else None
        if results and len(results) > 0:
            entry = results[0]
            # v4.0 — values nested under "detalle"
            detalle = entry.get("detalle")
            if isinstance(detalle, list) and detalle:
                inner = detalle[0]
                return {
                    "id": var_id,
                    "name": TRACKED.get(var_id, f"var_{var_id}"),
                    "fecha": inner.get("fecha"),
                    "valor": inner.get("valor"),
                    "_error": None,
                }
            # v3.0 fallback shape
            if "valor" in entry or "fecha" in entry:
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

    # Group: tasa de referencia (TAMAR) first, BM/Reservas, FX, then inflación
    priority = [44, 7, 1, 15, 5, 4, 27, 28]
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
            # WI-9a sanity bounds: a rate series outside 0-200% is a parsing /
            # mapping error — print n/d and log, never a bogus 1,446% rate.
            if pid in RATE_IDS and not (RATE_BOUNDS[0] <= val <= RATE_BOUNDS[1]):
                log.warning(
                    "bcra var %s (%s) out of rate bounds: %s — rendering n/d",
                    pid, name, val,
                )
                lines.append(f"  • {name}: n/d (valor fuera de rango)")
                rendered += 1
                continue
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
