"""R-BOT-DEFINITIVE WI-6 — trailing-rule monitor (suggestion-only, never executes).

Fund rule: from +10% favorable price move on an individual BASKET leg,
a 10% trailing stop applies. BCD moves the stops manually on HL — the bot
ONLY suggests, it NEVER places/modifies/cancels orders.

Mechanics:
  * Basket legs = SHORT perp legs on a HIP-3 builder dex (same definition as
    the WI-2 header split).
  * favorable % = (entry − mark)/entry × 100 for a SHORT
                  (mark − entry)/entry × 100 for a LONG.
  * Thresholds: +10% then every further +5% step (15, 20, 25, …).
  * ONE alert per (leg, threshold), persisted in SQLite — re-crossing the same
    threshold never re-fires; the next step does.
  * Suggested SL = mark × 1.10 (SHORT) / mark × 0.90 (LONG) — i.e. trailing
    10% from the CURRENT mark. Locked PnL = (entry − suggested_SL) × |size|
    for a SHORT (mirror for a LONG).
NEVER raises from public functions.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any

log = logging.getLogger(__name__)

try:
    from config import DATA_DIR
except Exception:  # noqa: BLE001
    DATA_DIR = os.getenv("DATA_DIR", "/tmp")

DB_PATH = os.path.join(DATA_DIR, "trailing_monitor.db")

START_PCT = float(os.getenv("TRAILING_START_PCT", "10") or 10)
STEP_PCT = float(os.getenv("TRAILING_STEP_PCT", "5") or 5)
TRAIL_PCT = float(os.getenv("TRAILING_TRAIL_PCT", "10") or 10)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trailing_state (
            coin TEXT NOT NULL,
            threshold_pct REAL NOT NULL,
            fired_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (coin, threshold_pct)
        )
        """
    )
    return conn


def favorable_move_pct(side: str, entry_px: float, mark_px: float) -> float | None:
    """Favorable % move from entry. None when inputs invalid."""
    try:
        e = float(entry_px)
        m = float(mark_px)
        if e <= 0 or m <= 0:
            return None
        if (side or "").upper() == "SHORT":
            return (e - m) / e * 100.0
        return (m - e) / e * 100.0
    except (TypeError, ValueError):
        return None


def crossed_thresholds(move_pct: float) -> list[float]:
    """All thresholds (10, 15, 20, …) at or below the current move."""
    out: list[float] = []
    if move_pct is None or move_pct < START_PCT:
        return out
    t = START_PCT
    while t <= move_pct + 1e-9:
        out.append(round(t, 4))
        t += STEP_PCT
    return out


def _already_fired(coin: str, threshold: float) -> bool:
    try:
        conn = _conn()
        try:
            cur = conn.execute(
                "SELECT 1 FROM trailing_state WHERE coin=? AND threshold_pct=?",
                ((coin or "?").upper(), float(threshold)),
            )
            return cur.fetchone() is not None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return True  # fail-safe: never spam on storage errors


def _mark_fired(coin: str, threshold: float) -> None:
    try:
        conn = _conn()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO trailing_state (coin, threshold_pct) VALUES (?, ?)",
                ((coin or "?").upper(), float(threshold)),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def suggested_sl(side: str, mark_px: float) -> float:
    """Trailing SL anchored to the CURRENT mark (10% beyond)."""
    if (side or "").upper() == "SHORT":
        return mark_px * (1.0 + TRAIL_PCT / 100.0)
    return mark_px * (1.0 - TRAIL_PCT / 100.0)


def locked_pnl(side: str, entry_px: float, sl_px: float, size_abs: float) -> float:
    """PnL locked if the suggested SL fills."""
    if (side or "").upper() == "SHORT":
        return (entry_px - sl_px) * size_abs
    return (sl_px - entry_px) * size_abs


def build_suggestion(
    coin: str,
    side: str,
    entry_px: float,
    mark_px: float,
    size_abs: float,
    move_pct: float,
) -> str:
    sl = suggested_sl(side, mark_px)
    lock = locked_pnl(side, entry_px, sl, size_abs)
    return (
        f"📐 TRAILING RULE: {side} {coin} +{move_pct:.1f}% desde entry "
        f"${entry_px:,.2f} (mark ${mark_px:,.2f}).\n"
        f"SL sugerido ${sl:,.2f} (trailing {TRAIL_PCT:.0f}% del mark) — "
        f"lockea ${lock:,.2f} de PnL.\n"
        "BCD mueve el stop manualmente en HL — el bot NUNCA ejecuta."
    )


def _basket_legs(wallets: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """SHORT legs on HIP-3 builder dexes (same definition as the WI-2 split)."""
    legs: list[dict[str, Any]] = []
    for w in wallets or []:
        if not isinstance(w, dict) or w.get("status") != "ok":
            continue
        d = w.get("data") or {}
        for p in d.get("positions") or []:
            try:
                sz = float(p.get("size") or p.get("szi") or 0.0)
            except (TypeError, ValueError):
                sz = 0.0
            side = str(p.get("side") or ("LONG" if sz > 0 else "SHORT")).upper()
            dex = str(p.get("dex") or "main").lower()
            if side == "SHORT" and dex not in ("", "main"):
                legs.append(p)
    return legs


def evaluate_leg(p: dict[str, Any]) -> tuple[bool, str]:
    """Evaluate ONE basket leg. Returns (fire, message). NEVER raises."""
    try:
        coin = str(p.get("coin") or "?")
        try:
            sz = abs(float(p.get("size") or p.get("szi") or 0.0))
        except (TypeError, ValueError):
            sz = 0.0
        try:
            entry = float(p.get("entry_px") or 0.0)
        except (TypeError, ValueError):
            entry = 0.0
        # Live mark from notional / size (both live fields on the position).
        try:
            ntl = abs(float(p.get("notional_usd") or p.get("positionValue") or 0.0))
            mark = (ntl / sz) if sz > 0 else 0.0
        except (TypeError, ValueError, ZeroDivisionError):
            mark = 0.0
        side = str(p.get("side") or "SHORT").upper()
        move = favorable_move_pct(side, entry, mark)
        if move is None:
            return False, ""
        pend = [
            t for t in crossed_thresholds(move)
            if not _already_fired(coin, t)
        ]
        if not pend:
            return False, ""
        # Fire ONCE for the HIGHEST newly-crossed threshold; mark every
        # crossed threshold as consumed so backlog never double-fires.
        for t in pend:
            _mark_fired(coin, t)
        msg = build_suggestion(coin, side, entry, mark, sz, move)
        return True, msg
    except Exception:  # noqa: BLE001
        log.exception("trailing evaluate_leg failed")
        return False, ""


async def run_trailing_alerts(bot, wallets: list[dict[str, Any]] | None) -> int:
    """Evaluate every basket leg; send one-shot suggestions. Returns sent."""
    sent = 0
    try:
        from config import TELEGRAM_CHAT_ID
        from utils.telegram import send_bot_message
    except Exception:  # noqa: BLE001
        return 0
    if not TELEGRAM_CHAT_ID:
        return 0
    try:
        for p in _basket_legs(wallets):
            fire, msg = evaluate_leg(p)
            if fire and msg:
                try:
                    await send_bot_message(bot, TELEGRAM_CHAT_ID, msg)
                    sent += 1
                except Exception:  # noqa: BLE001
                    log.exception("trailing alert send failed")
    except Exception:  # noqa: BLE001
        log.exception("run_trailing_alerts failed")
    return sent
