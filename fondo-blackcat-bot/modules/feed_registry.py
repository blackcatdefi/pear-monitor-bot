"""R-BOT-DEFINITIVE Fase 4 — el registro formal de fuentes de intel.

Por que existe este archivo
---------------------------
Durante meses el reporte imprimio, todos los dias, lineas como

    "🪶 HypurrScan: fuente no disponible este run"
    "📊 ASXN: fuente no disponible este run"

Esa linea es lo peor de los dos mundos: ocupa lugar como si fuera informacion
y no informa nada. Nadie sabia si la fuente se habia caido ayer o si estaba
rota desde el dia uno, ni si alguien iba a arreglarla alguna vez. Una fuente
permanentemente muerta que imprime una linea educada todos los dias es ruido
disfrazado de dato.

La regla de la ronda es: **cada fuente esta o VIVA o RETIRADA, y si esta
retirada hay que decir por que, desde cuando y quien lo decidio.** No hay
tercer estado "medio rota que ya veremos". Las unicas dos excepciones son
SIN_CLAVE (la fuente funciona, falta configurar una variable de entorno, y el
nombre de esa variable esta aca) y DEGRADADA (la fuente responde pero con
datos viejos, y eso se declara con fecha).

Este modulo NO hace red. Solo dice que se espera de cada fuente y lee el
estado observado que dejan los modulos intel30 en source_state.db.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# ── estados posibles ────────────────────────────────────────────────────────
ACTIVA = "ACTIVA"          # se espera que funcione; si falla, es un problema
RETIRADA = "RETIRADA"      # decision tomada: no se intenta mas, no se imprime
SIN_CLAVE = "SIN_CLAVE"    # el codigo esta bien, falta una env var
DEGRADADA = "DEGRADADA"    # responde, pero con datos viejos conocidos

# Cuantos runs seguidos fallando antes de considerar una fuente "muerta".
FEED_DEAD_RUNS = 10


@dataclass(frozen=True)
class Feed:
    key: str                       # nombre con el que el modulo llama a set_source_state
    label: str
    estado: str = ACTIVA
    motivo: str = ""               # obligatorio si no esta ACTIVA
    desde: str = ""                # fecha de la decision (YYYY-MM-DD)
    env_var: str = ""              # si SIN_CLAVE, el nombre de la variable
    reemplazo: str = ""            # que se usa en su lugar
    money: bool = False            # si alimenta un numero de plata


# ── el catalogo ─────────────────────────────────────────────────────────────
# Solo se listan explicitamente las fuentes sobre las que se tomo una decision
# en esta ronda. El resto se asume ACTIVA por defecto (ver feed_state()).
FEEDS: dict[str, Feed] = {f.key: f for f in [
    Feed(
        key="hypurrscan",
        label="HypurrScan — subastas HIP-1/HIP-3 + TWAPs",
        estado=ACTIVA,
        motivo=(
            "ARREGLADA en R-BOT-DEFINITIVE. La API nunca se murio: cambiaron "
            "las rutas. El modulo probaba /ui/auctions, /auctions, "
            "/auction/current, /v1/auctions y /api/auctions — las cinco dan "
            "404 desde hace meses. El openapi.json publico expone las rutas "
            "reales: /pastAuctions, /pastAuctionsPerp, /twap/{token}."),
        desde="2026-09-01",
    ),
    Feed(
        key="asxn",
        label="ASXN — buyback / burn / staking de HYPE",
        estado=ACTIVA,
        motivo=(
            "ARREGLADA en R-BOT-DEFINITIVE. data.asxn.xyz es una SPA de React "
            "(no Next.js), asi que el probe de /_next/data nunca podia "
            "funcionar y el scrape del HTML leia un shell vacio de 1 KB. Los "
            "datos salen de api-data.asxn.xyz. OJO: no todos sus datasets "
            "estan frescos, por eso cada uno viaja con su propia fecha."),
        desde="2026-09-01",
    ),
    Feed(
        key="farside_btc",
        label="Flujos ETF spot BTC (via bitbo)",
        estado=ACTIVA,
        motivo="farside.co.uk esta detras de Cloudflare; bitbo publica BTC.",
        desde="2026-09-01",
    ),
    Feed(
        key="farside_eth",
        label="Flujos ETF spot ETH",
        estado=RETIRADA,
        motivo=(
            "farside.co.uk responde 403 con el desafio JS de Cloudflare "
            "('Just a moment...') a cualquier cliente sin navegador, o sea "
            "siempre desde Railway. bitbo solo publica BTC. El espejo de ASXN "
            "(/api/data/eth-etf-flows) existe pero quedo congelado el "
            "2026-03-23, y un numero de hace cinco meses presentado como el "
            "flujo de hoy es peor que no tener el dato. Se retira: deja de "
            "intentarse y deja de imprimirse."),
        desde="2026-09-01",
        reemplazo="ninguno gratuito verificado",
    ),
    Feed(
        key="farside_sol",
        label="Flujos ETF spot SOL",
        estado=RETIRADA,
        motivo=(
            "Mismo bloqueo de Cloudflare que ETH. El espejo de ASXN tiene 14 "
            "filas y tambien corta el 2026-03-23."),
        desde="2026-09-01",
        reemplazo="ninguno gratuito verificado",
    ),
    Feed(
        key="eia_oil",
        label="EIA — inventarios semanales de crudo (WPSR)",
        estado=SIN_CLAVE,
        motivo=(
            "El modulo esta sano; nunca hubo clave. La API de la EIA es "
            "gratuita y la clave se pide en "
            "https://www.eia.gov/opendata/register.php. Mientras la variable "
            "no exista, la fuente NO se intenta y NO imprime: dejar de "
            "avisar todos los dias que falta una clave que nadie va a poner "
            "en el medio de un reporte de mercado."),
        desde="2026-09-01",
        env_var="EIA_API_KEY",
    ),
]}

# Alias que consume modules/diagnostics.py (Fase 2).
RETIRED: dict[str, str] = {
    k: f.motivo for k, f in FEEDS.items() if f.estado == RETIRADA
}


# ── consultas ───────────────────────────────────────────────────────────────
def get(key: str) -> Feed:
    """Feed declarado, o uno ACTIVA por defecto (una fuente sin decision
    explicita se espera que funcione: el default nunca es 'y bueno')."""
    f = FEEDS.get(key)
    if f is not None:
        return f
    return Feed(key=key, label=key, estado=ACTIVA)


def is_retired(key: str) -> bool:
    return get(key).estado == RETIRADA


def needs_key(key: str) -> str:
    """Devuelve el nombre de la env var faltante, o '' si no aplica."""
    f = get(key)
    return f.env_var if f.estado == SIN_CLAVE else ""


def should_attempt(key: str) -> bool:
    """False para fuentes retiradas o sin clave: ni red, ni linea en el
    reporte. Es la funcion que convierte una decision en silencio real."""
    return get(key).estado not in (RETIRADA, SIN_CLAVE)


def env_vars_faltantes() -> dict[str, str]:
    """{env_var: label} de todo lo que esta SIN_CLAVE. Va al /health y al
    informe de cierre (Fase 0.3), NUNCA con el valor."""
    return {f.env_var: f.label for f in FEEDS.values()
            if f.estado == SIN_CLAVE and f.env_var}


# ── estado observado (lo que realmente paso, no lo que se espera) ───────────
def _observed() -> dict[str, dict[str, Any]]:
    """Lee source_state.db. Si no se puede leer devuelve {} — y el que llama
    tiene que distinguir 'no hay datos' de 'todo bien' (nunca son lo mismo)."""
    try:
        from modules.intel30 import _intel_base as ib
        with ib._state_db() as conn:
            conn.row_factory = None
            rows = conn.execute(
                "SELECT source, status, last_change_utc, consecutive_fails, "
                "last_success_utc FROM source_state").fetchall()
        return {r[0]: {"status": r[1], "last_change_utc": r[2],
                       "consecutive_fails": int(r[3] or 0),
                       "last_success_utc": r[4]} for r in rows}
    except Exception as exc:  # noqa: BLE001
        log.warning("feed_registry: no se pudo leer source_state.db: %s", exc)
        return {}


def _horas_desde(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return round((datetime.now(timezone.utc) - dt).total_seconds() / 3600.0, 1)
    except (ValueError, TypeError):
        return None


def feed_status() -> dict[str, Any]:
    """Foto completa: que se espera de cada fuente y que se observo.

    Devuelve {'_error': ...} si la DB de estado no se pudo leer, para que
    /health muestre '❓ sin datos' y no un tranquilizador '0 muertas'.
    """
    obs = _observed()
    if not obs:
        return {"_error": "source_state.db ilegible o vacia",
                "declaradas": len(FEEDS),
                "retiradas": sorted(RETIRED),
                "sin_clave": sorted(env_vars_faltantes())}

    vivas, rancias, muertas, retiradas, sin_clave = [], [], [], [], []
    detalle: dict[str, Any] = {}
    for key, o in sorted(obs.items()):
        f = get(key)
        horas = _horas_desde(o.get("last_success_utc"))
        fails = o.get("consecutive_fails", 0)
        d = {"estado_declarado": f.estado, "status_observado": o.get("status"),
             "fallos_seguidos": fails,
             "ultimo_exito_utc": o.get("last_success_utc"),
             "horas_desde_ultimo_exito": horas}
        if f.estado == RETIRADA:
            retiradas.append(key)
            d["motivo"] = f.motivo
        elif f.estado == SIN_CLAVE:
            sin_clave.append(key)
            d["falta_env"] = f.env_var
        elif fails >= FEED_DEAD_RUNS:
            muertas.append(key)
        elif fails > 0:
            rancias.append(key)
        else:
            vivas.append(key)
        detalle[key] = d

    return {"vivas": vivas, "rancias": rancias, "muertas": muertas,
            "retiradas": retiradas, "sin_clave": sin_clave,
            "detalle": detalle,
            "env_faltantes": sorted(env_vars_faltantes())}


def dead_feeds(min_runs: int = FEED_DEAD_RUNS) -> list[str]:
    """Fuentes que ANTES funcionaban y llevan min_runs runs caidas.

    'Antes funcionaban' es la condicion clave: sin ella, una fuente que nunca
    anduvo genera una alerta eterna, que es como se entrena a la gente a
    ignorar las alertas.
    """
    st = feed_status()
    if st.get("_error"):
        return []
    out = []
    for key in st.get("muertas", []):
        d = st["detalle"][key]
        if d.get("ultimo_exito_utc"):
            out.append(
                f"{get(key).label}: {d['fallos_seguidos']} runs caida, "
                f"ultimo exito {d['ultimo_exito_utc']}")
    return out
