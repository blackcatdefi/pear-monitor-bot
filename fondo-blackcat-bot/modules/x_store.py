"""R-COST-V2 (2026-07-23) — persistent X tweet store + monthly post budget.

THE PROBLEM (owner evidence, X Developer Console Jun 23–Jul 23 2026):
$106.21 for 21,240 posts read (real rate ≈ $0.005/post). The bot re-fetched
the FULL 48h window of the X List on every /reporte, paying repeatedly for
the same tweets.

THE FIX — never pay twice:
  * Every fetched tweet is persisted here (SQLite on the Railway volume,
    same intel_memory.db file) keyed by tweet id.
  * Each /reporte fetches ONLY tweets newer than the stored since_id.
  * The 48h timeline view is assembled from the LOCAL store
    (new fetch + previously stored tweets inside the window).
  * Entries older than X_STORE_RETENTION_HOURS (72h) are pruned.
  * A monthly post budget (X_MONTHLY_POST_BUDGET, default 8000) gates live
    fetches: at 80% one warning push; at 100% /reporte renders from cache
    only with an explicit banner. X_BUDGET_OVERRIDE=true bypasses (emergency).

Owner design decisions (do NOT deviate):
  * The X List composition is curated BY THE OWNER inside X. The store never
    filters accounts — it stores whatever the complete list returns.
  * X reads happen EXCLUSIVELY inside the /reporte flow (+ manual /xrefresh).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from config import DATA_DIR

log = logging.getLogger(__name__)

DB_PATH = os.path.join(DATA_DIR, "intel_memory.db")

# Retention: window shown is 48h; keep 72h so the view is always assemblable
# even right after a prune.
RETENTION_HOURS = int(os.getenv("X_STORE_RETENTION_HOURS", "72"))

# Monthly post budget (calendar month, UTC). At the real $0.005/post rate the
# default 8000 caps X spend at $40/mo worst case; actual usage with the
# incremental fetch should land far below.
MONTHLY_POST_BUDGET = int(os.getenv("X_MONTHLY_POST_BUDGET", "8000"))
# Emergency override: when true, the 100% budget gate does NOT block live
# fetches (the 80% warning still fires). For "I need fresh data NOW" moments.
BUDGET_OVERRIDE = os.getenv("X_BUDGET_OVERRIDE", "false").strip().lower() in (
    "true", "1", "yes", "on"
)

COST_PER_POST_USD = 0.005  # measured: $106.21 / 21,240 posts (Jun-Jul 2026)


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS x_tweets (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            name TEXT DEFAULT '',
            verified INTEGER DEFAULT 0,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            metrics_json TEXT DEFAULT '{}',
            url TEXT DEFAULT '',
            source TEXT DEFAULT 'list',
            fetched_at TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_x_tweets_created ON x_tweets(created_at)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS x_fetch_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    # Shared tables normally created by intel_memory._get_conn (same DB file).
    # Mirrored here so x_store works standalone (fresh volume, unit tests).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS x_api_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            endpoint TEXT NOT NULL,
            status INTEGER NOT NULL,
            pages INTEGER DEFAULT 1,
            tweets_returned INTEGER DEFAULT 0,
            est_cost_usd REAL DEFAULT 0,
            caller TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS x_api_alerts (
            key TEXT PRIMARY KEY,
            last_sent_utc TEXT NOT NULL,
            payload TEXT
        )
    """)
    conn.commit()
    return conn


# ─── since_id state ─────────────────────────────────────────────────────────

def get_since_id() -> str | None:
    """Highest tweet id already persisted for the list (None = first run)."""
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT value FROM x_fetch_state WHERE key='list_since_id'"
        ).fetchone()
        conn.close()
        return row["value"] if row and row["value"] else None
    except Exception:
        log.exception("x_store.get_since_id failed")
        return None


