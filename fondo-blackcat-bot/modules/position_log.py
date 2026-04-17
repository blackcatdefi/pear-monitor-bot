"""Persistent position log — append-only event journal for the fund.

Covers:
  • Position opens / closes (with PnL)
  • Balance changes (transfers, withdrawals)
  • HyperLend debt rotations (USDH → UETH and similar)
  • Any other free-form note with a timestamp

Storage: sqlite3 at DATA_DIR/position_log.db. Single table, append-only.

Commands (wired from bot.py):
  /log                  → last 20 entries
  /log add <kind> <msg> → append entry
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from config import DATA_DIR

log = logging.getLogger(__name__)

DB_PATH = os.path.join(DATA_DIR, "position_log.db")

# Canonical kinds. Free-form strings are allowed, but these are suggested.
KIND_OPEN = "OPEN"
KIND_CLOSE = "CLOSE"
KIND_TRANSFER = "TRANSFER"
KIND_WITHDRAW = "WITHDRAW"
KIND_DEBT_ROTATION = "DEBT_ROTATION"
KIND_NOTE = "NOTE"


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute(
        """CREATE TABLE IF NOT EXISTS position_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            kind TEXT NOT NULL,
            asset TEXT,
            amount_usd REAL,
            wallet_label TEXT,
            message TEXT NOT NULL
        )"""
    )
    c.execute("CREATE INDEX IF NOT EXISTS idx_log_ts ON position_log(ts)")
    return c


def append(
    kind: str,
    message: str,
    asset: str | None = None,
    amount_usd: float | None = None,
    wallet_label: str | None = None,
    ts: datetime | None = None,
) -> int:
    when = (ts or datetime.now(timezone.utc)).isoformat()
    c = _conn()
    try:
        cur = c.execute(
            "INSERT INTO position_log(ts,kind,asset,amount_usd,wallet_label,message) "
            "VALUES(?,?,?,?,?,?)",
            (when, kind.upper(), asset, amount_usd, wallet_label, message),
        )
        c.commit()
        return int(cur.lastrowid or 0)
    finally:
        c.close()


def last_n(n: int = 20) -> list[dict[str, Any]]:
    c = _conn()
    try:
        rows = c.execute(
            "SELECT id,ts,kind,asset,amount_usd,wallet_label,message "
            "FROM position_log ORDER BY id DESC LIMIT ?",
            (n,),
        ).fetchall()
        return [
            {
                "id": r[0],
                "ts": r[1],
                "kind": r[2],
                "asset": r[3],
                "amount_usd": r[4],
                "wallet_label": r[5],
                "message": r[6],
            }
            for r in rows
        ]
    finally:
        c.close()


def _fmt_ts(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return ts[:16]


def _fmt_amount(v: float | None) -> str:
    if v is None:
        return ""
    sign = "-" if v < 0 else "+"
    av = abs(v)
    if av >= 1_000_000:
        return f" {sign}${av/1_000_000:.2f}M"
    if av >= 1_000:
        return f" {sign}${av/1_000:.1f}K"
    return f" {sign}${av:.2f}"


def format_log(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return (
            "📜 POSITION LOG\n"
            + ("─" * 40)
            + "\n— vacío —\n"
            + "Agregá entradas con:  /log add <kind> <mensaje>\n"
            + "Kinds sugeridos: OPEN / CLOSE / TRANSFER / WITHDRAW / DEBT_ROTATION / NOTE"
        )
    lines: list[str] = []
    lines.append("📜 POSITION LOG — últimos {}".format(len(entries)))
    lines.append("─" * 40)
    for e in entries:
        amount_s = _fmt_amount(e.get("amount_usd"))
        asset_s = f" {e['asset']}" if e.get("asset") else ""
        wallet_s = f" [{e['wallet_label']}]" if e.get("wallet_label") else ""
        lines.append(
            f"• {_fmt_ts(e['ts'])} {e['kind']}{asset_s}{amount_s}{wallet_s}\n"
            f"    {e['message']}"
        )
    return "\n".join(lines)


def parse_manual_add(args: list[str]) -> dict[str, Any]:
    """Parse `/log add <kind> <message...>`.

    Keeps parsing simple — more structured fields come via record_* helpers.
    """
    if len(args) < 2:
        raise ValueError("Usage: /log add <kind> <message...>")
    kind = args[0].upper()
    message = " ".join(args[1:])
    return {"kind": kind, "message": message}


# ─── Convenience helpers for other modules ──────────────────────────────
def record_debt_rotation(from_asset: str, to_asset: str, wallet_label: str) -> int:
    return append(
        KIND_DEBT_ROTATION,
        f"Debt rotation: {from_asset} → {to_asset}",
        asset=to_asset,
        wallet_label=wallet_label,
    )


def record_position_event(
    side_kind: str,  # OPEN or CLOSE
    asset: str,
    amount_usd: float,
    message: str,
    wallet_label: str | None = None,
) -> int:
    return append(
        side_kind,
        message,
        asset=asset,
        amount_usd=amount_usd,
        wallet_label=wallet_label,
    )
