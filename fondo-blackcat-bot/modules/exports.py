"""Round 17 — Export de datos del fondo a CSV.

Comando: /export <tipo> <periodo>
    tipos: fills, pnl, positions, intel, errors
    periodos: 7d, 30d, 90d, ytd, all

Output: archivo CSV en DATA_DIR/exports/, listo para enviar como Document.

R-BOT-DEFINITIVE (2026-09-01) — REESCRITURA DEL ACCESO A DATOS
--------------------------------------------------------------
Este modulo estaba roto en 3 de sus 5 exports, y lo estuvo desde que se
escribio, sin emitir un solo error. El patron era siempre el mismo:

    db = os.path.join(DATA_DIR, "intel_memory.db")   # <- DB equivocada
    if _table_exists(conn, "pnl_events"):            # <- False, no esta ahi
        ...
    # cae al final:
    w.writerow([...cabeceras...])                    # <- CSV vacio
    return out_path                                  # -> "0 rows"

El usuario pedia ``/export pnl 30d``, recibia un CSV con cabeceras y cero
filas, y concluia razonablemente "no hubo PnL en 30 dias". La verdad es que
la tabla nunca fue consultada. Tres defectos apilados:

  1. DB equivocada. ``pnl_events`` vive en ``pnl.db``, ``position_log`` en
     ``position_log.db`` y ``snapshots`` en ``snapshots.db``; los tres se
     buscaban en ``intel_memory.db``. ``_table_exists`` devolvia False y el
     codigo trataba "la tabla no existe" como "no hay datos".
  2. Columna equivocada. ``pnl_events`` ordena por ``ts``, pero la consulta
     usaba ``timestamp`` -> ``OperationalError: no such column``. Ni siquiera
     hacia falta llegar ahi por el defecto 1, pero estaba armado igual.
     ``position_log`` y ``snapshots`` usaban ``timestamp_utc`` y la columna
     real tambien es ``ts``.
  3. ``except Exception`` que loguea y sigue, convirtiendo cualquier fallo en
     el mismo CSV vacio indistinguible de un periodo sin actividad.

La correccion no es parchear las rutas: es quitarle a este modulo el derecho a
saber donde vive cada tabla. Ahora cada export declara el MODULO DUENO y la
ruta se lee de su ``DB_PATH`` en tiempo de llamada. Si alguien mueve una DB,
este modulo la sigue sola. Y "tabla ausente" o "consulta fallida" ahora levantan
``ExportError`` en vez de fabricar un CSV vacio: un default silencioso en el
camino del dinero no es una degradacion aceptable.
"""
from __future__ import annotations

import csv
import importlib
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from config import DATA_DIR
from modules import health_registry

log = logging.getLogger(__name__)


class ExportError(RuntimeError):
    """La fuente de datos no pudo consultarse.

    Se levanta SOLO cuando no sabemos si hay datos (DB ausente, tabla ausente,
    consulta fallida). Un periodo genuinamente sin filas NO levanta: devuelve un
    CSV con cabeceras y count 0, que es una respuesta verdadera.
    """

_EXPORT_DIR = os.path.join(DATA_DIR, "exports")
os.makedirs(_EXPORT_DIR, exist_ok=True)

VALID_TYPES = {"fills", "pnl", "positions", "intel", "errors"}
VALID_PERIODS = {"7d", "30d", "90d", "ytd", "all"}


def _cutoff(period: str) -> datetime | None:
    now = datetime.now(timezone.utc)
    if period == "7d":
        return now - timedelta(days=7)
    if period == "30d":
        return now - timedelta(days=30)
    if period == "90d":
        return now - timedelta(days=90)
    if period == "ytd":
        return datetime(now.year, 1, 1, tzinfo=timezone.utc)
    return None  # all


