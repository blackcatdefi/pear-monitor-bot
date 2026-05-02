"""Round 16: Persistent error log + decorator + /errors handler payload.

Goals:
    1. Wrap every command handler so unhandled exceptions are captured to
       SQLite *and* surfaced briefly to the user (no silent kills).
    2. Provide a `/errors` payload listing the last 20 entries.

The data lives in `intel_memory.db` (same file the rest of the bot uses)
under a new `errors_log` table — schema created on first call.
"""
from __future__ import annotations

import functools
import logging
import os
import sqlite3
import traceback
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from telegram import Update
from telegram.ext import ContextTypes

from config import DATA_DIR

log = logging.getLogger(__name__)

DB_PATH = os.path.join(DATA_DIR, "intel_memory.db")


def _ensure_table() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS errors_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp_utc TEXT NOT NULL,
                handler TEXT,
                error_type TEXT,
                error_message TEXT,
                traceback TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_errors_log_ts ON errors_log(timestamp_utc)"
        )


def log_error(handler_name: str, exc: BaseException) -> int:
    """Persist a single error. Returns the row id (or -1 on db failure)."""
    try:
        _ensure_table()
        ts = datetime.now(timezone.utc).isoformat()
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.execute(
                "INSERT INTO errors_log "
                "(timestamp_utc, handler, error_type, error_message, traceback) "
                "VALUES (?, ?, ?, ?, ?)",
                (ts, handler_name, type(exc).__name__, str(exc)[:1000], tb[:8000]),
            )
            return cur.lastrowid or -1
    except Exception as db_exc:  # noqa: BLE001
        log.exception("errors_log: could not persist error: %s", db_exc)
        return -1


def fetch_recent(limit: int = 20) -> list[dict[str, Any]]:
    try:
        _ensure_table()
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT id, timestamp_utc, handler, error_type, error_message "
                "FROM errors_log ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]
    except Exception:  # noqa: BLE001
        log.exception("errors_log: fetch_recent failed")
        return []


def count_last_24h() -> int:
    try:
        _ensure_table()
        cutoff = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM errors_log "
                "WHERE timestamp_utc >= datetime('now', '-1 day')",
            )
            return int(cur.fetchone()[0])
    except Exception:  # noqa: BLE001
        return 0


def cleanup_old(days: int = 90) -> int:
    """Delete entries older than `days` days. Returns count deleted."""
    try:
        _ensure_table()
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.execute(
                "DELETE FROM errors_log WHERE timestamp_utc < datetime('now', ?)",
                (f"-{int(days)} days",),
            )
            deleted = cur.rowcount or 0
            conn.execute("VACUUM")
        return deleted
    except Exception:  # noqa: BLE001
        log.exception("errors_log cleanup failed")
        return 0


HandlerFn = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


def with_error_logging(func: HandlerFn) -> HandlerFn:
    """Decorator: persist exception + reply briefly to user, never crash the bot."""

    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            return await func(update, context)
        except Exception as exc:  # noqa: BLE001
            handler_name = func.__name__
            row_id = log_error(handler_name, exc)
            log.exception("Handler %s failed (errors_log id=%s)", handler_name, row_id)
            try:
                cmd_name = handler_name.replace("cmd_", "").replace("handle_", "")
                msg = f"❌ Error en /{cmd_name}: {str(exc)[:200]}"
                if update.message is not None:
                    await update.message.reply_text(msg)
            except Exception:  # noqa: BLE001
                pass  # do not raise — error is already persisted

    return wrapper


def format_recent(limit: int = 20) -> str:
    rows = fetch_recent(limit=limit)
    if not rows:
        return "📋 Errors log empty (no errors captured yet)."
    lines = ["📋 RECENT ERRORS", "─" * 30]
    for r in rows:
        ts = (r.get("timestamp_utc") or "")[:19].replace("T", " ")
        h = (r.get("handler") or "?").replace("cmd_", "/")
        et = r.get("error_type") or "?"
        em = (r.get("error_message") or "")[:120]
        lines.append(f"#{r['id']} [{ts}] {h}")
        lines.append(f"  {et}: {em}")
    return "\n".join(lines)
