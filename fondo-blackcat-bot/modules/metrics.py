"""Round 16: dashboard de salud del bot — /metrics handler payload.

Reads from existing SQLite (intel_memory.db) tables:
    - llm_usage  → tokens + cost por modelo (24h)
    - errors_log → conteo de errores
    - intel_memory → tamaño de memoria intel
    - throttle_state → contador de calls

No new tables — solo agrega lectura.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from config import DATA_DIR

log = logging.getLogger(__name__)

DB_PATH = os.path.join(DATA_DIR, "intel_memory.db")


def _safe_query(query: str, params: tuple = ()) -> list[Any]:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(query, params)
            return cur.fetchall()
    except Exception:  # noqa: BLE001
        return []


def llm_cost_24h() -> dict[str, float]:
    """Sum cost + tokens by model name in last 24h."""
    rows = _safe_query(
        "SELECT model_used, SUM(tokens_in) AS tin, SUM(tokens_out) AS tout, "
        "SUM(cost_usd) AS cost, COUNT(*) AS n "
        "FROM llm_usage WHERE timestamp >= datetime('now', '-1 day') "
        "GROUP BY model_used"
    )
    out: dict[str, float] = {}
    for r in rows:
        model = r["model_used"] or "?"
        out[model] = float(r["cost"] or 0.0)
        out[f"{model}__calls"] = int(r["n"] or 0)
        out[f"{model}__tin"] = int(r["tin"] or 0)
        out[f"{model}__tout"] = int(r["tout"] or 0)
    return out


def error_count_24h() -> int:
    rows = _safe_query(
        "SELECT COUNT(*) AS n FROM errors_log "
        "WHERE timestamp_utc >= datetime('now', '-1 day')"
    )
    if rows:
        try:
            return int(rows[0]["n"])
        except Exception:  # noqa: BLE001
            return 0
    return 0


def intel_memory_count() -> int:
    rows = _safe_query("SELECT COUNT(*) AS n FROM intel_memory")
    if rows:
        try:
            return int(rows[0]["n"])
        except Exception:  # noqa: BLE001
            return 0
    return 0


def sqlite_size_mb() -> float:
    try:
        return os.path.getsize(DB_PATH) / (1024 * 1024)
    except Exception:  # noqa: BLE001
        return 0.0


def x_api_cost_summary() -> dict[str, Any]:
    """Pull cost summary from x_intel module if available."""
    try:
        from modules.x_intel import get_api_stats
        stats = get_api_stats()
        if isinstance(stats, dict):
            return {
                "calls_today": stats.get("count", 0),
                "cost_estimate_usd": float(stats.get("total_cost_estimate_usd", 0.0)),
            }
    except Exception:  # noqa: BLE001
        pass
    return {"calls_today": 0, "cost_estimate_usd": 0.0}


def format_metrics() -> str:
    llm = llm_cost_24h()
    err24 = error_count_24h()
    intel_n = intel_memory_count()
    db_mb = sqlite_size_mb()
    x_summary = x_api_cost_summary()

    lines = [
        "📊 BOT METRICS — last 24h",
        "─" * 30,
        f"❌ Errors: {err24}",
        "",
        "🤖 LLM cost (24h):",
    ]
    if not llm:
        lines.append("  (no data in llm_usage)")
    else:
        # Aggregate per model
        models = sorted({k for k in llm.keys() if "__" not in k})
        for m in models:
            n = llm.get(f"{m}__calls", 0)
            cost = llm.get(m, 0.0)
            tin = llm.get(f"{m}__tin", 0)
            tout = llm.get(f"{m}__tout", 0)
            lines.append(f"  {m}: {n} calls · ${cost:.4f} · in={tin}/out={tout}")

    lines.extend([
        "",
        "💰 X API:",
        f"  calls today: {x_summary['calls_today']}",
        f"  cost est.:   ${x_summary['cost_estimate_usd']:.4f}",
        "",
        "💾 SQLite:",
        f"  intel_memory: {intel_n} entries",
        f"  size on disk: {db_mb:.2f} MB",
        "",
        f"Generado: {datetime.now(timezone.utc).isoformat()}",
    ])
    return "\n".join(lines)
