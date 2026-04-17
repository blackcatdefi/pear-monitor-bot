"""PnL tracker — realized PnL, transfers, withdrawals (SQLite-backed).

Three event categories:
  • CLOSED    — realized PnL from a closed position (positive or negative $)
  • TRANSFER  — movement between own wallets; neutral for PnL accounting
  • WITHDRAW  — capital removed from the fund; NOT counted as PnL

Commands:
  /pnl             → 7D / 30D / YTD summaries
  /pnl add ...     → record a new event (one-off manual entry; future work)

Storage: sqlite3 at DATA_DIR/pnl.db, single table `pnl_events`.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from config import DATA_DIR

log = logging.getLogger(__name__)

DB_PATH = os.path.join(DATA_DIR, "pnl.db")

EVENT_CLOSED = "CLOSED"
EVENT_TRANSFER = "TRANSFER"
EVENT_WITHDRAW = "WITHDRAW"
VALID_EVENTS = (EVENT_CLOSED, EVENT_TRANSFER, EVENT_WITHDRAW)


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute(
        """CREATE TABLE IF NOT EXISTS pnl_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            category TEXT NOT NULL,
            asset TEXT,
            amount_usd REAL NOT NULL,
            wallet_label TEXT,
            notes TEXT
        )"""
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_pnl_ts ON pnl_events(ts)")
    return c


def record_event(
    category: str,
    amount_usd: float,
    asset: str | None = None,
    wallet_label: str | None = None,
    notes: str | None = None,
    ts: datetime | None = None,
) -> int:
    if category not in VALID_EVENTS:
        raise ValueError(f"Invalid category {category}; must be one of {VALID_EVENTS}")
    when = (ts or datetime.now(timezone.utc)).isoformat()
    c = _conn()
    try:
        cur = c.execute(
            "INSERT INTO pnl_events(ts,category,asset,amount_usd,wallet_label,notes) "
            "VALUES(?,?,?,?,?,?)",
            (when, category, asset, amount_usd, wallet_label, notes),
        )
        c.commit()
        return int(cur.lastrowid or 0)
    finally:
        c.close()


def _sum_by_category(since: datetime | None) -> dict[str, float]:
    c = _conn()
    try:
        if since:
            rows = c.execute(
                "SELECT category, SUM(amount_usd) FROM pnl_events "
                "WHERE ts >= ? GROUP BY category",
                (since.isoformat(),),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT category, SUM(amount_usd) FROM pnl_events GROUP BY category"
            ).fetchall()
        return {cat: float(total or 0.0) for cat, total in rows}
    finally:
        c.close()


def _fmt_usd(v: float) -> str:
    sign = "-" if v < 0 else "+"
    av = abs(v)
    if av >= 1_000_000:
        return f"{sign}${av/1_000_000:.2f}M"
    if av >= 1_000:
        return f"{sign}${av/1_000:.1f}K"
    return f"{sign}${av:.2f}"


def build_summary() -> str:
    now = datetime.now(timezone.utc)
    ytd_start = datetime(now.year, 1, 1, tzinfo=timezone.utc)
    d7 = now - timedelta(days=7)
    d30 = now - timedelta(days=30)

    totals_7d = _sum_by_category(d7)
    totals_30d = _sum_by_category(d30)
    totals_ytd = _sum_by_category(ytd_start)
    totals_all = _sum_by_category(None)

    def _line(label: str, totals: dict[str, float]) -> list[str]:
        closed = totals.get(EVENT_CLOSED, 0.0)
        transfer = totals.get(EVENT_TRANSFER, 0.0)
        withdraw = totals.get(EVENT_WITHDRAW, 0.0)
        return [
            f"  {label}",
            f"    Realized PnL:  {_fmt_usd(closed)}",
            f"    Transfers:     {_fmt_usd(transfer)}   (neutral — entre wallets)",
            f"    Withdrawals:   {_fmt_usd(withdraw)}   (capital out, no PnL)",
        ]

    lines: list[str] = []
    lines.append("💰 PNL TRACKER")
    lines.append("─" * 40)
    lines.extend(_line("Últimos 7d", totals_7d))
    lines.append("")
    lines.extend(_line("Últimos 30d", totals_30d))
    lines.append("")
    lines.extend(_line(f"YTD {now.year}", totals_ytd))
    lines.append("")
    lines.extend(_line("Histórico total", totals_all))

    # Event count for context
    c = _conn()
    try:
        total_events = c.execute("SELECT COUNT(*) FROM pnl_events").fetchone()[0]
    finally:
        c.close()
    lines.append("")
    lines.append(f"Total events registrados: {total_events}")
    if total_events == 0:
        lines.append(
            "Tip: registrá eventos con `/pnl add closed <asset> <amount> [notes]` "
            "(o las variantes transfer / withdraw)."
        )
    return "\n".join(lines)


def parse_manual_add(args: list[str]) -> dict[str, Any]:
    """Parse `/pnl add <category> <asset> <amount> [notes...]`.

    Returns dict suitable for record_event(), or raises ValueError.
    """
    if len(args) < 3:
        raise ValueError(
            "Usage: /pnl add <closed|transfer|withdraw> <asset> <amount_usd> [notes]"
        )
    cat_raw = args[0].upper()
    if cat_raw not in VALID_EVENTS:
        raise ValueError(f"Category must be one of {VALID_EVENTS} (got {cat_raw})")
    asset = args[1]
    try:
        amount = float(args[2])
    except ValueError as exc:
        raise ValueError(f"amount_usd must be a number (got {args[2]!r})") from exc
    notes = " ".join(args[3:]) if len(args) > 3 else None
    return {
        "category": cat_raw,
        "asset": asset,
        "amount_usd": amount,
        "notes": notes,
    }
