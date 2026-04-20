"""Persistent memory of all intel gathered by the bot.

Every time the bot reads Telegram, X, Gmail, or any data source —
even during failed /reporte executions — the raw intel gets saved here.
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from config import DATA_DIR

log = logging.getLogger(__name__)

DB_PATH = os.path.join(DATA_DIR, "intel_memory.db")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS intel_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp_utc TEXT NOT NULL,
            source TEXT NOT NULL,
            raw_text TEXT NOT NULL,
            parsed_summary TEXT,
            tags TEXT,
            processed_for_thesis INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    return conn


def save_intel(source: str, raw_text: str, parsed_summary: str | None = None, tags: list[str] | None = None) -> int:
    """Save intel entry to database."""
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO intel_memory (timestamp_utc, source, raw_text, parsed_summary, tags) VALUES (?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), source, raw_text, parsed_summary, json.dumps(tags or []))
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def get_recent_intel(hours: int = 24) -> dict[str, list[dict]]:
    """Get recent intel grouped by source."""
    conn = _get_conn()
    cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff = cutoff_dt.isoformat()
    rows = conn.execute(
        "SELECT * FROM intel_memory WHERE timestamp_utc >= ? ORDER BY timestamp_utc DESC",
        (cutoff,)
    ).fetchall()
    conn.close()
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        source = r["source"]
        grouped.setdefault(source, []).append(dict(r))
    return grouped


def get_unprocessed_count() -> int:
    """Count intel entries not yet processed by analysis."""
    conn = _get_conn()
    count = conn.execute("SELECT COUNT(*) FROM intel_memory WHERE processed_for_thesis = 0").fetchone()[0]
    conn.close()
    return count


def mark_as_processed(ids: list[int]) -> None:
    """Mark intel entries as processed."""
    if not ids:
        return
    conn = _get_conn()
    placeholders = ",".join("?" * len(ids))
    conn.execute(f"UPDATE intel_memory SET processed_for_thesis = 1 WHERE id IN ({placeholders})", ids)
    conn.commit()
    conn.close()


def format_intel_summary(hours: int = 24, source_filter: str | None = None) -> str:
    """Format intel memory for display in Telegram."""
    grouped = get_recent_intel(hours)
    if source_filter:
        grouped = {k: v for k, v in grouped.items() if k == source_filter}

    if not grouped:
        return f"📥 Sin intel registrada en las últimas {hours}h"

    total = sum(len(v) for v in grouped.values())
    lines = [f"📥 INTEL MEMORY — últimas {hours}h ({total} items)\n"]

    for source, items in sorted(grouped.items()):
        lines.append(f"{source.upper()} ({len(items)} items):")
        for item in items[:10]:  # Show max 10 per source
            text_preview = (item.get("raw_text") or "")[:80]
            ts = item.get("timestamp_utc", "")[:16]
            tags = json.loads(item.get("tags") or "[]")
            tag_str = f" [{', '.join(tags)}]" if tags else ""
            lines.append(f"  {ts} —{tag_str} {text_preview}")
        if len(items) > 10:
            lines.append(f"  ... y {len(items) - 10} más")
        lines.append("")

    unprocessed = get_unprocessed_count()
    lines.append(f"Status: {total - unprocessed}/{total} procesados por análisis IA")
    return "\n".join(lines)


def cleanup_old(days: int = 7) -> int:
    """Remove intel older than N days. Returns count deleted."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = _get_conn()
    cur = conn.execute("DELETE FROM intel_memory WHERE timestamp_utc < ?", (cutoff,))
    conn.commit()
    deleted = cur.rowcount
    conn.close()
    return deleted
