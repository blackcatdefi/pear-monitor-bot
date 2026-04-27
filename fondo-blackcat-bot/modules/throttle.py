"""Round 16: simple SQLite-backed throttle for expensive commands.

Avoids stacking N concurrent /reporte invocations from the same chat.
"""
from __future__ import annotations

import functools
import logging
import os
import sqlite3
import time
from typing import Awaitable, Callable

from telegram import Update
from telegram.ext import ContextTypes

from config import DATA_DIR

log = logging.getLogger(__name__)

DB_PATH = os.path.join(DATA_DIR, "intel_memory.db")


def _ensure_table() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS throttle_state (
                key TEXT PRIMARY KEY,
                last_called REAL NOT NULL
            )
            """
        )


def is_throttled(key: str, min_interval_s: int) -> tuple[bool, int]:
    """Return (throttled, seconds_until_ok). If not throttled, registers the call now."""
    try:
        _ensure_table()
        now = time.time()
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.execute(
                "SELECT last_called FROM throttle_state WHERE key = ?",
                (key,),
            )
            row = cur.fetchone()
            if row is not None:
                elapsed = now - float(row[0])
                if elapsed < min_interval_s:
                    return True, int(min_interval_s - elapsed)
            conn.execute(
                "INSERT OR REPLACE INTO throttle_state (key, last_called) VALUES (?, ?)",
                (key, now),
            )
        return False, 0
    except Exception:  # noqa: BLE001
        log.exception("throttle: error reading/writing state — letting through")
        return False, 0


HandlerFn = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


def throttle(min_interval_s: int = 60, key_prefix: str | None = None) -> Callable[[HandlerFn], HandlerFn]:
    """Decorator factory: throttles per (handler, user_id) by `min_interval_s` seconds."""

    def decorator(func: HandlerFn) -> HandlerFn:
        @functools.wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            user = update.effective_user
            user_id = str(user.id) if user else "anon"
            prefix = key_prefix or func.__name__
            key = f"{prefix}:{user_id}"
            blocked, wait_s = is_throttled(key, min_interval_s)
            if blocked:
                if update.message is not None:
                    cmd_name = func.__name__.replace("cmd_", "")
                    await update.message.reply_text(
                        f"⏱ /{cmd_name} ya ejecutado hace <{min_interval_s}s. "
                        f"Esperá {wait_s}s.",
                    )
                return
            return await func(update, context)
        return wrapper

    return decorator
