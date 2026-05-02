"""Round 18 — Compounding detector.

Tracks per-wallet snapshots (notional + positions set + account_value) and
flags COMPOUND events when the same set of positions keeps the same
composition but notional + equity grow simultaneously.

A compound is when BCD reabres una basket aumentando size con fondos
disponibles desde Pear — el bot principal no detectaba esto.

Heuristic:
    positions_set unchanged  AND  notional_growth >= +10%  AND  equity_growth >= +5%

Storage: sqlite3 at DATA_DIR/compounding.db
    table wallet_snapshots → snapshots history (rolling N=200)
    table compounding_events → detected events log
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from config import DATA_DIR, FUND_WALLETS

log = logging.getLogger(__name__)

DB_PATH = os.path.join(DATA_DIR, "compounding.db")

NOTIONAL_GROWTH_THRESHOLD = 0.10  # +10%
EQUITY_GROWTH_THRESHOLD = 0.05    # +5%


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute(
        """CREATE TABLE IF NOT EXISTS wallet_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            wallet TEXT NOT NULL,
            total_notional REAL NOT NULL,
            account_value REAL NOT NULL,
            positions_json TEXT NOT NULL
        )"""
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_snap_wallet_ts "
        "ON wallet_snapshots(wallet, ts_utc DESC)"
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS compounding_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            wallet TEXT NOT NULL,
            prev_notional REAL NOT NULL,
            curr_notional REAL NOT NULL,
            prev_equity REAL NOT NULL,
            curr_equity REAL NOT NULL,
            positions_count INTEGER NOT NULL,
            notional_growth_pct REAL NOT NULL,
            equity_growth_pct REAL NOT NULL,
            notes TEXT
        )"""
    )
    return c


@dataclass
class WalletSnapshot:
    wallet: str
    timestamp: datetime
    total_notional: float
    account_value: float
    positions_set: frozenset  # tuples (asset, side)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_positions_set(state: dict[str, Any]) -> frozenset:
    """Extract (asset, side) tuples from a clearinghouseState-like dict."""
    out: list[tuple[str, str]] = []
    for ap in state.get("assetPositions", []) or []:
        p = ap.get("position", {}) or {}
        try:
            sz = float(p.get("szi", 0) or 0)
        except (TypeError, ValueError):
            sz = 0.0
        if sz == 0:
            continue
        coin = p.get("coin") or "?"
        side = "LONG" if sz > 0 else "SHORT"
        out.append((coin, side))
    return frozenset(out)


def _extract_total_notional(state: dict[str, Any]) -> float:
    total = 0.0
    for ap in state.get("assetPositions", []) or []:
        p = ap.get("position", {}) or {}
        try:
            total += abs(float(p.get("positionValue", 0) or 0))
        except (TypeError, ValueError):
            continue
    return total


