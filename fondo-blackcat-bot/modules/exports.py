"""Round 17 — Export de datos del fondo a CSV.

Comando: /export <tipo> <periodo>
    tipos: fills, pnl, positions, intel, errors
    periodos: 7d, 30d, 90d, ytd, all

Output: archivo CSV en DATA_DIR/exports/, listo para enviar como Document.
"""
from __future__ import annotations

import csv
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from config import DATA_DIR

log = logging.getLogger(__name__)

_EXPORT_DIR = os.path.join(DATA_DIR, "exports")
os.makedirs(_EXPORT_DIR, exist_ok=True)

VALID_TYPES = {"fills", "pnl", "positions", "intel", "errors"}
VALID_PERIODS = {"7d", "30d", "90d", "ytd", "all"}


def _cutoff(period: str) -> datetime | None:
    now = datetime.now(timezone.utc)
    if period == "7d":
        return now - timedelta(days=7)
    if period == "30d":
        return now - timedelta(days=30)
    if period == "90d":
        return now - timedelta(days=90)
    if period == "ytd":
        return datetime(now.year, 1, 1, tzinfo=timezone.utc)
    return None  # all


def _output_path(tipo: str, periodo: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    fname = f"fondo_blackcat_{tipo}_{periodo}_{ts}.csv"
    return os.path.join(_EXPORT_DIR, fname)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cur.fetchone() is not None


def export_fills(periodo: str) -> str:
    """Export from snapshots / position_log if available, else from intel_memory recent_fills cache."""
    out_path = _output_path("fills", periodo)
    cutoff = _cutoff(periodo)
    cutoff_iso = cutoff.isoformat() if cutoff else None
    rows: list[dict] = []

    # Try position_log table
    db = os.path.join(DATA_DIR, "intel_memory.db")
    if os.path.isfile(db):
        try:
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            if _table_exists(conn, "position_log"):
                if cutoff_iso:
                    cur = conn.execute(
                        "SELECT * FROM position_log WHERE timestamp_utc >= ? ORDER BY timestamp_utc DESC",
                        (cutoff_iso,),
                    )
                else:
                    cur = conn.execute("SELECT * FROM position_log ORDER BY timestamp_utc DESC")
                for r in cur.fetchall():
                    rows.append({k: r[k] for k in r.keys()})
            conn.close()
        except Exception:
            log.exception("position_log export failed")

    if not rows:
        # write header-only CSV so user knows file generated
        with open(out_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp_utc", "kind", "asset", "amount_usd", "wallet_label", "message"])
        return out_path

    fields = list(rows[0].keys())
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow(row)
    return out_path


def export_pnl(periodo: str) -> str:
    out_path = _output_path("pnl", periodo)
    cutoff = _cutoff(periodo)
    cutoff_iso = cutoff.isoformat() if cutoff else None
    rows: list[dict] = []

    db = os.path.join(DATA_DIR, "intel_memory.db")
    if os.path.isfile(db):
        try:
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            if _table_exists(conn, "pnl_events"):
                if cutoff_iso:
                    cur = conn.execute(
                        "SELECT * FROM pnl_events WHERE timestamp >= ? ORDER BY timestamp DESC",
                        (cutoff_iso,),
                    )
                else:
                    cur = conn.execute("SELECT * FROM pnl_events ORDER BY timestamp DESC")
                for r in cur.fetchall():
                    rows.append({k: r[k] for k in r.keys()})
            conn.close()
        except Exception:
            log.exception("pnl_events export failed")

    if not rows:
        with open(out_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "category", "asset", "amount_usd", "note"])
        return out_path

    fields = list(rows[0].keys())
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow(row)
    return out_path


def export_positions(periodo: str) -> str:
    """Snapshot CURRENT positions only (periodo ignored)."""
    out_path = _output_path("positions", periodo)
    rows: list[dict] = []
    try:
        # Read snapshots table if available (from modules/snapshots.py)
        db = os.path.join(DATA_DIR, "intel_memory.db")
        if os.path.isfile(db):
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            if _table_exists(conn, "snapshots"):
                cutoff = _cutoff(periodo)
                cutoff_iso = cutoff.isoformat() if cutoff else None
                if cutoff_iso:
                    cur = conn.execute(
                        "SELECT * FROM snapshots WHERE timestamp_utc >= ? ORDER BY timestamp_utc DESC",
                        (cutoff_iso,),
                    )
                else:
                    cur = conn.execute("SELECT * FROM snapshots ORDER BY timestamp_utc DESC")
                for r in cur.fetchall():
                    rows.append({k: r[k] for k in r.keys()})
            conn.close()
    except Exception:
        log.exception("snapshots export failed")

    if not rows:
        with open(out_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp_utc", "wallet", "data_json"])
        return out_path

    fields = list(rows[0].keys())
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow(row)
    return out_path


def export_intel(periodo: str) -> str:
    out_path = _output_path("intel", periodo)
    cutoff = _cutoff(periodo)
    cutoff_iso = cutoff.isoformat() if cutoff else None
    rows: list[dict] = []

    db = os.path.join(DATA_DIR, "intel_memory.db")
    if os.path.isfile(db):
        try:
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            if _table_exists(conn, "intel_memory"):
                if cutoff_iso:
                    cur = conn.execute(
                        """
                        SELECT id, timestamp_utc, source, raw_text, parsed_summary, tags
                        FROM intel_memory
                        WHERE timestamp_utc >= ?
                        ORDER BY timestamp_utc DESC
                        """,
                        (cutoff_iso,),
                    )
                else:
                    cur = conn.execute(
                        """
                        SELECT id, timestamp_utc, source, raw_text, parsed_summary, tags
                        FROM intel_memory
                        ORDER BY timestamp_utc DESC
                        """
                    )
                for r in cur.fetchall():
                    rows.append({k: r[k] for k in r.keys()})
            conn.close()
        except Exception:
            log.exception("intel export failed")

    fields = ["id", "timestamp_utc", "source", "raw_text", "parsed_summary", "tags"]
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fields})
    return out_path


