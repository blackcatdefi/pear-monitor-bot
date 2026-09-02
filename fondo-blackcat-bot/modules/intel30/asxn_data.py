"""ASXN — buyback / burn / staking de HYPE (R-INTEL30 #2).

R-BOT-DEFINITIVE Fase 4.1 — ARREGLADA, y con un detalle que importa.
--------------------------------------------------------------------
El modulo viejo imprimia "ASXN: fuente no disponible este run" todos los dias
por dos motivos encadenados:

  1. Probaba endpoints /api/... contra data.asxn.xyz, que es solo el frontend.
     Los seis daban 404 o el shell HTML.
  2. Como fallback intentaba leer el snapshot de Next.js (/_next/data/{buildId})
     y despues scrapear el HTML. data.asxn.xyz NO es Next.js: es una SPA de
     create-react-app cuyo index.html pesa 1 KB y no tiene ni un numero. El
     scrape "funcionaba" perfectamente sobre una pagina vacia.

Los datos salen de otro host: **api-data.asxn.xyz**, que el propio bundle
declara. Rutas usadas aca (todas publicas, sin clave):

    GET /api/hype-burn/metrics      supply, burn 24h/7d/all-time, burn rate
    GET /api/hype-staking/metrics   stake total, yield, % staked, validadores
    GET /api/data/hl-buybacks       serie diaria de buybacks del AF

Y ACA ESTA LA PARTE QUE NO SE PUEDE OMITIR
------------------------------------------
No todos los datasets de ASXN estan frescos. Verificado el 2026-09-01:
hl-buybacks llega a ayer, pero /api/data/hl-auctions quedo congelado el
2026-03-05 y los espejos de flujos ETF el 2026-03-23. Todos responden 200 con
JSON impecable. Un dataset viejo de cinco meses que devuelve 200 es
exactamente la clase de falla que esta ronda existe para hacer imposible: no
rompe nada, no loguea nada, y pone un numero de marzo en el reporte de
septiembre.

Por eso cada bloque viaja con su propia fecha (`as_of`) y su antiguedad en
dias, y el formateador MARCA lo viejo en vez de imprimirlo como si fuera de
hoy. Las subastas se dejaron de leer de aca: las sirve hypurrscan, fresco.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://api-data.asxn.xyz"
DASHBOARD_BASE = "https://data.asxn.xyz"
DASHBOARD = "https://data.asxn.xyz/dashboard/hype"  # alias historico (tests)
HTTP_TIMEOUT = 15.0
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "Chrome/124.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json",
    "Referer": f"{DASHBOARD_BASE}/",
    "Origin": DASHBOARD_BASE,
}

PATH_BURN = "/api/hype-burn/metrics"
PATH_STAKING = "/api/hype-staking/metrics"
PATH_BUYBACKS = "/api/data/hl-buybacks"

# Un dataset con mas dias que esto se marca RANCIO en el reporte.
STALE_DAYS = 3


def _dias_desde(iso: Any) -> int | None:
    """Antiguedad en dias de una fecha ISO de ASXN ('2026-08-31T00:00:00.000Z')."""
    if not iso:
        return None
    txt = str(iso).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        try:
            dt = datetime.strptime(str(iso)[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).days


async def _get_json(client: httpx.AsyncClient, path: str) -> tuple[Any, str | None]:
    """(payload, error). Un fallo NUNCA se convierte en {} ni en []."""
    try:
        r = await client.get(f"{API_BASE}{path}")
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}@{path}: {str(exc)[:60]}"
    if r.status_code != 200:
        return None, f"http_{r.status_code}@{path}"
    try:
        return r.json(), None
    except Exception:  # noqa: BLE001
        return None, f"non_json@{path}"


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def fetch_hype_stats() -> dict[str, Any]:
    """Metricas de burn, staking y buyback de HYPE, cada una con su fecha."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=HEADERS,
                                 follow_redirects=True) as client:
        burn_raw, e1 = await _get_json(client, PATH_BURN)
        stake_raw, e2 = await _get_json(client, PATH_STAKING)
        bb_raw, e3 = await _get_json(client, PATH_BUYBACKS)

    errs = [e for e in (e1, e2, e3) if e]
    if burn_raw is None and stake_raw is None and bb_raw is None:
        return {"source": API_BASE, "data": {},
                "_error": "; ".join(errs)[:200] or "all_paths_failed"}

    out: dict[str, Any] = {}

    # ── burn ────────────────────────────────────────────────────────────────
    if isinstance(burn_raw, dict):
        m = burn_raw.get("metrics")
        if isinstance(m, dict):
            out["total_supply"] = _f(m.get("todayTotalSupply"))
            out["burn_24h"] = _f(m.get("burn24h"))
            out["burn_7d"] = _f(m.get("burn7d"))
            out["burn_hype_total"] = _f(m.get("burnAllTime"))
            out["burn_rate_30d"] = _f(m.get("burnRate30dAvg"))
            out["burn_pct_anual"] = _f(m.get("annualBurnPercent7d"))
        else:
            errs.append("burn: 200 sin clave 'metrics'")
    elif burn_raw is not None:
        errs.append(f"burn: shape {type(burn_raw).__name__}, se esperaba dict")

    # ── staking ─────────────────────────────────────────────────────────────
    if isinstance(stake_raw, dict):
        out["stake_hype_total"] = _f(stake_raw.get("currentTotalStake"))
        out["stake_pct"] = _f(stake_raw.get("stakingPercentage"))
        out["stake_yield"] = _f(stake_raw.get("currentYield"))
        vals = stake_raw.get("activeValidators")
        out["validadores"] = vals if isinstance(vals, int) else (
            len(stake_raw.get("currentValidatorStakes") or [])
            if isinstance(stake_raw.get("currentValidatorStakes"), list) else None)
    elif stake_raw is not None:
        errs.append(f"staking: shape {type(stake_raw).__name__}, se esperaba dict")

    # ── buybacks del AF (serie diaria) ──────────────────────────────────────
    if isinstance(bb_raw, list) and bb_raw:
        filas = [r for r in bb_raw if isinstance(r, dict) and r.get("date")]
        filas.sort(key=lambda r: str(r["date"]))
        ult = filas[-1] if filas else None
        if ult:
            out["buyback_as_of"] = str(ult.get("date"))[:10]
            out["buyback_dias"] = _dias_desde(ult.get("date"))
            out["buyback_usd_dia"] = _f(ult.get("ntl"))
            out["buyback_hype_dia"] = _f(ult.get("sz"))
            out["buyback_px_dia"] = _f(ult.get("average_price"))
            total = sum(_f(r.get("ntl")) or 0.0 for r in filas)
            out["buyback_usd_total"] = total
            ult7 = filas[-7:]
            out["buyback_usd_7d"] = sum(_f(r.get("ntl")) or 0.0 for r in ult7)
    elif bb_raw is not None:
        errs.append(f"buybacks: shape {type(bb_raw).__name__}, se esperaba lista")

    if not out:
        return {"source": API_BASE, "data": {},
                "_error": "; ".join(errs)[:200] or "sin metricas legibles"}

    return {"source": API_BASE, "data": out,
            "_partial": "; ".join(errs)[:200] or None, "_error": None}


