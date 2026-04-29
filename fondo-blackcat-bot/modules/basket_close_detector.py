"""Round 18 — Basket close detector + summary.

Track close events arriving from alerts.py / portfolio diff. If we see ≥3
closes for the same wallet within 5 minutes, treat it as a basket close and
emit a single consolidated summary instead of N individual alerts.

The detector is intentionally idempotent and stateful in a single SQLite
table so that across restarts we don't replay events.

Usage from alerts.py:
    from modules.basket_close_detector import track_close, maybe_emit_summary
    await track_close({"wallet": w, "asset": a, "pnl": pnl, "entry_value": ev, "exit_value": xv})
    await maybe_emit_summary(application.bot, chat_id)
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from config import DATA_DIR

log = logging.getLogger(__name__)

DB_PATH = os.path.join(DATA_DIR, "basket_close.db")

WINDOW_MINUTES = 5
MIN_CLOSES = 3
QUIET_PERIOD_SECONDS = 30  # wait for stragglers before summarising


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute(
        """CREATE TABLE IF NOT EXISTS recent_closes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            wallet TEXT NOT NULL,
            asset TEXT NOT NULL,
            side TEXT,
            pnl REAL DEFAULT 0,
            entry_value REAL DEFAULT 0,
            exit_value REAL DEFAULT 0,
            consumed INTEGER DEFAULT 0
        )"""
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_close_wallet_ts "
        "ON recent_closes(wallet, ts_utc DESC)"
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS basket_close_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            wallet TEXT NOT NULL,
            close_count INTEGER NOT NULL,
            total_pnl REAL NOT NULL,
            total_entry_value REAL NOT NULL,
            total_exit_value REAL NOT NULL,
            assets_csv TEXT NOT NULL,
            summary_text TEXT
        )"""
    )
    return c


async def track_close(event: dict[str, Any]) -> None:
    """Persist a single close event. Caller should provide:
    wallet (required), asset (required), pnl (float), entry_value, exit_value, side.
    """
    if not event.get("wallet") or not event.get("asset"):
        return
    if os.getenv("BASKET_CLOSE_DETECTOR_ENABLED", "true").strip().lower() == "false":
        return
    ts = event.get("ts_utc") or datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        c.execute(
            "INSERT INTO recent_closes(ts_utc,wallet,asset,side,pnl,entry_value,exit_value) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                ts,
                str(event["wallet"]).lower(),
                str(event["asset"]).upper(),
                event.get("side") or "?",
                float(event.get("pnl") or 0),
                float(event.get("entry_value") or 0),
                float(event.get("exit_value") or 0),
            ),
        )


def _purge_old(c: sqlite3.Connection) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    c.execute(
        "DELETE FROM recent_closes WHERE consumed=1 AND ts_utc < ?",
        (cutoff,),
    )


def _query_recent_unconsumed(c: sqlite3.Connection, wallet: str) -> list[sqlite3.Row]:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=WINDOW_MINUTES)).isoformat()
    rows = c.execute(
        "SELECT id,ts_utc,wallet,asset,side,pnl,entry_value,exit_value "
        "FROM recent_closes WHERE wallet=? AND consumed=0 AND ts_utc>=?",
        (wallet.lower(), cutoff),
    ).fetchall()
    return list(rows)


def _mark_consumed(c: sqlite3.Connection, ids: list[int]) -> None:
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    c.execute(
        f"UPDATE recent_closes SET consumed=1 WHERE id IN ({placeholders})",
        tuple(ids),
    )