def export_errors(periodo: str) -> str:
    out_path = _output_path("errors", periodo)
    cutoff = _cutoff(periodo)
    cutoff_iso = cutoff.isoformat() if cutoff else None
    rows: list[dict] = []

    db = os.path.join(DATA_DIR, "intel_memory.db")
    if os.path.isfile(db):
        try:
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            if _table_exists(conn, "errors_log"):
                if cutoff_iso:
                    cur = conn.execute(
                        "SELECT * FROM errors_log WHERE timestamp_utc >= ? ORDER BY timestamp_utc DESC",
                        (cutoff_iso,),
                    )
                else:
                    cur = conn.execute("SELECT * FROM errors_log ORDER BY timestamp_utc DESC")
                for r in cur.fetchall():
                    rows.append({k: r[k] for k in r.keys()})
            conn.close()
        except Exception:
            log.exception("errors export failed")

    if not rows:
        with open(out_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp_utc", "handler", "error_type", "error_message", "stacktrace"])
        return out_path

    fields = list(rows[0].keys())
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow(row)
    return out_path


def export_dispatch(tipo: str, periodo: str) -> tuple[str, int]:
    """Run the right exporter. Returns (file_path, row_count)."""
    if tipo not in VALID_TYPES:
        raise ValueError(
            f"Invalid type '{tipo}'. Valid: {', '.join(sorted(VALID_TYPES))}"
        )
    if periodo not in VALID_PERIODS:
        raise ValueError(
            f"Invalid period '{periodo}'. Valid: {', '.join(sorted(VALID_PERIODS))}"
        )

    fn = {
        "fills": export_fills,
        "pnl": export_pnl,
        "positions": export_positions,
        "intel": export_intel,
        "errors": export_errors,
    }[tipo]

    path = fn(periodo)
    # Count rows (excl header)
    count = 0
    try:
        with open(path) as f:
            for i, _ in enumerate(f):
                count = i  # excl header
    except Exception:
        pass
    return path, count