def set_since_id(tweet_id: str) -> None:
    """Persist the new high-water mark. Only ever moves forward."""
    if not tweet_id:
        return
    try:
        cur = get_since_id()
        if cur is not None:
            try:
                if int(tweet_id) <= int(cur):
                    return  # never move backwards
            except (TypeError, ValueError):
                pass
        conn = _get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO x_fetch_state (key, value, updated_at) "
            "VALUES ('list_since_id', ?, ?)",
            (str(tweet_id), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception:
        log.exception("x_store.set_since_id failed")


def last_fetch_ts() -> datetime | None:
    """UTC ts of the last successful live fetch persisted into the store."""
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT updated_at FROM x_fetch_state WHERE key='list_since_id'"
        ).fetchone()
        conn.close()
        if not row:
            return None
        ts = datetime.fromisoformat(row["updated_at"])
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except Exception:
        return None


# ─── Tweet persistence ──────────────────────────────────────────────────────

def upsert_tweets(tweets: list[dict[str, Any]], source: str = "list") -> int:
    """Insert fetched tweets keyed by id; returns count of NEW rows.

    since_id already guarantees the API never returns a tweet twice, but
    INSERT OR IGNORE makes the store idempotent under retries/backfills too.
    """
    if not tweets:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    inserted = 0
    try:
        conn = _get_conn()
        for t in tweets:
            tid = str(t.get("id") or "").strip()
            if not tid:
                # Legacy dicts carry the id only inside the URL — recover it.
                url = str(t.get("url") or "")
                tid = url.rsplit("/", 1)[-1] if "/" in url else ""
            if not tid:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO x_tweets "
                "(id, username, name, verified, text, created_at, metrics_json, url, source, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tid,
                    t.get("username") or "unknown",
                    t.get("name") or "",
                    1 if t.get("verified") else 0,
                    t.get("text") or "",
                    t.get("created_at") or now,
                    json.dumps(t.get("metrics") or {}),
                    t.get("url") or "",
                    source,
                    now,
                ),
            )
            inserted += cur.rowcount
        conn.commit()
        conn.close()
    except Exception:
        log.exception("x_store.upsert_tweets failed")
    return inserted


def prune_old(hours: int | None = None) -> int:
    """Delete tweets older than the retention window. Returns rows deleted."""
    hours = RETENTION_HOURS if hours is None else hours
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        conn = _get_conn()
        cur = conn.execute("DELETE FROM x_tweets WHERE created_at < ?", (cutoff,))
        conn.commit()
        deleted = cur.rowcount
        conn.close()
        if deleted:
            log.info("[X_STORE] pruned %d tweets older than %dh", deleted, hours)
        return deleted
    except Exception:
        log.exception("x_store.prune_old failed")
        return 0


