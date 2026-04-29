"""Round 18 — Extended /pnl with multi-period filters + breakdown.

Reuses the existing pnl_events SQLite table (modules.pnl_tracker.DB_PATH)
but adds:
    - period filtering: today / week / month / ytd / all
    - group_by_wallet, group_by_asset
    - best/worst trade
    - win-rate computation

Surfaced as /pnl <period> when args are passed (defaults remain the legacy
3-period summary).
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from config import DATA_DIR

DB_PATH = os.path.join(DATA_DIR, "pnl.db")

VALID_PERIODS = ("today", "week", "month", "ytd", "all")


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return sqlite3.connect(DB_PATH)


def is_enabled() -> bool:
    return os.getenv("PNL_TRACKER_ENABLED", "true").strip().lower() != "false"


def _cutoff(period: str) -> datetime | None:
    now = datetime.now(timezone.utc)
    p = (period or "").lower()
    if p == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if p == "week":
        return now - timedelta(days=7)
    if p == "month":
        return now - timedelta(days=30)
    if p == "ytd":
        return datetime(now.year, 1, 1, tzinfo=timezone.utc)
    if p == "all":
        return None
    raise ValueError(f"period must be one of {VALID_PERIODS}; got {period!r}")


def _query_closes(since: datetime | None) -> list[dict[str, Any]]:
    """Only CLOSED events (the realised PnL ones). Transfers/withdraws excluded."""
    c = _conn()
    try:
        if since is not None:
            rows = c.execute(
                "SELECT id,ts,asset,amount_usd,wallet_label,notes FROM pnl_events "
                "WHERE category='CLOSED' AND ts >= ? ORDER BY ts DESC",
                (since.isoformat(),),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT id,ts,asset,amount_usd,wallet_label,notes FROM pnl_events "
                "WHERE category='CLOSED' ORDER BY ts DESC"
            ).fetchall()
    finally:
        c.close()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": r[0],
                "ts": r[1],
                "asset": r[2] or "?",
                "pnl_usd": float(r[3] or 0),
                "wallet_label": r[4] or "?",
                "notes": r[5] or "",
            }
        )
    return out


def _fmt_usd(v: float) -> str:
    sign = "-" if v < 0 else "+"
    av = abs(v)
    if av >= 1_000_000:
        return f"{sign}${av/1_000_000:.2f}M"
    if av >= 1_000:
        return f"{sign}${av/1_000:.1f}K"
    return f"{sign}${av:.2f}"


def _group_by(closes: list[dict[str, Any]], key: str) -> str:
    if not closes:
        return "  (sin eventos)"
    buckets: dict[str, list[dict[str, Any]]] = {}
    for e in closes:
        k = e.get(key) or "?"
        buckets.setdefault(k, []).append(e)
    rows: list[str] = []
    for k in sorted(buckets, key=lambda kk: -sum(x["pnl_usd"] for x in buckets[kk])):
        items = buckets[k]
        total = sum(x["pnl_usd"] for x in items)
        wins = sum(1 for x in items if x["pnl_usd"] > 0)
        rows.append(f"  {k}: {_fmt_usd(total)} ({wins}/{len(items)} W)")
    return "\n".join(rows)


def build_period_summary(period: str) -> str:
    if not is_enabled():
        return "\u26a0\ufe0f /pnl tracker DISABLED (PNL_TRACKER_ENABLED=false)"
    since = _cutoff(period)
    closes = _query_closes(since)

    total = sum(c["pnl_usd"] for c in closes)
    wins = [c for c in closes if c["pnl_usd"] > 0]
    losses = [c for c in closes if c["pnl_usd"] < 0]
    win_rate = (len(wins) / len(closes) * 100) if closes else 0.0

    best = max(closes, key=lambda c: c["pnl_usd"]) if closes else None
    worst = min(closes, key=lambda c: c["pnl_usd"]) if closes else None

    lines: list[str] = [
        f"\U0001f4b0 PnL REALIZADO \u2014 {period.upper()}",
        "\u2500" * 30,
        f"Total PnL: {_fmt_usd(total)}",
        f"Trades: {len(closes)} ({len(wins)}W / {len(losses)}L)",
        f"Win rate: {win_rate:.1f}%",
    ]

    if best:
        lines.append(
            f"Best:  {best['asset']} {_fmt_usd(best['pnl_usd'])} "
            f"({best['ts'][:16]})"
        )
    if worst:
        lines.append(
            f"Worst: {worst['asset']} {_fmt_usd(worst['pnl_usd'])} "
            f"({worst['ts'][:16]})"
        )

    if closes:
        lines.append("")
        lines.append("Por wallet:")
        lines.append(_group_by(closes, "wallet_label"))
        lines.append("")
        lines.append("Por asset:")
        lines.append(_group_by(closes, "asset"))

    if not closes:
        lines.append("")
        lines.append(
            "Sin eventos en este período. Los cierres se registran auto cuando "
            "alerts.py detecta closes; manual: /pnl add closed <asset> <amount>."
        )

    return "\n".join(lines)


def is_valid_period(token: str) -> bool:
    return (token or "").lower() in VALID_PERIODS
