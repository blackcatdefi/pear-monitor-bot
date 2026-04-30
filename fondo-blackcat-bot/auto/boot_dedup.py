"""R-FINAL — Bug #3 fix: boot announcement spam.

Symptom (apr-30 2026):
    BCD received 5 identical "🟢 BOT ONLINE" Telegram messages within ~5
    minutes (16:03 / 16:05 / 16:06 / 16:06 / 16:08 ART). Every Railway
    cold restart re-fired the announcement.

Root cause:
    R21 introduced ``boot_announcement.announce_boot()`` but with no dedup.
    Every ``post_init`` (whether after deploy, OOM kill, or a transient
    crash + supervisor restart) sends the message.

Fix:
    Persist the timestamp of the last boot announcement in a SQLite file
    under ``DATA_DIR``. Before sending, check if the previous send was
    within the suppression window (default 30 min, env-configurable).
    If yes → log + skip. Else → mark + send.

Public API:
    should_announce() -> bool
        True if the boot announcement should be sent right now.

    mark_announced() -> None
        Record that we just sent the boot announcement.

    last_announcement() -> dict | None
        Diagnostic helper — returns {"ts_utc": iso, "ts_epoch": float, "age_s": int}
        for the last persisted boot.

Kill switch: BOOT_DEDUP_ENABLED=false → should_announce() always True.

Env var: BOOT_DEDUP_WINDOW_MIN=30 (default).

Cache: $DATA_DIR/boot_dedup.db (SQLite).
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

ENABLED = os.getenv("BOOT_DEDUP_ENABLED", "true").strip().lower() != "false"
WINDOW_MIN = max(1, int(os.getenv("BOOT_DEDUP_WINDOW_MIN", "30") or 30))


def _db_path() -> str:
    try:
        from config import DATA_DIR  # type: ignore

        base = DATA_DIR
    except Exception:  # noqa: BLE001
        base = os.path.join(os.path.dirname(__file__), "..", "data")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "boot_dedup.db")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_db_path())
    c.execute(
        """CREATE TABLE IF NOT EXISTS boot_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_epoch REAL NOT NULL,
            ts_utc TEXT NOT NULL
        )"""
    )
    return c


def last_announcement() -> dict | None:
    """Return the most recent boot record, or None if never announced."""
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT ts_epoch, ts_utc FROM boot_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
    except Exception:  # noqa: BLE001
        log.exception("boot_dedup: read failed")
        return None
    if not row:
        return None
    ts_epoch = float(row[0])
    return {
        "ts_epoch": ts_epoch,
        "ts_utc": row[1],
        "age_s": max(0, int(time.time() - ts_epoch)),
    }


def should_announce() -> bool:
    """Decide if a boot announcement should be sent.

    Returns True if either:
      - Module disabled (kill switch).
      - No prior announcement on record.
      - Last announcement was longer than ``WINDOW_MIN`` minutes ago.

    Otherwise returns False (skip + log suppression).
    """
    if not ENABLED:
        return True
    last = last_announcement()
    if last is None:
        return True
    age_s = last["age_s"]
    window_s = WINDOW_MIN * 60
    if age_s >= window_s:
        return True
    log.info(
        "boot_dedup: suppressing boot announcement (último hace %ds, ventana %ds)",
        age_s,
        window_s,
    )
    return False


def mark_announced() -> None:
    """Persist that we just sent the boot announcement."""
    if not ENABLED:
        return
    try:
        with _conn() as c:
            c.execute(
                "INSERT INTO boot_log(ts_epoch, ts_utc) VALUES (?, ?)",
                (time.time(), datetime.now(timezone.utc).isoformat()),
            )
            # keep only last 50 entries to avoid unbounded growth
            c.execute(
                "DELETE FROM boot_log WHERE id NOT IN ("
                "  SELECT id FROM boot_log ORDER BY id DESC LIMIT 50"
                ")"
            )
    except Exception:  # noqa: BLE001
        log.exception("boot_dedup: write failed")


def _reset_for_tests() -> None:
    """Wipe the SQLite store. Test-only."""
    path = _db_path()
    if os.path.isfile(path):
        try:
            os.remove(path)
        except Exception:  # noqa: BLE001
            pass


def _backdate_for_tests(minutes_ago: int) -> None:
    """Force the most recent record to be N minutes in the past. Test-only."""
    target = time.time() - minutes_ago * 60
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT id FROM boot_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row:
                c.execute(
                    "UPDATE boot_log SET ts_epoch=?, ts_utc=? WHERE id=?",
                    (
                        target,
                        datetime.fromtimestamp(target, tz=timezone.utc).isoformat(),
                        row[0],
                    ),
                )
    except Exception:  # noqa: BLE001
        log.exception("boot_dedup: backdate failed")