async def fetch_all() -> dict[str, Any]:
    return await fetch_hype_stats()


def err_corto(err: Any) -> str:
    """Una linea corta, sin URLs ni tracebacks (WI-9e)."""
    txt = re.sub(r"https?://\S*", "", str(err or "").strip())
    txt = re.sub(r"\s+", " ", txt).strip(" :;,-")
    return txt[:40] or "sin detalle"


def format_for_telegram(data: dict[str, Any]) -> str:
    if data.get("_error"):
        return f"🟪 ASXN: sin respuesta este run ({err_corto(data['_error'])})"
    p = data.get("data") or {}
    if not p:
        return "🟪 ASXN: respondio sin metricas legibles"

    lines = ["🟪 *ASXN — flywheel de HYPE*"]

    bb_usd = p.get("buyback_usd_dia")
    if isinstance(bb_usd, (int, float)):
        dias = p.get("buyback_dias")
        # El dato viejo se marca. Nunca se imprime marzo como si fuera hoy.
        sello = f" ({p.get('buyback_as_of')})"
        if isinstance(dias, int) and dias > STALE_DAYS:
            sello = f" ⚠️ RANCIO: ultimo dato {p.get('buyback_as_of')} ({dias}d)"
        hype = p.get("buyback_hype_dia")
        px = p.get("buyback_px_dia")
        det = f" = {hype:,.0f} HYPE @ ${px:,.2f}" if hype and px else ""
        lines.append(f"  • Buyback AF dia: ${bb_usd/1e6:,.2f}M{det}{sello}")
    if isinstance(p.get("buyback_usd_7d"), (int, float)):
        lines.append(f"  • Buyback 7d: ${p['buyback_usd_7d']/1e6:,.1f}M "
                     f"| all-time ${(p.get('buyback_usd_total') or 0)/1e6:,.0f}M")

    if isinstance(p.get("burn_hype_total"), (int, float)):
        lines.append(f"  • Burn all-time: {p['burn_hype_total']:,.0f} HYPE "
                     f"| 7d {(p.get('burn_7d') or 0):,.0f}")
    if isinstance(p.get("burn_rate_30d"), (int, float)):
        lines.append(f"  • Burn rate 30d: {p['burn_rate_30d']:,.0f} HYPE/dia")

    if isinstance(p.get("stake_hype_total"), (int, float)):
        pct = p.get("stake_pct")
        y = p.get("stake_yield")
        extra = f" ({pct:,.1f}% del supply)" if isinstance(pct, (int, float)) else ""
        lines.append(f"  • Staked: {p['stake_hype_total']/1e6:,.1f}M HYPE{extra}")
        if isinstance(y, (int, float)):
            lines.append(f"  • Yield staking: {y:,.2f}% | validadores "
                         f"{p.get('validadores') or 'n/d'}")

    if data.get("_partial"):
        lines.append(f"  ⚠️ parcial: {str(data['_partial'])[:70]}")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - smoke manual
    import json
    d = asyncio.run(fetch_all())
    print(json.dumps(d, indent=2, default=str)[:1500])
    print(format_for_telegram(d))
