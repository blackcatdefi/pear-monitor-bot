"""R-PERFECT Phase 3 #3 — LLM cost tracker.

Records every LLM call with model, tokens in/out, and USD estimate to a SQLite
DB at /app/data/cost.db. Used for:

  • /cost — last 7d breakdown by model + total USD
  • daily/monthly threshold alerts (>$3/day, >$50/mo)

Pricing table is Anthropic + Google list prices as of 2026-05-08; update via
PRICING_USD_PER_MTOK env override if rates change.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except (PermissionError, OSError):
    DATA_DIR = Path("/tmp/intel_data")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

COST_DB_PATH = DATA_DIR / "cost.db"

# USD per million tokens — input / output
DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-6":      (15.0, 75.0),
    "claude-opus-4":        (15.0, 75.0),
    "claude-sonnet-4-6":    (3.0, 15.0),
    "claude-sonnet-4":      (3.0, 15.0),
    "claude-3-7-sonnet":    (3.0, 15.0),
    "claude-haiku-4-5":     (1.0, 5.0),
    "claude-haiku-4":       (1.0, 5.0),
    "gemini-2.0-flash":     (0.0, 0.0),    # free tier
    "gemini-2.5-flash":     (0.0, 0.0),
    "gemini-pro":           (1.25, 5.0),
    "gpt-4o":               (5.0, 20.0),
    "gpt-4o-mini":          (0.15, 0.60),
    "_unknown":             (3.0, 15.0),   # default fallback
}

DAILY_ALERT_THRESHOLD_USD = float(os.getenv("COST_DAILY_ALERT_USD", "3.0"))
MONTHLY_ALERT_THRESHOLD_USD = float(os.getenv("COST_MONTHLY_ALERT_USD", "50.0"))


def _load_pricing() -> dict[str, tuple[float, float]]:
    """Allow env override: PRICING_USD_PER_MTOK='{"model": [in_usd, out_usd]}'."""
    override = os.getenv("PRICING_USD_PER_MTOK", "").strip()
    if not override:
        return DEFAULT_PRICING
    try:
        parsed = json.loads(override)
        out = dict(DEFAULT_PRICING)
        for model, prices in parsed.items():
            if isinstance(prices, (list, tuple)) and len(prices) == 2:
                out[model] = (float(prices[0]), float(prices[1]))
        return out
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        log.debug("PRICING_USD_PER_MTOK parse failed: %s", e)
        return DEFAULT_PRICING


def _resolve_pricing(model: str) -> tuple[float, float]:
    pricing = _load_pricing()
    if model in pricing:
        return pricing[model]
    # try prefix match
    for k, v in pricing.items():
        if k != "_unknown" and model.startswith(k):
            return v
    return pricing.get("_unknown", (3.0, 15.0))


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(COST_DB_PATH), timeout=2.0)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            model TEXT NOT NULL,
            tokens_in INTEGER NOT NULL,
            tokens_out INTEGER NOT NULL,
            cost_usd REAL NOT NULL,
            source TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_ts ON llm_calls(ts)")
    return conn


def estimate_cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    in_per_mtok, out_per_mtok = _resolve_pricing(model)
    return (tokens_in / 1_000_000.0) * in_per_mtok + (tokens_out / 1_000_000.0) * out_per_mtok


def log_llm_call(model: str, tokens_in: int, tokens_out: int,
                 source: str = "") -> float:
    """Persist one LLM call. Returns USD estimate. Never raises."""
    cost = estimate_cost_usd(model, tokens_in, tokens_out)
    try:
        with _conn() as conn:
            conn.execute(
                "INSERT INTO llm_calls (ts, model, tokens_in, tokens_out, cost_usd, source) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (int(time.time()), model, tokens_in, tokens_out, cost, source),
            )
    except sqlite3.Error as e:
        log.debug("cost_tracker insert failed: %s", e)
    return cost


def _aggregate(since_ts: int) -> dict[str, Any]:
    """Return per-model + total breakdown since timestamp."""
    out: dict[str, Any] = {"total_usd": 0.0, "total_calls": 0, "by_model": {}}
    try:
        with _conn() as conn:
            cur = conn.execute(
                "SELECT model, COUNT(*), SUM(tokens_in), SUM(tokens_out), SUM(cost_usd) "
                "FROM llm_calls WHERE ts >= ? GROUP BY model",
                (since_ts,),
            )
            for model, n, tin, tout, cost in cur:
                out["by_model"][model] = {
                    "calls": int(n or 0),
                    "tokens_in": int(tin or 0),
                    "tokens_out": int(tout or 0),
                    "cost_usd": float(cost or 0.0),
                }
                out["total_usd"] += float(cost or 0.0)
                out["total_calls"] += int(n or 0)
    except sqlite3.Error as e:
        log.debug("cost_tracker aggregate failed: %s", e)
    return out


def cost_last_24h() -> float:
    return _aggregate(int(time.time()) - 86400)["total_usd"]


def cost_month_to_date() -> float:
    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    return _aggregate(int(month_start.timestamp()))["total_usd"]


def check_alert_thresholds() -> str | None:
    """Return Telegram alert text if a threshold is breached, else None."""
    day_cost = cost_last_24h()
    month_cost = cost_month_to_date()
    msgs = []
    if day_cost > DAILY_ALERT_THRESHOLD_USD:
        msgs.append(f"💸 Cost 24h ${day_cost:.2f} > ${DAILY_ALERT_THRESHOLD_USD:.2f}")
    if month_cost > MONTHLY_ALERT_THRESHOLD_USD:
        msgs.append(f"💸 Cost MTD ${month_cost:.2f} > ${MONTHLY_ALERT_THRESHOLD_USD:.2f}")
    return "\n".join(msgs) if msgs else None


def format_cost_report(days: int = 7) -> str:
    """Render breakdown last N days for Telegram."""
    since = int(time.time()) - (days * 86400)
    agg = _aggregate(since)
    lines = [f"💰 *LLM cost — últimos {days}d*"]
    if agg["total_calls"] == 0:
        lines.append("  · sin llamadas registradas")
    else:
        lines.append(f"  · total: ${agg['total_usd']:.4f} en {agg['total_calls']} llamadas")
        by_model = sorted(agg["by_model"].items(),
                          key=lambda kv: -kv[1]["cost_usd"])
        for model, info in by_model:
            lines.append(
                f"  · `{model[:24]:24s}` "
                f"{info['calls']:4d} calls · "
                f"in={info['tokens_in']:>7d} out={info['tokens_out']:>7d} · "
                f"${info['cost_usd']:.4f}"
            )
    day_cost = cost_last_24h()
    month_cost = cost_month_to_date()
    lines.append("")
    lines.append(f"  24h: ${day_cost:.4f} (alert >${DAILY_ALERT_THRESHOLD_USD:.2f})")
    lines.append(f"  MTD: ${month_cost:.4f} (alert >${MONTHLY_ALERT_THRESHOLD_USD:.2f})")
    return "\n".join(lines)