def _extract_account_value(state: dict[str, Any]) -> float:
    margin = state.get("marginSummary") or {}
    try:
        return float(margin.get("accountValue", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _save_snapshot(snap: WalletSnapshot) -> None:
    positions_list = sorted([f"{a}:{s}" for (a, s) in snap.positions_set])
    with _conn() as c:
        c.execute(
            "INSERT INTO wallet_snapshots(ts_utc,wallet,total_notional,account_value,positions_json) "
            "VALUES (?,?,?,?,?)",
            (
                snap.timestamp.isoformat(),
                snap.wallet.lower(),
                snap.total_notional,
                snap.account_value,
                json.dumps(positions_list),
            ),
        )
        # keep only last 200 per wallet
        c.execute(
            "DELETE FROM wallet_snapshots WHERE wallet=? AND id NOT IN "
            "(SELECT id FROM wallet_snapshots WHERE wallet=? ORDER BY id DESC LIMIT 200)",
            (snap.wallet.lower(), snap.wallet.lower()),
        )


def _load_last_snapshot(wallet: str) -> WalletSnapshot | None:
    with _conn() as c:
        row = c.execute(
            "SELECT ts_utc,total_notional,account_value,positions_json "
            "FROM wallet_snapshots WHERE wallet=? ORDER BY id DESC LIMIT 1",
            (wallet.lower(),),
        ).fetchone()
    if not row:
        return None
    ts_str, notional, equity, positions_json = row
    try:
        positions = frozenset(
            tuple(p.split(":", 1)) for p in (json.loads(positions_json) or [])
        )
    except Exception:
        positions = frozenset()
    return WalletSnapshot(
        wallet=wallet.lower(),
        timestamp=datetime.fromisoformat(ts_str),
        total_notional=float(notional),
        account_value=float(equity),
        positions_set=positions,
    )


def _record_event(
    wallet: str,
    prev: WalletSnapshot,
    curr: WalletSnapshot,
    notional_growth: float,
    equity_growth: float,
) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO compounding_events("
            "ts_utc,wallet,prev_notional,curr_notional,prev_equity,curr_equity,"
            "positions_count,notional_growth_pct,equity_growth_pct,notes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                curr.timestamp.isoformat(),
                wallet.lower(),
                prev.total_notional,
                curr.total_notional,
                prev.account_value,
                curr.account_value,
                len(curr.positions_set),
                notional_growth * 100,
                equity_growth * 100,
                None,
            ),
        )
        return int(cur.lastrowid or 0)


async def check_compounding(application=None) -> list[dict[str, Any]]:
    """Run a sweep across all FUND_WALLETS, return list of detected events.

    Lightweight: imports portfolio at call-time to avoid circular imports.
    Each event is a dict ready to format into a Telegram alert.
    """
    if os.getenv("COMPOUNDING_DETECTOR_ENABLED", "true").strip().lower() == "false":
        return []

    from modules.portfolio import clearinghouse_state  # local to break cycle

    detected: list[dict[str, Any]] = []
    for wallet in list(FUND_WALLETS.keys()):
        try:
            state = await clearinghouse_state(wallet)
        except Exception:
            log.exception("compounding: failed to read state for %s", wallet)
            continue

        positions_set = _extract_positions_set(state)
        notional = _extract_total_notional(state)
        equity = _extract_account_value(state)
        curr = WalletSnapshot(
            wallet=wallet,
            timestamp=datetime.now(timezone.utc),
            total_notional=notional,
            account_value=equity,
            positions_set=positions_set,
        )

        prev = _load_last_snapshot(wallet)
        if not prev or prev.total_notional <= 0 or prev.account_value <= 0:
            _save_snapshot(curr)
            continue

        positions_unchanged = (curr.positions_set == prev.positions_set) and bool(curr.positions_set)
        if not positions_unchanged:
            _save_snapshot(curr)
            continue

        notional_growth = (curr.total_notional - prev.total_notional) / prev.total_notional
        equity_growth = (curr.account_value - prev.account_value) / prev.account_value

        if (
            notional_growth >= NOTIONAL_GROWTH_THRESHOLD
            and equity_growth >= EQUITY_GROWTH_THRESHOLD
        ):
            event_id = _record_event(wallet, prev, curr, notional_growth, equity_growth)
            detected.append(
                {
                    "id": event_id,
                    "wallet": wallet,
                    "prev_notional": prev.total_notional,
                    "curr_notional": curr.total_notional,
                    "prev_equity": prev.account_value,
                    "curr_equity": curr.account_value,
                    "positions_count": len(curr.positions_set),
                    "leverage_now": (curr.total_notional / curr.account_value) if curr.account_value else 0.0,
                    "notional_growth_pct": notional_growth * 100,
                    "equity_growth_pct": equity_growth * 100,
                    "ts_utc": curr.timestamp.isoformat(),
                }
            )

        _save_snapshot(curr)
    return detected


def format_event(event: dict[str, Any]) -> str:
    w = event["wallet"]
    short = f"{w[:6]}…{w[-4:]}"
    return (
        "\U0001f504 COMPOUNDING DETECTADO — " + short + "\n"
        f"Notional: ${event['prev_notional']:,.0f} → "
        f"${event['curr_notional']:,.0f} ({event['notional_growth_pct']:+.1f}%)\n"
        f"Equity:   ${event['prev_equity']:,.0f} → "
        f"${event['curr_equity']:,.0f} ({event['equity_growth_pct']:+.1f}%)\n"
        f"Positions: {event['positions_count']} unchanged\n"
        f"Current effective leverage: {event['leverage_now']:.2f}x\n"
        "BCD added capital to the active basket."
    )


def history(limit: int = 20) -> list[dict[str, Any]]:
    """Return the last N compounding events."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id,ts_utc,wallet,prev_notional,curr_notional,prev_equity,curr_equity,"
            "positions_count,notional_growth_pct,equity_growth_pct "
            "FROM compounding_events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": r[0],
                "ts_utc": r[1],
                "wallet": r[2],
                "prev_notional": float(r[3]),
                "curr_notional": float(r[4]),
                "prev_equity": float(r[5]),
                "curr_equity": float(r[6]),
                "positions_count": int(r[7]),
                "notional_growth_pct": float(r[8]),
                "equity_growth_pct": float(r[9]),
            }
        )
    return out


def format_history(limit: int = 20) -> str:
    events = history(limit)
    if not events:
        return (
            "\U0001f504 COMPOUNDING HISTORY\n"
            + ("\u2500" * 30) + "\n"
            "No events recorded yet.\n"
            "Detector runs every 30 min via scheduler.\n"
            "Toggle: COMPOUNDING_DETECTOR_ENABLED env var."
        )
    lines: list[str] = [
        "\U0001f504 COMPOUNDING HISTORY (last {})".format(min(limit, len(events))),
        "\u2500" * 30,
    ]
    for e in events:
        w = e["wallet"]
        short = f"{w[:6]}…{w[-4:]}"
        lines.append(
            f"#{e['id']} · {e['ts_utc'][:16]} · {short}\n"
            f"  Notional ${e['prev_notional']:,.0f} → ${e['curr_notional']:,.0f} "
            f"({e['notional_growth_pct']:+.1f}%)\n"
            f"  Equity ${e['prev_equity']:,.0f} → ${e['curr_equity']:,.0f} "
            f"({e['equity_growth_pct']:+.1f}%) · pos={e['positions_count']}"
        )
    return "\n".join(lines)
