"""R-PERFECT Phase 3 #4 — source flap-alert detector.

Compares current /selftest matrix against last successful state per source.
Emits a Telegram alert when:

  • LIVE → UNAVAILABLE / TIMEOUT / EXCEPTION for >SOURCE_FLAP_THRESHOLD_HRS
  • UNAVAILABLE → LIVE (recovery, single alert)

Dedup window 24h on the same (source, transition) pair so we don't spam the
operator if a source flaps repeatedly.

State persisted to /app/data/source_state.db so transitions survive restarts.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except (PermissionError, OSError):
    DATA_DIR = Path("/tmp/intel_data")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

STATE_DB_PATH = DATA_DIR / "source_state.db"
FLAP_THRESHOLD_HRS = float(os.getenv("SOURCE_FLAP_THRESHOLD_HRS", "6.0"))
ALERT_DEDUP_HRS = float(os.getenv("SOURCE_ALERT_DEDUP_HRS", "24.0"))

DOWN_STATUSES = {"UNAVAILABLE", "TIMEOUT", "EXCEPTION", "BAD_SHAPE", "IMPORT_FAIL"}
UP_STATUSES = {"LIVE"}
NEUTRAL_STATUSES = {"GRACEFUL_NO_KEY", "DEGRADED", "EMPTY", "UNKNOWN"}


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(STATE_DB_PATH), timeout=2.0)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_state (
            source TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            since_ts INTEGER NOT NULL,
            last_status TEXT,
            last_change_ts INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_alerts_sent (
            source TEXT NOT NULL,
            transition TEXT NOT NULL,
            sent_ts INTEGER NOT NULL,
            PRIMARY KEY (source, transition)
        )
        """
    )
    return conn


def _was_alerted(conn: sqlite3.Connection, source: str,
                 transition: str, now_ts: int) -> bool:
    cutoff = now_ts - int(ALERT_DEDUP_HRS * 3600)
    try:
        cur = conn.execute(
            "SELECT sent_ts FROM source_alerts_sent WHERE source=? AND transition=?",
            (source, transition),
        )
        row = cur.fetchone()
        if row and int(row[0]) >= cutoff:
            return True
    except sqlite3.Error as e:
        log.debug("alert dedup read failed: %s", e)
    return False


def _record_alert(conn: sqlite3.Connection, source: str,
                  transition: str, now_ts: int) -> None:
    try:
        conn.execute(
            "INSERT OR REPLACE INTO source_alerts_sent (source, transition, sent_ts) "
            "VALUES (?, ?, ?)",
            (source, transition, now_ts),
        )
    except sqlite3.Error as e:
        log.debug("alert dedup write failed: %s", e)


def evaluate_matrix(matrix: dict[str, Any]) -> list[str]:
    """Compare matrix to persisted state, return list of alert texts.

    Mutates state DB: updates last_seen status per source.
    """
    rows = matrix.get("rows", [])
    now_ts = int(time.time())
    alerts: list[str] = []
    flap_seconds = int(FLAP_THRESHOLD_HRS * 3600)

    try:
        with _conn() as conn:
            for r in rows:
                if not isinstance(r, dict):
                    continue
                name = r.get("name", "?")
                status = r.get("status", "UNKNOWN")
                cur = conn.execute(
                    "SELECT status, since_ts, last_status FROM source_state WHERE source=?",
                    (name,),
                )
                row = cur.fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO source_state (source, status, since_ts, last_status, last_change_ts) "
                        "VALUES (?, ?, ?, NULL, ?)",
                        (name, status, now_ts, now_ts),
                    )
                    continue
                prev_status, since_ts, last_known = row
                if prev_status == status:
                    # check sustained-down alert
                    if status in DOWN_STATUSES and last_known in UP_STATUSES:
                        downtime = now_ts - int(since_ts)
                        if downtime >= flap_seconds:
                            transition = f"{last_known}->{status}"
                            if not _was_alerted(conn, name, transition, now_ts):
                                alerts.append(
                                    f"⚠️ `{name}` DOWN — was {last_known}, now {status} "
                                    f"for {downtime // 3600}h{(downtime % 3600) // 60}m"
                                )
                                _record_alert(conn, name, transition, now_ts)
                    continue
                # status changed
                conn.execute(
                    "UPDATE source_state SET status=?, since_ts=?, last_status=?, last_change_ts=? "
                    "WHERE source=?",
                    (status, now_ts, prev_status, now_ts, name),
                )
                # recovery alert
                if prev_status in DOWN_STATUSES and status in UP_STATUSES:
                    transition = f"{prev_status}->{status}"
                    if not _was_alerted(conn, name, transition, now_ts):
                        alerts.append(
                            f"✅ `{name}` RECOVERED — back to {status} (was {prev_status})"
                        )
                        _record_alert(conn, name, transition, now_ts)
    except sqlite3.Error as e:
        log.debug("flap evaluate failed: %s", e)
    return alerts


def format_alerts(alerts: list[str]) -> str:
    if not alerts:
        return ""
    return "🚨 *Source flap report*\n" + "\n".join(f"  {a}" for a in alerts)


def get_persisted_state() -> dict[str, dict[str, Any]]:
    """Return current state map for /health introspection."""
    out: dict[str, dict[str, Any]] = {}
    try:
        with _conn() as conn:
            cur = conn.execute(
                "SELECT source, status, since_ts, last_status FROM source_state"
            )
            for source, status, since_ts, last_status in cur:
                out[source] = {
                    "status": status,
                    "since_ts": int(since_ts),
                    "last_status": last_status,
                }
    except sqlite3.Error as e:
        log.debug("state read failed: %s", e)
    return out
