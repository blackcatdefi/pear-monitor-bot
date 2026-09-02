"""Round 16: nightly SQLite backups + weekly cleanup.

Backup target: <DATA_DIR>/backups/intel_memory_YYYYMMDD.db
Cleanup keeps the latest 7 backups.

For Railway (no persistent disk by default), backups still survive container
restarts as long as DATA_DIR is on a Railway volume. If not, they're best-effort.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from datetime import datetime, timezone

from config import DATA_DIR

log = logging.getLogger(__name__)

DB_PATH = os.path.join(DATA_DIR, "intel_memory.db")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")


def _ensure_backup_dir() -> None:
    os.makedirs(BACKUP_DIR, exist_ok=True)


async def backup_sqlite() -> dict:
    """Atomic backup using the SQLite backup API. Idempotent."""
    if not os.path.exists(DB_PATH):
        return {"ok": False, "reason": "db_missing", "path": DB_PATH}
    _ensure_backup_dir()
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    dst = os.path.join(BACKUP_DIR, f"intel_memory_{today}.db")
    try:
        # Use the backup API for consistency under load. `with` sobre una
        # conexion sqlite maneja la TRANSACCION, no la cierra: se cierra a
        # mano para no dejar file descriptors colgados en un job diario.
        src = sqlite3.connect(DB_PATH, timeout=15.0)
        try:
            dest = sqlite3.connect(dst, timeout=15.0)
            try:
                src.backup(dest)
            finally:
                dest.close()
        finally:
            src.close()
        size_mb = os.path.getsize(dst) / (1024 * 1024)
        log.info("SQLite backup OK: %s (%.2f MB)", dst, size_mb)
        cleanup_old_backups(keep=7)
        return {"ok": True, "path": dst, "size_mb": round(size_mb, 2)}
    except Exception as exc:  # noqa: BLE001
        log.exception("SQLite backup failed")
        # R-BOT-DEFINITIVE Fase 5.1 — este fallback devolvia {"ok": True}.
        #
        # shutil.copy2 sobre una sqlite en WAL con el bot escribiendo NO es
        # "menos seguro": produce un archivo en el que las tablas pueden no
        # existir, porque las paginas nuevas viven en el -wal y aca se copia
        # solo el .db. Verificado: con una conexion viva abierta, la copia
        # cruda de una base con 1000 filas devuelve "no such table".
        #
        # O sea que el camino de emergencia devolvia ok=True sobre un archivo
        # inservible, y el unico que miraba el resultado (_backup_job) solo
        # lo logueaba. Se mantiene la copia — algo es mejor que nada — pero
        # ahora se ABRE para comprobar que se pueda leer, y el resultado dice
        # con todas las letras que es un backup degradado.
        try:
            shutil.copy2(DB_PATH, dst)
        except Exception as exc2:  # noqa: BLE001
            return {"ok": False, "reason": f"{exc} / copia: {exc2}"}
        legible, motivo = _es_restaurable(dst)
        return {
            "ok": legible,
            "path": dst,
            "degradado": True,
            "fallback": "shutil.copy2",
            "reason": f"backup API fallo ({str(exc)[:60]}); copia cruda "
                      f"{'legible' if legible else 'INSERVIBLE: ' + motivo}",
        }


def _es_restaurable(path: str) -> tuple[bool, str]:
    """Abre el archivo y comprueba que sea una sqlite con tablas legibles.

    No alcanza con integrity_check: una copia cruda de una base en WAL puede
    pasar el pragma y no tener ni una tabla. Por eso se exige ademas que haya
    al menos una tabla y que se pueda contar.
    """
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0)
    except sqlite3.Error as e:
        return False, f"no abre: {e!s:.50s}"
    try:
        row = con.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            return False, f"integrity_check={row[0] if row else 'sin respuesta'}"
        tablas = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]
        if not tablas:
            return False, "sin tablas"
        for t in tablas:
            con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()
    except sqlite3.Error as e:
        return False, f"lectura: {e!s:.50s}"
    finally:
        con.close()
    return True, "ok"


def cleanup_old_backups(keep: int = 7) -> int:
    if not os.path.isdir(BACKUP_DIR):
        return 0
    files = sorted(
        (f for f in os.listdir(BACKUP_DIR) if f.startswith("intel_memory_") and f.endswith(".db")),
        reverse=True,
    )
    deleted = 0
    for old in files[keep:]:
        try:
            os.remove(os.path.join(BACKUP_DIR, old))
            deleted += 1
        except Exception:  # noqa: BLE001
            log.exception("could not remove old backup %s", old)
    return deleted


def cleanup_sqlite_weekly(days: int = 90) -> dict:
    """Delete entries older than `days` from rotating tables, then VACUUM."""
    deleted: dict[str, int] = {}
    if not os.path.exists(DB_PATH):
        return deleted
    try:
        with sqlite3.connect(DB_PATH) as conn:
            for table, ts_col in [
                ("intel_memory", "timestamp_utc"),
                ("errors_log", "timestamp_utc"),
                ("llm_usage", "timestamp"),
            ]:
                try:
                    cur = conn.execute(
                        f"DELETE FROM {table} WHERE {ts_col} < datetime('now', ?)",
                        (f"-{int(days)} days",),
                    )
                    deleted[table] = cur.rowcount or 0
                except Exception:  # noqa: BLE001
                    deleted[table] = -1
            try:
                conn.execute("VACUUM")
            except Exception:  # noqa: BLE001
                pass
        log.info("Weekly SQLite cleanup: %s", deleted)
    except Exception:  # noqa: BLE001
        log.exception("weekly cleanup failed")
    return deleted