def get_window(hours: int = 48) -> list[dict[str, Any]]:
    """Assemble the timeline window from the LOCAL store (x_intel dict shape)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    out: list[dict[str, Any]] = []
    try:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM x_tweets WHERE created_at >= ? ORDER BY created_at DESC",
            (cutoff,),
        ).fetchall()
        conn.close()
        for r in rows:
            try:
                metrics = json.loads(r["metrics_json"] or "{}")
            except Exception:
                metrics = {}
            out.append({
                "id": r["id"],
                "username": r["username"],
                "name": r["name"],
                "verified": bool(r["verified"]),
                "text": r["text"],
                "created_at": r["created_at"],
                "metrics": metrics,
                "url": r["url"],
            })
    except Exception:
        log.exception("x_store.get_window failed")
    return out


def store_stats() -> dict[str, Any]:
    """Store size/age metrics for /costs and /debug_x."""
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(created_at) AS oldest, MAX(created_at) AS newest, "
            "COUNT(DISTINCT username) AS accounts FROM x_tweets"
        ).fetchone()
        conn.close()
        newest = row["newest"]
        age_h = None
        if newest:
            try:
                ts = datetime.fromisoformat(newest)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
            except Exception:
                age_h = None
        lf = last_fetch_ts()
        lf_iso = lf.isoformat() if lf else None
        n = int(row["n"] or 0)
        return {
            "tweets": n,
            "total_tweets": n,                       # alias
            "accounts": int(row["accounts"] or 0),
            "oldest": row["oldest"],
            "oldest_created_at": row["oldest"],      # alias
            "newest": newest,
            "newest_created_at": newest,             # alias
            "newest_age_hours": age_h,
            "since_id": get_since_id(),
            "last_fetch": lf_iso,
            "last_fetch_ts": lf_iso,                 # alias
            "retention_hours": RETENTION_HOURS,
        }
    except Exception:
        log.exception("x_store.store_stats failed")
        return {"tweets": 0, "total_tweets": 0, "accounts": 0,
                "oldest": None, "oldest_created_at": None,
                "newest": None, "newest_created_at": None,
                "newest_age_hours": None, "since_id": None,
                "last_fetch": None, "last_fetch_ts": None,
                "retention_hours": RETENTION_HOURS}


# ─── Monthly post budget (reads x_api_calls written by intel_memory) ────────

def posts_fetched_since(start_iso: str) -> int:
    """SUM(tweets_returned) from x_api_calls since a UTC ISO timestamp."""
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT COALESCE(SUM(tweets_returned),0) AS n FROM x_api_calls WHERE ts >= ?",
            (start_iso,),
        ).fetchone()
        conn.close()
        return int(row["n"] or 0)
    except Exception:
        log.exception("x_store.posts_fetched_since failed")
        return 0


def posts_fetched_today() -> int:
    day0 = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return posts_fetched_since(day0.isoformat())


def posts_fetched_month() -> int:
    now = datetime.now(timezone.utc)
    m0 = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return posts_fetched_since(m0.isoformat())


def budget_state() -> dict[str, Any]:
    """Current budget picture: used, %, projections at $0.005/post."""
    used = posts_fetched_month()
    budget = max(1, MONTHLY_POST_BUDGET)
    pct = used / budget * 100.0  # percentage 0–100
    now = datetime.now(timezone.utc)
    day_of_month = max(1, now.day)
    # days in current month
    nxt = (now.replace(day=28) + timedelta(days=4)).replace(day=1)
    days_in_month = (nxt - now.replace(day=1)).days
    projected_posts = int(used / day_of_month * days_in_month)
    return {
        "budget": budget,
        "used": used,
        "pct": pct,
        "exhausted": (used >= budget) and not BUDGET_OVERRIDE,
        "override": BUDGET_OVERRIDE,
        "today": posts_fetched_today(),
        "projected_month_posts": projected_posts,
        "projected_month_cost_usd": projected_posts * COST_PER_POST_USD,
        "mtd_cost_usd": used * COST_PER_POST_USD,
    }


def should_send_budget_warning(pct: float | None = None) -> bool:
    """True once per calendar month when usage crosses 80% of the budget.

    Uses the existing x_api_alerts table (intel_memory) for throttling so the
    state survives redeploys and joins the critical-ops push path.
    """
    st = budget_state() if pct is None else None
    p = st["pct"] if st is not None else pct
    if p is None or p < 80.0:
        return False
    try:
        conn = _get_conn()
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        key = f"x_budget_80pct_{month}"
        row = conn.execute(
            "SELECT last_sent_utc FROM x_api_alerts WHERE key=?", (key,)
        ).fetchone()
        if row:
            conn.close()
            return False
        conn.execute(
            "INSERT OR REPLACE INTO x_api_alerts (key, last_sent_utc, payload) "
            "VALUES (?, ?, ?)",
            (key, datetime.now(timezone.utc).isoformat(),
             json.dumps({"pct": p})),
        )
        conn.commit()
        conn.close()
        return True
    except Exception:
        log.exception("x_store.should_send_budget_warning failed")
        return False
