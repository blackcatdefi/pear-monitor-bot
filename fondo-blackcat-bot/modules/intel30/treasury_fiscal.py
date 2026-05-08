"""US Treasury Fiscal Data API — no key (R-PERFECT Sub-2 #1).

Endpoint: https://api.fiscaldata.treasury.gov/services/api/fiscal_service
Documentation: https://fiscaldata.treasury.gov/api-documentation/

Tracks: total public debt outstanding (TPD), debt held by public, intragov holdings.
Macro signal: deficit pace, debt ceiling proximity.
"""
from __future__ import annotations

import logging
from typing import Any

from modules.intel30._intel_base import LIVE, get_json, log_call

log = logging.getLogger(__name__)

BASE = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service"
SOURCE = "treasury_fiscal"


async def fetch_debt() -> dict[str, Any]:
    url = f"{BASE}/v2/accounting/od/debt_to_penny"
    data, meta = await get_json(
        SOURCE, url,
        params={"sort": "-record_date", "page[size]": 1},
        timeout=10.0,
    )
    if not data:
        return {"_error": meta.get("reason", "fetch_failed")}
    rows = data.get("data") or []
    if not rows:
        return {"_error": "empty"}
    row = rows[0]
    try:
        return {
            "fecha": row.get("record_date"),
            "total_debt_usd": float(row.get("tot_pub_debt_out_amt", 0)),
            "debt_held_public_usd": float(row.get("debt_held_public_amt", 0)),
            "intragov_usd": float(row.get("intragov_hold_amt", 0)),
            "_error": None,
        }
    except (TypeError, ValueError) as e:
        return {"_error": f"parse: {e}"}


async def fetch_all() -> dict[str, Any]:
    debt = await fetch_debt()
    if debt.get("_error"):
        return {"_global_error": debt["_error"], "series": []}
    log_call(SOURCE, LIVE, 0, 0, 200, "")
    return {
        "series": [
            {"label": "total_public_debt", "valor": debt["total_debt_usd"], "fecha": debt["fecha"], "_error": None},
            {"label": "debt_held_public", "valor": debt["debt_held_public_usd"], "fecha": debt["fecha"], "_error": None},
            {"label": "intragov_holdings", "valor": debt["intragov_usd"], "fecha": debt["fecha"], "_error": None},
        ],
        "_global_error": None,
    }


def _trillions(val: float) -> str:
    return f"${val / 1e12:.2f}T"


def format_for_telegram(data: dict[str, Any]) -> str:
    lines = ["💵 *Treasury Fiscal — US Public Debt*"]
    if data.get("_global_error"):
        lines.append(f"  ⚠️ {data['_global_error']}")
        return "\n".join(lines)
    series = data.get("series", []) or []
    by_label = {s.get("label"): s for s in series if isinstance(s, dict)}
    fecha = ""
    tpd = by_label.get("total_public_debt", {})
    dhp = by_label.get("debt_held_public", {})
    ig = by_label.get("intragov_holdings", {})
    if tpd:
        fecha = tpd.get("fecha", "")
        lines.append(f"  • Total: {_trillions(tpd.get('valor', 0))} ({fecha})")
    if dhp:
        lines.append(f"  • Held by public: {_trillions(dhp.get('valor', 0))}")
    if ig:
        lines.append(f"  • Intragov: {_trillions(ig.get('valor', 0))}")
    return "\n".join(lines)