def _format_summary(wallet: str, closes: list[dict[str, Any]]) -> str:
    total_pnl = sum(c["pnl"] for c in closes)
    total_entry = sum(c.get("entry_value", 0) for c in closes)
    total_exit = sum(c.get("exit_value", 0) for c in closes)
    pct = (total_pnl / total_entry * 100) if total_entry else 0.0
    short = f"{wallet[:6]}\u2026{wallet[-4:]}"
    assets = ", ".join(c["asset"] for c in closes)
    lines: list[str] = []
    lines.append("\U0001f408\u200d\u2b1b BASKET CLOSED \u2014 " + short)
    lines.append("\U0001f4ca RESUMEN:")
    lines.append(f"  \u2022 Posiciones cerradas: {len(closes)}")
    lines.append(f"  \u2022 Assets: {assets}")
    lines.append(f"  \u2022 PnL total: ${total_pnl:+,.2f} ({pct:+.2f}%)")
    lines.append(f"  \u2022 Entry value: ${total_entry:,.2f}")
    lines.append(f"  \u2022 Exit value: ${total_exit:,.2f}")
    lines.append("")
    lines.append("\U0001f4cb BREAKDOWN (best to worst):")
    for c in sorted(closes, key=lambda x: x["pnl"], reverse=True):
        emoji = "\U0001f7e2" if c["pnl"] >= 0 else "\U0001f534"
        lines.append(f"  {emoji} {c['asset']}: ${c['pnl']:+,.2f}")
    lines.append("")
    lines.append(f"\U0001f4b0 Capital ahora libre en {short}: ${total_exit:,.2f}")
    lines.append("\U0001f3af Listo para próxima basket o compound.")
    return "\n".join(lines)


def _store_summary(
    wallet: str,
    closes: list[dict[str, Any]],
    summary_text: str,
) -> int:
    total_pnl = sum(c["pnl"] for c in closes)
    total_entry = sum(c.get("entry_value", 0) for c in closes)
    total_exit = sum(c.get("exit_value", 0) for c in closes)
    assets_csv = ",".join(c["asset"] for c in closes)
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO basket_close_summaries("
            "ts_utc,wallet,close_count,total_pnl,total_entry_value,total_exit_value,"
            "assets_csv,summary_text) VALUES (?,?,?,?,?,?,?,?)",
            (
                datetime.now(timezone.utc).isoformat(),
                wallet.lower(),
                len(closes),
                total_pnl,
                total_entry,
                total_exit,
                assets_csv,
                summary_text,
            ),
        )
        return int(cur.lastrowid or 0)


async def maybe_emit_summary(
    bot,
    chat_id: str | int,
    *,
    wallet: str | None = None,
) -> dict[str, Any] | None:
    """If a wallet has accumulated >=MIN_CLOSES recent closes, wait the
    quiet period then emit a single summary message and mark them consumed.

    Returns the dict that was sent (for tests) or None if not triggered.
    """
    if os.getenv("BASKET_CLOSE_DETECTOR_ENABLED", "true").strip().lower() == "false":
        return None

    with _conn() as c:
        _purge_old(c)
        # Build candidate wallets list
        if wallet:
            wallets_to_check = [wallet.lower()]
        else:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(minutes=WINDOW_MINUTES)
            ).isoformat()
            wallets_to_check = [
                r[0]
                for r in c.execute(
                    "SELECT DISTINCT wallet FROM recent_closes "
                    "WHERE consumed=0 AND ts_utc>=?",
                    (cutoff,),
                ).fetchall()
            ]

    for w in wallets_to_check:
        with _conn() as c:
            rows = _query_recent_unconsumed(c, w)
        if len(rows) < MIN_CLOSES:
            continue

        # Quiet period — wait for stragglers
        await asyncio.sleep(QUIET_PERIOD_SECONDS)

        with _conn() as c:
            rows = _query_recent_unconsumed(c, w)
        if len(rows) < MIN_CLOSES:
            continue

        closes = [
            {
                "id": r[0],
                "ts_utc": r[1],
                "wallet": r[2],
                "asset": r[3],
                "side": r[4],
                "pnl": float(r[5] or 0),
                "entry_value": float(r[6] or 0),
                "exit_value": float(r[7] or 0),
            }
            for r in rows
        ]
        summary = _format_summary(w, closes)
        try:
            from utils.telegram import send_bot_message
            await send_bot_message(bot, chat_id, summary)
        except Exception:
            log.exception("basket_close_detector: failed to send summary")
            continue

        _store_summary(w, closes, summary)
        with _conn() as c:
            _mark_consumed(c, [int(c_["id"]) for c_ in closes])

        return {"wallet": w, "summary": summary, "count": len(closes)}
    return None


def history(limit: int = 10) -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id,ts_utc,wallet,close_count,total_pnl,total_entry_value,"
            "total_exit_value,assets_csv FROM basket_close_summaries "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": r[0],
                "ts_utc": r[1],
                "wallet": r[2],
                "close_count": int(r[3]),
                "total_pnl": float(r[4]),
                "total_entry_value": float(r[5]),
                "total_exit_value": float(r[6]),
                "assets": (r[7] or "").split(","),
            }
        )
    return out