def _output_path(tipo: str, periodo: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    fname = f"fondo_blackcat_{tipo}_{periodo}_{ts}.csv"
    return os.path.join(_EXPORT_DIR, fname)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cur.fetchone() is not None


# ─── Registro de fuentes ────────────────────────────────────────────────────
# Cada export declara QUE modulo es el dueno de la tabla. La ruta de la DB se
# resuelve leyendo el DB_PATH de ese modulo en tiempo de llamada, nunca se
# hardcodea aca. Esto hace imposible el defecto 1 del encabezado: si el dueno
# mueve su DB, el export la sigue. Tambien permite que los tests parcheen
# ``modules.pnl_tracker.DB_PATH`` y el export lo respete sin tocar nada mas.

@dataclass(frozen=True)
class _Source:
    owner: str          # modulo dueno, del que se lee DB_PATH
    table: str          # tabla a exportar
    ts_col: str         # columna de tiempo por la que se filtra y ordena
    columns: tuple[str, ...]   # cabeceras cuando no hay filas
    subsystem: str      # subsistema del health_registry a marcar si degrada


# Los ts_col de abajo estan verificados contra el CREATE TABLE del modulo dueno
# y contra el escritor: los cinco se escriben con datetime.isoformat(), asi que
# comparar contra cutoff.isoformat() es consistente y NO necesita utils.tsnorm.
# Si alguna pasara a DEFAULT CURRENT_TIMESTAMP, hay que normalizar ambos lados.
_SOURCES: dict[str, _Source] = {
    "fills": _Source(
        owner="modules.position_log", table="position_log", ts_col="ts",
        columns=("id", "ts", "kind", "asset", "amount_usd", "wallet_label",
                 "message"),
        subsystem="ledger"),
    "pnl": _Source(
        owner="modules.pnl_tracker", table="pnl_events", ts_col="ts",
        columns=("id", "ts", "category", "asset", "amount_usd", "wallet_label",
                 "notes"),
        subsystem="attribution"),
    "positions": _Source(
        owner="modules.snapshots", table="snapshots", ts_col="ts",
        columns=("id", "ts", "payload"),
        subsystem="portfolio"),
    "intel": _Source(
        owner="modules.intel_memory", table="intel_memory",
        ts_col="timestamp_utc",
        columns=("id", "timestamp_utc", "source", "raw_text",
                 "parsed_summary", "tags"),
        subsystem="intel_feeds"),
    "errors": _Source(
        owner="modules.errors_log", table="errors_log", ts_col="timestamp_utc",
        columns=("id", "timestamp_utc", "handler", "error_type",
                 "error_message", "traceback"),
        subsystem="intel_feeds"),
}


def _db_path_of(src: _Source) -> str:
    """Ruta de la DB segun el modulo dueno, resuelta ahora (no al importar).

    Se importa cada vez a proposito: ``importlib.import_module`` devuelve el
    modulo ya cacheado en sys.modules, asi que el costo es nulo, pero se lee el
    valor ACTUAL de DB_PATH — que es lo que permite monkeypatch en tests y lo
    que evita congelar una ruta vieja si el dueno la recalcula.
    """
    mod = importlib.import_module(src.owner)
    path = getattr(mod, "DB_PATH", "")
    if not path:
        raise ExportError(
            f"el modulo {src.owner} no expone DB_PATH; no se puede localizar "
            f"la tabla {src.table}")
    return str(path)


def _fetch(src: _Source, cutoff_iso: str | None) -> list[dict]:
    """Lee las filas de la fuente. Levanta ExportError si no puede consultarla.

    La distincion es el punto entero de la reescritura:
      - lista vacia  -> la tabla existe y no hay filas en la ventana (verdad)
      - ExportError  -> no sabemos si hay filas (DB o tabla ausente, o error)
    """
    db = _db_path_of(src)
    if not os.path.isfile(db):
        raise ExportError(
            f"la base {os.path.basename(db)} (duena de {src.table}) todavia no "
            f"existe: el subsistema nunca escribio nada")
    conn = None
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        if not _table_exists(conn, src.table):
            raise ExportError(
                f"la tabla {src.table} no existe en "
                f"{os.path.basename(db)}")
        order = f"ORDER BY {src.ts_col} DESC"
        if cutoff_iso:
            cur = conn.execute(
                f"SELECT * FROM {src.table} WHERE {src.ts_col} >= ? {order}",
                (cutoff_iso,))
        else:
            cur = conn.execute(f"SELECT * FROM {src.table} {order}")
        return [{k: r[k] for k in r.keys()} for r in cur.fetchall()]
    except ExportError:
        raise
    except sqlite3.Error as exc:
        # Antes esto era `except Exception: log.exception(...)` y devolvia CSV
        # vacio. Ahora degrada el subsistema Y corta: el usuario se entera.
        health_registry.swallowed(src.subsystem, f"export {src.table}")
        raise ExportError(
            f"fallo la consulta a {src.table}: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        if conn is not None:
            conn.close()


def _write_csv(out_path: str, rows: list[dict],
               columns: tuple[str, ...]) -> str:
    """Escribe el CSV. Con filas usa sus claves; sin filas, las cabeceras
    declaradas — que ahora si significan 'no hubo actividad'."""
    fields = list(rows[0].keys()) if rows else list(columns)
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)
    return out_path


def export_table(tipo: str, periodo: str) -> str:
    """Export generico dirigido por _SOURCES. Levanta ExportError si degrada."""
    src = _SOURCES[tipo]
    cutoff = _cutoff(periodo)
    # positions es un snapshot del estado actual, pero conservar la ventana no
    # hace dano y ademas permite pedir el historico: se respeta el periodo.
    cutoff_iso = cutoff.isoformat() if cutoff else None
    rows = _fetch(src, cutoff_iso)
    return _write_csv(_output_path(tipo, periodo), rows, src.columns)


# ─── Wrappers por tipo ──────────────────────────────────────────────────────
# Se conservan los nombres publicos historicos; el cuerpo es uno solo. Cada uno
# levanta ExportError si su fuente no se puede consultar.

def export_fills(periodo: str) -> str:
    return export_table("fills", periodo)


def export_pnl(periodo: str) -> str:
    return export_table("pnl", periodo)


def export_positions(periodo: str) -> str:
    return export_table("positions", periodo)


def export_intel(periodo: str) -> str:
    return export_table("intel", periodo)


def export_errors(periodo: str) -> str:
    return export_table("errors", periodo)


def export_dispatch(tipo: str, periodo: str) -> tuple[str, int]:
    """Run the right exporter. Returns (file_path, row_count)."""
    if tipo not in VALID_TYPES:
        raise ValueError(
            f"Invalid type '{tipo}'. Valid: {', '.join(sorted(VALID_TYPES))}"
        )
    if periodo not in VALID_PERIODS:
        raise ValueError(
            f"Invalid period '{periodo}'. Valid: {', '.join(sorted(VALID_PERIODS))}"
        )

    fn = {
        "fills": export_fills,
        "pnl": export_pnl,
        "positions": export_positions,
        "intel": export_intel,
        "errors": export_errors,
    }[tipo]

    path = fn(periodo)

    # El conteo se hace con csv.reader, no contando lineas fisicas. El codigo
    # viejo hacia `for i, _ in enumerate(f)`, que sobre-cuenta cualquier campo
    # con saltos de linea embebidos — y `intel.raw_text` los tiene casi siempre
    # (son tweets y mensajes multilinea). El caption decia "312 rows" sobre 40
    # filas reales. csv.reader respeta el quoting y cuenta registros.
    with open(path, newline="") as f:
        # -1 por la cabecera; max(0, ...) por si el archivo quedara vacio.
        count = max(0, sum(1 for _ in csv.reader(f)) - 1)
    return path, count
