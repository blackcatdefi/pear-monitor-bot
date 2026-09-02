"""R-BOT-DEFINITIVE (2026-09-01) — registro compartido de salud por subsistema.

POR QUE EXISTE
==============
Cinco rondas seguidas destaparon un componente distinto corriendo DEGRADADO EN
SILENCIO mientras se veia sano desde afuera. El patron es siempre el mismo: una
falla se captura, se loguea y se convierte en un valor plausible — [], 0, None,
un payload cacheado — y el numero equivocado llega al reporte sin ninguna marca.

El unico subsistema que YA resolvio esto es el ledger, con ``ledger_sync_health``
y el banner ``LEDGER INCOMPLETO``. Este modulo generaliza exactamente ese patron
para que CUALQUIER subsistema pueda declarar "produje datos incompletos" y para
que toda seccion del reporte que dependa de el muestre un banner visible en vez
de numeros callados.

DOCTRINA
========
Un default silencioso en el money path NUNCA es una degradacion aceptable. Las
unicas dos salidas validas de un handler que traga una falla son:

  1. levantar (preferido cuando el llamador puede decidir), o
  2. devolver el default PERO llamar a ``swallowed()`` / ``mark_degraded()``,
     para que la degradacion aparezca en /health, en /diagnostico y como banner
     en la seccion del reporte afectada.

``swallowed()`` esta pensado para llamarse DESDE ADENTRO de un ``except`` sin
tener que ligar la excepcion con ``as exc``: lee ``sys.exc_info()``. Eso permite
instrumentar handlers existentes agregando UNA linea, sin tocar su firma ni su
flujo. Es deliberado: un fix que obliga a reescribir 56 handlers no se aplica.

GARANTIAS
=========
* NADA de este modulo levanta jamas. Un registro de salud que rompe el proceso
  que esta vigilando es peor que no tenerlo. Todo error interno se traga a
  proposito (esta es la UNICA excepcion legitima a la doctrina de arriba, y es
  por eso que ``_self_error`` se cuenta y se expone en ``all_status()``).
* Escribir es barato: un UPSERT sobre una tabla de una fila por subsistema.
* Un ``mark_ok`` posterior LIMPIA la degradacion. La salud es un estado actual,
  no un historial de agravios.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from config import DATA_DIR

log = logging.getLogger(__name__)

DB_PATH = os.path.join(DATA_DIR, "intel_memory.db")

# Contador de fallas del propio registro. Si esto no es 0, /diagnostico lo dice:
# un vigilante roto no puede reportarse a si mismo como sano.
_self_error = 0
_self_error_last = ""
_lock = threading.Lock()

# R-BOT-FINAL (2026-09-02) — contador EN PROCESO de degradaciones por
# subsistema. No se persiste: solo sirve para que ``tracked`` sepa si, entre que
# entro y que salio una operacion, alguien llamo a swallowed() adentro. Ver el
# comentario largo en ``tracked`` para por que hace falta.
_deg_seq: dict[str, int] = {}


# ─── catalogo de subsistemas ────────────────────────────────────────────────
# `section` es la seccion del reporte que queda invalidada cuando el subsistema
# esta degradado — de ahi sale el banner. `money` marca si una degradacion puede
# corromper un numero que BCD lee o una decision que el bot toma.
SUBSYSTEMS: dict[str, dict[str, Any]] = {
    "ledger":       {"label": "Ledger de cierres",     "section": "cierres",   "money": True},
    "portfolio":    {"label": "Posiciones HL",         "section": "positions", "money": True},
    "market":       {"label": "Precios y mercado",     "section": "market",    "money": True},
    "funding":      {"label": "Funding rates",         "section": "funding",   "money": True},
    "pm_state":     {"label": "Oraculo PM / LTV",      "section": "pm",        "money": True},
    "vault":        {"label": "Vault (depositos/hist)", "section": "vault",     "money": True},
    "ppc":          {"label": "PPC de HYPE spot",      "section": "positions", "money": True},
    "x_api":        {"label": "X API / panel de costos", "section": "costs",   "money": True},
    "cost_tracker": {"label": "Tracker de costos LLM", "section": "costs",     "money": True},
    "gmail":        {"label": "Gmail intel",           "section": "intel",     "money": False},
    "intel_feeds":  {"label": "Feeds de intel",        "section": "intel",     "money": False},
    "backup":       {"label": "Backup sqlite",         "section": "ops",       "money": False},
    "attribution":  {"label": "Atribucion de performance", "section": "pnl",   "money": True},
    "integrity":    {"label": "Reconciliacion de integridad", "section": "intel", "money": False},
}

# Secciones -> subsistemas que las alimentan (para el banner).
def subsystems_for_section(section: str) -> list[str]:
    return [k for k, v in SUBSYSTEMS.items() if v["section"] == section]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10.0)
    con.row_factory = sqlite3.Row
    con.execute("""
        CREATE TABLE IF NOT EXISTS subsystem_health (
            subsystem     TEXT PRIMARY KEY,
            ok            INTEGER NOT NULL DEFAULT 1,
            detail        TEXT    NOT NULL DEFAULT '',
            last_ok_utc   TEXT,
            last_bad_utc  TEXT,
            fail_count    INTEGER NOT NULL DEFAULT 0,
            meta          TEXT    NOT NULL DEFAULT ''
        )
    """)
    con.commit()
    return con


def _note_self_error(exc: BaseException) -> None:
    global _self_error, _self_error_last
    _self_error += 1
    _self_error_last = f"{type(exc).__name__}: {exc}"[:200]
    log.warning("health_registry self-error: %s", _self_error_last)


# ─── escritura ──────────────────────────────────────────────────────────────

def mark_ok(subsystem: str, detail: str = "", meta: str = "") -> None:
    """Registra un exito. LIMPIA cualquier degradacion previa: la salud es el
    estado actual, no un historial."""
    try:
        with _lock:
            con = _conn()
            try:
                con.execute(
                    "INSERT INTO subsystem_health (subsystem, ok, detail,"
                    " last_ok_utc, fail_count, meta) VALUES (?,1,?,?,0,?) "
                    "ON CONFLICT(subsystem) DO UPDATE SET ok=1,"
                    " detail=excluded.detail, last_ok_utc=excluded.last_ok_utc,"
                    " fail_count=0, meta=excluded.meta",
                    (subsystem, detail[:400], _now(), meta[:400]),
                )
                con.commit()
            finally:
                con.close()
    except Exception as exc:  # noqa: BLE001
        _note_self_error(exc)


def mark_degraded(subsystem: str, detail: str, meta: str = "") -> None:
    """Registra que el subsistema produjo datos incompletos o equivocados.

    No limpia ``last_ok_utc``: saber cuando fue la ultima vez que funciono es
    justo el dato que faltaba en todas las rondas anteriores.
    """
    try:
        with _lock:
            con = _conn()
            try:
                con.execute(
                    "INSERT INTO subsystem_health (subsystem, ok, detail,"
                    " last_bad_utc, fail_count, meta) VALUES (?,0,?,?,1,?) "
                    "ON CONFLICT(subsystem) DO UPDATE SET ok=0,"
                    " detail=excluded.detail,"
                    " last_bad_utc=excluded.last_bad_utc,"
                    " fail_count=subsystem_health.fail_count+1,"
                    " meta=excluded.meta",
                    (subsystem, detail[:400], _now(), meta[:400]),
                )
                con.commit()
            finally:
                con.close()
        log.warning("SUBSISTEMA DEGRADADO [%s]: %s", subsystem, detail[:200])
    except Exception as exc:  # noqa: BLE001
        _note_self_error(exc)
    finally:
        # Fuera del try de arriba a proposito: aunque la ESCRITURA falle, la
        # degradacion ocurrio y ``tracked`` no debe declarar exito por eso.
        try:
            _deg_seq[subsystem] = _deg_seq.get(subsystem, 0) + 1
        except Exception:  # noqa: BLE001
            pass


def swallowed(subsystem: str, note: str = "") -> None:
    """Llamar DESDE ADENTRO de un ``except`` que devuelve un valor plausible.

    Lee ``sys.exc_info()``, asi que no hace falta ligar la excepcion con
    ``as exc`` — se puede instrumentar un handler existente agregando una sola
    linea. Si no hay excepcion en vuelo igual registra la degradacion, con el
    note como unico detalle.
    """
    try:
        exc = sys.exc_info()[1]
        if exc is not None:
            detail = f"{type(exc).__name__}: {exc}"
        else:
            detail = note or "degradado sin excepcion en vuelo"
        if note and exc is not None:
            detail = f"{note} — {detail}"
        mark_degraded(subsystem, detail)
    except Exception as exc:  # noqa: BLE001
        _note_self_error(exc)


# ─── registro de EXITO ──────────────────────────────────────────────────────
# R-BOT-FINAL (2026-09-02) — el agujero que tapa esta seccion.
#
# La ronda anterior instrumento 89 handlers del money path con swallowed(), y
# quedo un registro que SOLO sabia anotar fracasos. Nadie llamaba nunca a
# mark_ok(). Consecuencia en produccion, verificada en /diagnostico: los 14
# subsistemas decian "ultimo ok nunca" mientras el MISMO reporte mostraba "sync
# hace 4h" y "ultimo backup hace 18h". O sea: el bot funcionaba y su propio
# panel de salud no tenia forma de saberlo.
#
# Eso no es un detalle cosmetico. Rompe tres cosas concretas:
#   * stale_subsystems() devolvia SIEMPRE los 14, con lo cual "rancio" no
#     distinguia nada y dejaba de ser una senal.
#   * un subsistema que de verdad no corre nunca era indistinguible de uno que
#     anda perfecto — que es exactamente la ceguera que este registro vino a
#     eliminar.
#   * last_ok_utc, el dato que las cinco rondas anteriores extranaron ("¿cuando
#     fue la ultima vez que esto funciono?"), nunca se escribia.
#
# El decorador se aplica al punto de entrada de cada subsistema. Se eligio
# decorador y no context manager por una razon practica: envolver el cuerpo de
# funciones de 200 lineas obliga a reindentarlas enteras, y un diff asi esconde
# errores. Una linea arriba de la firma no esconde nada.
#
# EL DETALLE QUE IMPORTA: un exito NO puede borrar una degradacion registrada
# durante la misma operacion. Si fetch_market_data() devuelve un dict pero
# adentro un swallowed() ya marco que CoinGecko tiro 429, marcar ok al salir
# borraria justo el aviso que hay que dar. Por eso se compara el contador
# _deg_seq del subsistema entre la entrada y la salida: si crecio, la operacion
# "termino" pero no fue un exito, y la degradacion queda en pie.


def _mark_ok_if_clean(subsystem: str, before: int, detail: str,
                      out: Any = None) -> None:
    if _deg_seq.get(subsystem, 0) != before:
        return  # hubo un swallowed() adentro: NO es un exito, no lo pises
    # Varias de estas funciones no levantan nunca: reportan el fracaso
    # DEVOLVIENDO {"ok": False, "reason": ...} — run_backup() es el caso
    # tipico, cuando el tar falla. Retornar sin excepcion no es lo mismo que
    # haber funcionado, y tomar un ok=False explicito como exito seria repetir
    # el error que este registro existe para cazar.
    if isinstance(out, dict) and out.get("ok") is False:
        mark_degraded(subsystem, f"{detail}: {out.get('reason') or out.get('error') or 'devolvio ok=False'}")
        return
    mark_ok(subsystem, detail)


def tracked(subsystem: str, detail: str = ""):
    """Decora el punto de entrada de un subsistema para que anote sus exitos.

    * retorno limpio y sin degradaciones internas -> ``mark_ok``.
    * retorno limpio pero con un ``swallowed()`` adentro -> no toca nada, la
      degradacion sobrevive.
    * excepcion -> ``mark_degraded`` y la excepcion SIGUE VIAJANDO. Este
      decorador nunca traga nada; solo mira.

    Sirve para funciones sync y async.
    """
    def deco(fn):
        nombre = detail or getattr(fn, "__name__", subsystem)

        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrap(*a, **kw):
                before = _deg_seq.get(subsystem, 0)
                try:
                    out = await fn(*a, **kw)
                except Exception:
                    mark_degraded(subsystem, f"{nombre}: "
                                  f"{type(sys.exc_info()[1]).__name__}: "
                                  f"{sys.exc_info()[1]}")
                    raise
                _mark_ok_if_clean(subsystem, before, nombre, out)
                return out
            return awrap

        @functools.wraps(fn)
        def wrap(*a, **kw):
            before = _deg_seq.get(subsystem, 0)
            try:
                out = fn(*a, **kw)
            except Exception:
                mark_degraded(subsystem, f"{nombre}: "
                              f"{type(sys.exc_info()[1]).__name__}: "
                              f"{sys.exc_info()[1]}")
                raise
            _mark_ok_if_clean(subsystem, before, nombre, out)
            return out
        return wrap
    return deco


def clear(subsystem: str) -> None:
    try:
        with _lock:
            con = _conn()
            try:
                con.execute("DELETE FROM subsystem_health WHERE subsystem=?",
                            (subsystem,))
                con.commit()
            finally:
                con.close()
    except Exception as exc:  # noqa: BLE001
        _note_self_error(exc)


def reset_all() -> None:
    """Solo para tests y para /diagnostico --reset."""
    _deg_seq.clear()
    try:
        with _lock:
            con = _conn()
            try:
                con.execute("DELETE FROM subsystem_health")
                con.commit()
            finally:
                con.close()
    except Exception as exc:  # noqa: BLE001
        _note_self_error(exc)


# ─── lectura ────────────────────────────────────────────────────────────────

def status(subsystem: str) -> dict[str, Any]:
    try:
        con = _conn()
        try:
            row = con.execute(
                "SELECT * FROM subsystem_health WHERE subsystem=?",
                (subsystem,)).fetchone()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        _note_self_error(exc)
        return {"subsystem": subsystem, "ok": None, "detail": "registro no disponible"}
    if row is None:
        # Nunca reporto nada. NO es lo mismo que "sano": es "sin datos".
        return {"subsystem": subsystem, "ok": None, "detail": "sin datos",
                "last_ok_utc": None, "last_bad_utc": None, "fail_count": 0}
    d = dict(row)
    d["ok"] = bool(d["ok"])
    return d


def is_degraded(subsystem: str) -> bool:
    """True SOLO si el subsistema reporto una degradacion. 'Sin datos' no
    cuenta como degradado — cuenta como desconocido, y eso se ve en /health."""
    return status(subsystem).get("ok") is False


def all_status() -> dict[str, Any]:
    out: dict[str, Any] = {"subsystems": {}, "degraded": [], "unknown": [],
                           "registry_self_errors": _self_error}
    if _self_error:
        out["registry_self_error_last"] = _self_error_last
    try:
        con = _conn()
        try:
            rows = {r["subsystem"]: dict(r) for r in con.execute(
                "SELECT * FROM subsystem_health")}
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        _note_self_error(exc)
        rows = {}
    for name, spec in SUBSYSTEMS.items():
        r = rows.get(name)
        if r is None:
            entry = {"ok": None, "detail": "sin datos", "last_ok_utc": None,
                     "last_bad_utc": None, "fail_count": 0}
            out["unknown"].append(name)
        else:
            entry = {"ok": bool(r["ok"]), "detail": r["detail"],
                     "last_ok_utc": r["last_ok_utc"],
                     "last_bad_utc": r["last_bad_utc"],
                     "fail_count": int(r["fail_count"] or 0)}
            if not entry["ok"]:
                out["degraded"].append(name)
        entry["label"] = spec["label"]
        entry["money"] = spec["money"]
        entry["section"] = spec["section"]
        entry["stale_hours"] = _hours_since(entry.get("last_ok_utc"))
        out["subsystems"][name] = entry
    # Filas de subsistemas que no estan en el catalogo (alguien llamo
    # mark_degraded con un nombre nuevo): mostrarlas igual, nunca esconderlas.
    for name, r in rows.items():
        if name in SUBSYSTEMS:
            continue
        out["subsystems"][name] = {
            "ok": bool(r["ok"]), "detail": r["detail"], "label": name,
            "money": False, "section": "?", "fail_count": int(r["fail_count"] or 0),
            "last_ok_utc": r["last_ok_utc"], "last_bad_utc": r["last_bad_utc"],
            "stale_hours": _hours_since(r["last_ok_utc"]),
        }
        if not r["ok"]:
            out["degraded"].append(name)
    out["ok"] = not out["degraded"] and _self_error == 0
    return out


def _hours_since(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return round((datetime.now(timezone.utc) - dt).total_seconds() / 3600.0, 2)
    except Exception:  # noqa: BLE001
        return None


def stale_subsystems(max_hours: float) -> list[str]:
    """Subsistemas cuyo ultimo exito es mas viejo que ``max_hours``, o que
    nunca reportaron uno."""
    st = all_status()["subsystems"]
    out = []
    for name, e in st.items():
        h = e.get("stale_hours")
        if h is None or h > max_hours:
            out.append(name)
    return sorted(out)


# ─── banner ─────────────────────────────────────────────────────────────────

def banner(*subsystems: str, section: str = "") -> str:
    """Banner visible para pegar ARRIBA de una seccion del reporte.

    Devuelve "" cuando todo lo consultado esta sano — el reporte no cambia
    mientras el sistema funciona. Ese es el contrato: silencioso cuando esta
    sano, ruidoso cuando no.
    """
    names = list(subsystems) or (subsystems_for_section(section) if section else [])
    bad = []
    for n in names:
        st = status(n)
        if st.get("ok") is False:
            label = SUBSYSTEMS.get(n, {}).get("label", n)
            bad.append((label, str(st.get("detail") or "")[:120]))
    if not bad:
        return ""
    head = "\u26a0\ufe0f DATOS INCOMPLETOS — los numeros de abajo pueden estar mal"
    lines = [head]
    for label, detail in bad:
        lines.append(f"  \u2022 {label}: {detail}")
    lines.append("  Detalle completo en /diagnostico")
    return "\n".join(lines) + "\n"
