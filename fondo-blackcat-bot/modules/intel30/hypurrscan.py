"""HypurrScan REST — subastas HIP-1/HIP-3 y TWAPs (R-INTEL30 #3).

R-BOT-DEFINITIVE Fase 4.1 — ARREGLADA.
---------------------------------------
Durante meses el reporte imprimio "HypurrScan: fuente no disponible este run".
La lectura obvia era "la API se murio". Era falsa: la API esta perfecta y
responde en milisegundos. Lo que paso es que HypurrScan renombro las rutas y
el modulo seguia probando cinco caminos que hoy dan 404:

    /ui/auctions  /auctions  /auction/current  /v1/auctions  /api/auctions

Nada en el codigo podia notar la diferencia entre "renombraron la ruta" y "se
cayo el servicio", porque las dos cosas se ven igual: un 404. El propio
servidor publica el contrato en https://api.hypurrscan.io/openapi.json, que
lista las rutas reales:

    GET /pastAuctions       subastas HIP-1 spot adjudicadas
    GET /pastAuctionsPerp   deploys de perps HIP-3
    GET /twap/{token}       ordenes TWAP vivas

Limite global publicado: 1000 req/min/IP. Aca se usan 3 llamadas por run.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE = "https://api.hypurrscan.io"
HTTP_TIMEOUT = 12.0
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "Chrome/124.0 Safari/537.36")

PATH_AUCTIONS = "/pastAuctions"
PATH_AUCTIONS_PERP = "/pastAuctionsPerp"
PATH_TWAP = "/twap/HYPE"

# Ruta de descubrimiento: si manana vuelven a renombrar, esto lo dice.
PATH_OPENAPI = "/openapi.json"


def _ms_to_iso(ms: Any) -> str | None:
    try:
        v = float(ms)
    except (TypeError, ValueError):
        return None
    if v > 1e12:            # milisegundos
        v /= 1000.0
    try:
        return datetime.fromtimestamp(v, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return None


async def _get_list(client: httpx.AsyncClient, path: str) -> tuple[list | None, str | None]:
    """GET que devuelve (lista, error). Nunca convierte un fallo en [].

    C4 (R-BOT-DEFINITIVE): un 200 con una forma que no esperamos NO es "no
    hubo subastas". Devolver [] en ese caso hace que el reporte diga
    "sin subastas recientes", que es una afirmacion sobre el mercado que
    nadie verifico.
    """
    try:
        r = await client.get(f"{BASE}{path}")
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}@{path}: {str(exc)[:60]}"
    if r.status_code != 200:
        return None, f"http_{r.status_code}@{path}"
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        return None, f"non_json@{path}"
    if not isinstance(data, list):
        return None, (f"200 con shape inesperado en {path}: "
                      f"{type(data).__name__}, se esperaba lista")
    return data, None


async def fetch_auctions() -> dict[str, Any]:
    """Subastas HIP-1 (spot) + HIP-3 (perp) + TWAPs de HYPE."""
    async with httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        headers={"User-Agent": UA, "Accept": "application/json"},
        follow_redirects=True,
    ) as client:
        spot, e1 = await _get_list(client, PATH_AUCTIONS)
        perp, e2 = await _get_list(client, PATH_AUCTIONS_PERP)
        twap, e3 = await _get_list(client, PATH_TWAP)

    errs = [e for e in (e1, e2, e3) if e]
    if spot is None and perp is None:
        # Las dos rutas principales caidas: eso SI es la fuente abajo.
        return {"data": None, "source": BASE, "_error": "; ".join(errs)[:200]}

    def _norm_spot(rows: list) -> list[dict[str, Any]]:
        out = []
        for r in rows[-8:]:
            if not isinstance(r, dict):
                continue
            gas = r.get("deployGas")
            try:
                # deployGas viene negativo (gas quemado por el deployer).
                cost = abs(float(gas)) if gas is not None else None
            except (TypeError, ValueError):
                cost = None
            out.append({"name": r.get("name"), "usdc": cost,
                        "when_utc": _ms_to_iso(r.get("time")),
                        "deployer": r.get("deployer")})
        return list(reversed(out))

    def _norm_perp(rows: list) -> list[dict[str, Any]]:
        out = []
        for r in rows[-6:]:
            if not isinstance(r, dict):
                continue
            act = r.get("action") or {}
            req = ((act.get("registerAsset") or {}).get("assetRequest")) or {}
            out.append({"coin": req.get("coin") or act.get("type"),
                        "dex": (act.get("registerAsset") or {}).get("dex"),
                        "when_utc": _ms_to_iso(r.get("time")),
                        "error": r.get("error")})
        return list(reversed(out))

    def _norm_twap(rows: list) -> list[dict[str, Any]]:
        out = []
        for r in rows[-5:]:
            if not isinstance(r, dict):
                continue
            tw = ((r.get("action") or {}).get("twap")) or {}
            out.append({"size": tw.get("s"), "buy": tw.get("b"),
                        "minutes": tw.get("m"),
                        "when_utc": _ms_to_iso(r.get("time"))})
        return list(reversed(out))

    return {
        "data": {
            "spot": _norm_spot(spot or []),
            "perp": _norm_perp(perp or []),
            "twap": _norm_twap(twap or []),
            "spot_total": len(spot or []),
            "perp_total": len(perp or []),
        },
        "source": BASE,
        # Errores parciales se declaran, no se esconden: si los TWAPs fallaron
        # pero las subastas no, el reporte sale igual pero lo dice.
        "_partial": "; ".join(errs)[:200] or None,
        "_error": None,
    }


async def fetch_all() -> dict[str, Any]:
    return {"auctions": await fetch_auctions()}


async def discover_paths() -> dict[str, Any]:
    """Lee el openapi.json y devuelve las rutas publicadas.

    Sirve para el self-test: si las rutas que usamos desaparecen del contrato,
    se sabe ANTES de que el reporte empiece a decir "fuente no disponible".
    """
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT,
                                 headers={"User-Agent": UA}) as client:
        try:
            r = await client.get(f"{BASE}{PATH_OPENAPI}")
            paths = sorted((r.json() or {}).get("paths", {}))
        except Exception as exc:  # noqa: BLE001
            return {"paths": None, "_error": f"{type(exc).__name__}: {exc}"[:120]}
    usadas = [PATH_AUCTIONS, PATH_AUCTIONS_PERP, "/twap/{tokenOrAddress}"]
    return {"paths": paths,
            "faltantes": [p for p in usadas if p not in paths],
            "_error": None}


def err_corto(err: Any) -> str:
    """Una linea corta y SIN fragmentos de URL ni tracebacks (WI-9e).

    El motivo de la regla: cuando una fuente falla, pegar el error crudo en un
    reporte de mercado no ayuda a nadie y encima corta URLs por la mitad. Lo
    util es el codigo, no el texto.
    """
    txt = str(err or "").strip()
    txt = re.sub(r"https?://\S*", "", txt)
    txt = re.sub(r"\s+", " ", txt).strip(" :;,-")
    return txt[:40] or "sin detalle"


def format_for_telegram(data: dict[str, Any]) -> str:
    auc = data.get("auctions") or {}
    if auc.get("_error"):
        return f"🪶 HypurrScan: sin respuesta este run ({err_corto(auc['_error'])})"
    payload = auc.get("data") or {}
    lines = ["🪶 *HypurrScan — subastas HL*"]

    spot = payload.get("spot") or []
    if spot:
        lines.append(f"  • HIP-1 spot (ultimas {len(spot)} de "
                     f"{payload.get('spot_total', '?')}):")
        for s in spot[:4]:
            usdc = s.get("usdc")
            px = f"${usdc:,.0f}" if isinstance(usdc, (int, float)) else "n/d"
            lines.append(f"    – `{s.get('name') or '?'}` {px} "
                         f"({s.get('when_utc') or 's/f'})")
    perp = payload.get("perp") or []
    if perp:
        lines.append(f"  • HIP-3 perps (ultimos {len(perp)}):")
        for p in perp[:3]:
            lines.append(f"    – `{p.get('coin') or '?'}` dex={p.get('dex') or '-'} "
                         f"({p.get('when_utc') or 's/f'})")
    twap = payload.get("twap") or []
    if twap:
        lines.append(f"  • TWAPs HYPE vivos: {len(twap)}")
        for t in twap[:3]:
            lado = "compra" if t.get("buy") else "venta"
            lines.append(f"    – {lado} {t.get('size')} en {t.get('minutes')}min "
                         f"({t.get('when_utc') or 's/f'})")
    if len(lines) == 1:
        lines.append("  • sin subastas ni TWAPs en la ventana")
    if auc.get("_partial"):
        lines.append(f"  ⚠️ parcial: {auc['_partial'][:70]}")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - smoke manual
    import json
    print(json.dumps(asyncio.run(fetch_all()), indent=2, default=str)[:2000])
