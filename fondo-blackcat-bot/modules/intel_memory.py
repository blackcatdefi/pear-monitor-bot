"""Persistent memory of all intel gathered by the bot.

Every time the bot reads Telegram, X, Gmail, or any data source
— even during failed /reporte executions — the raw intel gets saved here.

Round 7 additions:
    - `llm_usage` table to make /providers counters survive redeploys.
    - `unlock_schedule` table for cached token unlock events (6h TTL).
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
    # Round 7: persistent LLM usage tracking (was in-memory; reset on redeploy)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS llm_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            task_name TEXT,
            model_used TEXT,
            tokens_in INTEGER DEFAULT 0,
            tokens_out INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0,
            success INTEGER DEFAULT 1
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_usage_model ON llm_usage(model_used)")
    # Round 7: unlock schedule cache (DropsTab / Tokenomist, 6h refresh)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS unlock_schedule (
            token TEXT NOT NULL,
            next_unlock_ts INTEGER NOT NULL,
            amount_tokens REAL,
            value_usd REAL,
            pct_supply REAL,
            category TEXT,
            source TEXT,
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (token, next_unlock_ts)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_unlock_ts ON unlock_schedule(next_unlock_ts)")
    conn.commit()
    return conn


def save_intel(
    source: str,
    raw_text: str,
    parsed_summary: str | None = None,
    tags: list[str] | None = None,
) -> int:
    """Save intel entry to database."""
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO intel_memory (timestamp_utc, source, raw_text, parsed_summary, tags) VALUES (?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), source, raw_text, parsed_summary, json.dumps(tags or [])),
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
        (cutoff,),
    ).fetchall()
    conn.close()

    grouped: dict[str, list[dict]] = {}
    for r in rows:
        source = r["source"]
        grouped.setdefault(source, []).append(dict(r))
    return grouped


def get_unprocessed_count() -> int:
    conn = _get_conn()
    count = conn.execute("SELECT COUNT(*) FROM intel_memory WHERE processed_for_thesis = 0").fetchone()[0]
    conn.close()
    return count


def get_unprocessed_intel(limit: int = 50) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, source, raw_text, timestamp_utc FROM intel_memory "
        "WHERE processed_for_thesis = 0 ORDER BY timestamp_utc ASC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_intel_item(
    item_id: int,
    parsed_summary: str | None = None,
    tags: list[str] | None = None,
) -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE intel_memory SET parsed_summary = ?, tags = ? WHERE id = ?",
        (parsed_summary, json.dumps(tags or []), item_id),
    )
    conn.commit()
    conn.close()


def mark_as_processed(ids: list[int]) -> None:
    if not ids:
        return
    conn = _get_conn()
    placeholders = ",".join("?" * len(ids))
    conn.execute(f"UPDATE intel_memory SET processed_for_thesis = 1 WHERE id IN ({placeholders})", ids)
    conn.commit()
    conn.close()


def format_intel_summary(hours: int = 24, source_filter: str | None = None) -> str:
    grouped = get_recent_intel(hours)

    if source_filter:
        grouped = {k: v for k, v in grouped.items() if k == source_filter}

    if not grouped:
        return f"\U0001f4e5 Sin intel registrada en las \u00faltimas {hours}h"

    total = sum(len(v) for v in grouped.values())
    lines = [f"\U0001f4e5 INTEL MEMORY \u2014 \u00faltimas {hours}h ({total} items)\n"]

    for source, items in sorted(grouped.items()):
        lines.append(f"{source.upper()} ({len(items)} items):")
        for item in items[:10]:
            text_preview = (item.get("raw_text") or "")[:80]
            ts = item.get("timestamp_utc", "")[:16]
            tags = json.loads(item.get("tags") or "[]")
            tag_str = f" [{', '.join(tags)}]" if tags else ""
            lines.append(f"  {ts} \u2014{tag_str} {text_preview}")
        if len(items) > 10:
            lines.append(f"  ... y {len(items) - 10} m\u00e1s")
        lines.append("")

    unprocessed = get_unprocessed_count()
    lines.append(f"Status: {total - unprocessed}/{total} procesados por an\u00e1lisis IA")

    return "\n".join(lines)


def cleanup_old(days: int = 7) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = _get_conn()
    cur = conn.execute("DELETE FROM intel_memory WHERE timestamp_utc < ?", (cutoff,))
    conn.commit()
    deleted = cur.rowcount
    conn.close()
    return deleted


# ─── Round 7: LLM usage tracking (persistent /providers counters) ────────────

def track_llm_usage(
    task_name: str,
    model_used: str,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: float = 0.0,
    success: bool = True,
) -> None:
    """Persist a single LLM call. Called from llm_router on each success/error."""
    try:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO llm_usage (task_name, model_used, tokens_in, tokens_out, cost_usd, success) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task_name, model_used, int(tokens_in or 0), int(tokens_out or 0),
             float(cost_usd or 0.0), 1 if success else 0),
        )
        conn.commit()
        conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("track_llm_usage failed (%s): %s", model_used, exc)


def get_usage_stats(period: str = "today") -> list[dict]:
    """Aggregate usage by model for a given period.

    period: 'session' (last 1h) | 'today' | 'month' | 'all'.
    """
    where_map = {
        "session": "timestamp >= datetime('now', '-1 hour')",
        "today": "date(timestamp) = date('now')",
        "month": "strftime('%Y-%m', timestamp) = strftime('%Y-%m', 'now')",
        "all": "1=1",
    }
    where = where_map.get(period, where_map["today"])
    try:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT model_used, "
            "       COUNT(*) AS reqs, "
            "       SUM(tokens_in) AS tin, "
            "       SUM(tokens_out) AS tout, "
            "       SUM(cost_usd) AS cost, "
            "       SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS errors, "
            "       MAX(timestamp) AS last_ts "
            f"FROM llm_usage WHERE {where} "
            "GROUP BY model_used"
        ).fetchall()
        conn.close()
        return [
            {
                "model_used": r["model_used"] or "unknown",
                "reqs": r["reqs"] or 0,
                "tokens_in": r["tin"] or 0,
                "tokens_out": r["tout"] or 0,
                "cost_usd": r["cost"] or 0.0,
                "errors": r["errors"] or 0,
                "last_ts": r["last_ts"],
            }
            for r in rows
        ]
    except Exception as exc:  # noqa: BLE001
        log.warning("get_usage_stats(%s) failed: %s", period, exc)
        return []


# ─── Round 7: unlock schedule cache ─────────────────────────────────────────

def save_unlock_events(events: list[dict]) -> int:
    """Upsert unlock events. Replaces prior rows for (token, next_unlock_ts)."""
    if not events:
        return 0
    conn = _get_conn()
    inserted = 0
    for ev in events:
        try:
            conn.execute(
                "INSERT OR REPLACE INTO unlock_schedule "
                "(token, next_unlock_ts, amount_tokens, value_usd, pct_supply, category, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    (ev.get("token") or ev.get("symbol") or "").upper(),
                    int(ev.get("next_unlock_ts") or ev.get("timestamp") or 0),
                    float(ev.get("amount_tokens") or ev.get("tokens") or 0),
                    float(ev.get("value_usd") or 0),
                    float(ev.get("pct_supply") or ev.get("float_pct") or 0),
                    ev.get("category") or ev.get("type"),
                    ev.get("source") or "unknown",
                ),
            )
            inserted += 1
        except Exception as exc:  # noqa: BLE001
            log.debug("save_unlock_events skip %s: %s", ev.get("token"), exc)
    conn.commit()
    conn.close()
    return inserted


def get_cached_unlocks(window_days: int = 14, max_age_hours: int = 6) -> list[dict]:
    """Return cached unlocks if the most-recent fetch is fresh enough."""
    now = int(datetime.now(timezone.utc).timestamp())
    horizon = now + window_days * 86400
    try:
        conn = _get_conn()
        cutoff_row = conn.execute(
            "SELECT MAX(fetched_at) AS last_fetch FROM unlock_schedule"
        ).fetchone()
        last_fetch = cutoff_row["last_fetch"] if cutoff_row else None
        if not last_fetch:
            conn.close()
            return []
        try:
            last_dt = datetime.fromisoformat(last_fetch.replace("Z", "+00:00"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            last_dt = datetime.now(timezone.utc)
        age_h = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
        if age_h > max_age_hours:
            conn.close()
            return []
        rows = conn.execute(
            "SELECT token, next_unlock_ts, amount_tokens, value_usd, pct_supply, "
            "       category, source "
            "FROM unlock_schedule WHERE next_unlock_ts BETWEEN ? AND ? "
            "ORDER BY next_unlock_ts ASC",
            (now, horizon),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        log.warning("get_cached_unlocks failed: %s", exc)
        return []
