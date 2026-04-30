"""Round 21 — Calendar drift guard.

At boot, every catalyst event whose ``timestamp_utc`` is already in the
past is forcibly marked as alerted (``alerted_24h = alerted_2h =
alerted_30m = 1``) so the proactive scheduler will never re-fire alerts
for stale events after a redeploy.

Defensive layer on top of ``scheduler_calendar_v2`` and ``time_awareness``.

Toggle:
    CALENDAR_DRIFT_GUARD_ENABLED=true   (default)

Underlying SQLite table is ``macro_events`` (DATA_DIR/macro_calendar.db),
created by ``modules.macro_calendar``.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Optional

from time_awareness import now_utc

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return os.getenv("CALENDAR_DRIFT_GUARD_ENABLED", "true").strip().lower() != "false"


def _resolve_db_path() -> Optional[str]:
    try:
        from modules.macro_calendar import DB_PATH  # type: ignore[attr-defined]
        return DB_PATH
    except Exception:  # noqa: BLE001
        try:
            from config import DATA_DIR
            return os.path.join(DATA_DIR, "macro_calendar.db")
        except Exception:  # noqa: BLE001
            logger.exception("drift_guard: cannot resolve macro_calendar DB path")
            return None


def mark_past_events_at_boot(db_path: Optional[str] = None) -> int:
    """Mark all past events as alerted to prevent re-firing.

    Args:
        db_path: optional override for the SQLite path. If omitted it is
            resolved from ``modules.macro_calendar.DB_PATH``.

    Returns:
        Number of rows updated (0 if guard disabled, db unreachable, or
        no past events needed marking).
    """
    if not _enabled():
        logger.info("drift_guard: disabled by env (CALENDAR_DRIFT_GUARD_ENABLED=false)")
        return 0

    path = db_path or _resolve_db_path()
    if not path or not os.path.exists(path):
        logger.warning("drift_guard: DB %s does not exist yet — nothing to mark", path)
        return 0

    now_str = now_utc().isoformat()
    affected = 0

    try:
        conn = sqlite3.connect(path)
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE macro_events
               SET alerted_24h = 1,
                   alerted_2h  = 1,
                   alerted_30m = 1
             WHERE timestamp_utc < ?
               AND (alerted_24h = 0 OR alerted_2h = 0 OR alerted_30m = 0)
            """,
            (now_str,),
        )
        affected = cur.rowcount or 0
        conn.commit()
        conn.close()
    except sqlite3.Error:
        logger.exception("drift_guard: SQLite error while marking past events")
        return 0

    if affected > 0:
        logger.info(
            "drift_guard: marked %d past event(s) as alerted (prevent re-fire post-boot)",
            affected,
        )
    else:
        logger.info("drift_guard: no past events needed marking")
    return affected
