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
        # Use the backup API for consistency under load
        with sqlite3.connect(DB_PATH) as src:
            with sqlite3.connect(dst) as dest:
                src.backup(dest)
        size_mb = os.path.getsize(dst) / (1024 * 1024)
        log.info("SQLite backup OK: %s (%.2f MB)", dst, size_mb)
        cleanup_old_backups(keep=7)
        return {"ok": True, "path": dst, "size_mb": round(size_mb, 2)}
    except Exception as exc:  # noqa: BLE001
        log.exception("SQLite backup failed")
        # Last-resort: shutil copy (less safe under writes but better than nothing)
        try:
            shutil.copy2(DB_PATH, dst)
            return {"ok": True, "path": dst, "fallback": "shutil.copy2"}
        except Exception as exc2:  # noqa: BLE001
            return {"ok": False, "reason": str(exc2)}


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
