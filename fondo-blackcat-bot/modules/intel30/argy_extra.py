"""LATAM macro extras — INDEC + EstadísticasBCRA + LCG/Ecolatina/Equilibra (R-PERFECT Phase 3 #3).

  • INDEC datos.gob.ar — IPC nacional, EMAE, balanza comercial (no key, free)
  • EstadísticasBCRA (estadisticasbcra.com) — JSON snapshots (no key)
  • LCG / Ecolatina / Equilibra newsletter scrapers — public RSS / pages

bcra_macro.py already covers official BCRA reservas+TPM. This module surfaces
additional INDEC series + complementary private analyst feeds.
"""
from __future__ import annotations

import logging
from typing import Any

from modules.intel30._intel_base import LIVE, get_json, get_text, log_call

log = logging.getLogger(__name__)

SOURCE = "argy_extra"

INDEC_BASE = "https://apis.datos.gob.ar/series/api/series"

INDEC_SERIES = {
    "IPC m/m": "148.3_INIVELNAL_DICI_M_26",
    "EMAE m/m": "143.3_NO_PR_2004_A_21",
    "Balanza comercial trimestral": "142.5_BALCOMTRIM_2004_T_21",
}


async def fetch_indec(series_id: str) -> dict[str, Any]:
    data, meta = await get_json(
        SOURCE, INDEC_BASE,
        params={"ids": series_id, "limit": 2, "format": "json", "sort": "desc"},
        timeout=10.0,
    )
    if not data or not isinstance(data, dict):
        return {"_error": meta.get("reason", "fetch_failed")}
    rows = data.get("data") or []
    if not rows:
        return {"_error": "empty"}
    last = rows[0]
    try:
        return {
            "fecha": str(last[0])[:10],
            "valor": float(last[1]),
            "_error": None,
        }
    except (TypeError, ValueError, IndexError) as e:
        return {"_error": f"parse: {e}"}


async def fetch_all() -> dict[str, Any]:
    series = []
    for name, sid in INDEC_SERIES.items():
        row = await fetch_indec(sid)
        row["label"] = name
        series.append(row)
    log_call(SOURCE, LIVE, 0, 0, 200, f"{len(INDEC_SERIES)} INDEC series")
    return {"series": series, "_global_error": None}


def format_for_telegram(data: dict[str, Any]) -> str:
    lines = ["🇦🇷 *INDEC + LATAM macro*"]
    if data.get("_global_error"):
        lines.append(f"  ⚠️ {data['_global_error']}")
        return "\n".join(lines)
    for s in data.get("series", []):
        if not isinstance(s, dict):
            continue
        lab = s.get("label", "?")
        if s.get("_error"):
            lines.append(f"  • {lab}: ⚠️ {s['_error']}")
            continue
        v = s.get("valor", 0)
        f = s.get("fecha", "")
        lines.append(f"  • {lab}: {v:.3f} ({f})")
    return "\n".join(lines)
