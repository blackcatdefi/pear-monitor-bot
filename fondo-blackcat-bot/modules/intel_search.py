"""Round 17 — Full-text search sobre intel_memory.

Crea (lazy) una virtual table FTS5 `intel_fts` content-rowid linkeada a
intel_memory(id). Si SQLite no soporta FTS5, hace fallback a LIKE %term%.

Comando:
    /intel_search <keyword>

Ej:
    /intel_search hormuz
    /intel_search BTC ATH
    /intel_search Trump tariff
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any

from config import DATA_DIR

log = logging.getLogger(__name__)


def _intel_db_path() -> str:
    return os.path.join(DATA_DIR, "intel_memory.db")


def _has_fts5(conn: sqlite3.Connection) -> bool:
    try:
        cur = conn.execute("PRAGMA compile_options")
        opts = {row[0] for row in cur.fetchall()}
        return any("FTS5" in opt for opt in opts)
    except Exception:
        return False


def _ensure_fts(conn: sqlite3.Connection) -> bool:
    """Setup virtual FTS table + triggers. Returns True if FTS5 available + ready."""
    if not _has_fts5(conn):
        return False
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS intel_fts USING fts5(
                source, raw_text, timestamp_utc,
                content='intel_memory',
                content_rowid='id'
            )
            """
        )
        # Triggers (idempotent — IF NOT EXISTS)
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS intel_fts_ai
            AFTER INSERT ON intel_memory BEGIN
                INSERT INTO intel_fts(rowid, source, raw_text, timestamp_utc)
                VALUES (new.id, new.source, new.raw_text, new.timestamp_utc);
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS intel_fts_ad
            AFTER DELETE ON intel_memory BEGIN
                INSERT INTO intel_fts(intel_fts, rowid, source, raw_text, timestamp_utc)
                VALUES ('delete', old.id, old.source, old.raw_text, old.timestamp_utc);
            END
            """
        )
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS intel_fts_au
            AFTER UPDATE ON intel_memory BEGIN
                INSERT INTO intel_fts(intel_fts, rowid, source, raw_text, timestamp_utc)
                VALUES ('delete', old.id, old.source, old.raw_text, old.timestamp_utc);
                INSERT INTO intel_fts(rowid, source, raw_text, timestamp_utc)
                VALUES (new.id, new.source, new.raw_text, new.timestamp_utc);
            END
            """
        )
        # Backfill if FTS empty
        cur = conn.execute("SELECT COUNT(*) AS c FROM intel_fts")
        c_fts = cur.fetchone()
        cur = conn.execute("SELECT COUNT(*) AS c FROM intel_memory")
        c_main = cur.fetchone()
        c_fts_n = (c_fts[0] if c_fts else 0)
        c_main_n = (c_main[0] if c_main else 0)
        if c_fts_n == 0 and c_main_n > 0:
            conn.execute(
                "INSERT INTO intel_fts(rowid, source, raw_text, timestamp_utc) "
                "SELECT id, source, raw_text, timestamp_utc FROM intel_memory"
            )
            log.info("intel_fts backfilled from %d intel_memory rows", c_main_n)
        conn.commit()
        return True
    except Exception:
        log.exception("intel_fts setup failed (will use LIKE fallback)")
        return False


def search_intel(query: str, limit: int = 15) -> list[dict[str, Any]]:
    """Search intel_memory by keyword. Returns list of dicts {ts, source, text}.

    Try FTS5 first; fall back to LIKE on failure.
    """
    if not query or not query.strip():
        return []
    db = _intel_db_path()
    if not os.path.isfile(db):
        return []
    out: list[dict[str, Any]] = []
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        ready = _ensure_fts(conn)
        if ready:
            try:
                # FTS5 MATCH expects a query string
                cur = conn.execute(
                    """
                    SELECT m.id, m.timestamp_utc, m.source, m.raw_text
                    FROM intel_fts f
                    JOIN intel_memory m ON m.id = f.rowid
                    WHERE intel_fts MATCH ?
                    ORDER BY m.timestamp_utc DESC
                    LIMIT ?
                    """,
                    (query, limit),
                )
                for r in cur.fetchall():
                    out.append({
                        "id": r["id"],
                        "ts": r["timestamp_utc"],
                        "source": r["source"],
                        "text": r["raw_text"],
                    })
                conn.close()
                if out:
                    return out
            except Exception:
                log.exception("FTS5 query failed, fallback to LIKE")
        # LIKE fallback (or empty FTS result, try LIKE too)
        terms = [t.strip() for t in query.split() if t.strip()]
        if not terms:
            return out
        sql = "SELECT id, timestamp_utc, source, raw_text FROM intel_memory WHERE 1=1"
        params: list[Any] = []
        for t in terms:
            sql += " AND raw_text LIKE ?"
            params.append(f"%{t}%")
        sql += " ORDER BY timestamp_utc DESC LIMIT ?"
        params.append(limit)
        cur = conn.execute(sql, params)
        for r in cur.fetchall():
            out.append({
                "id": r["id"],
                "ts": r["timestamp_utc"],
                "source": r["source"],
                "text": r["raw_text"],
            })
        conn.close()
    except Exception:
        log.exception("intel search failed")
    return out


def format_search_results(query: str, results: list[dict[str, Any]]) -> str:
    if not results:
        return f"🔍 No results for '{query}'."
    lines = [f"🔍 Results for '{query}' (top {len(results)}):", "─" * 40]
    for r in results:
        ts = (r.get("ts") or "")[:16]
        src = r.get("source") or "?"
        text = (r.get("text") or "").replace("\n", " ").strip()
        lines.append(f"\n📅 {ts} [{src}]")
        lines.append(f"   {text[:300]}{'…' if len(text) > 300 else ''}")
    return "\n".join(lines)
