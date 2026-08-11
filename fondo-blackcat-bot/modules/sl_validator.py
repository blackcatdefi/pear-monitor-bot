"""R-BOT-DEFINITIVE WI-5 — SL/TP structural sanity validator.

Live bug it kills (2026-06-10): the HOOD short carries a native SL at 99.58
beyond its isolated liq price 94.90. That stop can NEVER execute — the
liquidation fires first — and nothing flagged it.

Reachability rule (price path to the stop crosses the liquidation first):
  * SHORT: losses as price RISES; liq sits ABOVE mark. A protective BUY stop
    with trigger >= liq price is UNREACHABLE.
  * LONG: losses as price FALLS; liq sits BELOW mark. A protective SELL stop
    with trigger <= liq price is UNREACHABLE.

Surfaces:
  * ``sl_unreachable(side, sl_px, liq_px)`` — pure check (used inline by the
    position classifier to print "SL UNREACHABLE: liq executes first").
  * ``run_sl_reachability_alerts`` — ONE-TIME Telegram alert per position per
    condition (SQLite edge state; re-arms only when the condition changes:
    SL moved / became reachable / position gone).

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

DB_PATH = os.path.join(DATA_DIR, "sl_validator.db")


def sl_unreachable(side: str, sl_px: float | None, liq_px: float | None) -> bool:
    """True iff the SL can never execute because liquidation fires first."""
    try:
        if sl_px is None or liq_px is None:
            return False
        sl = float(sl_px)
        liq = float(liq_px)
        if sl <= 0 or liq <= 0:
            return False
        s = (side or "").upper()
        if s == "SHORT":
            return sl >= liq
        if s == "LONG":
            return sl <= liq
        return False
    except (TypeError, ValueError):
        return False


def unreachable_flag_text(sl_px: float, liq_px: float) -> str:
    return (
        f"🚫 SL UNREACHABLE: liq executes first "
        f"(SL ${sl_px:,.2f} vs liq ${liq_px:,.2f})"
    )


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sl_unreachable_state (
            coin TEXT PRIMARY KEY,
            sl_px REAL,
            liq_px REAL,
            alerted INTEGER DEFAULT 0
        )
        """
    )
    return conn


def should_alert(coin: str, sl_px: float, liq_px: float) -> bool:
    """R-LTV65-QUIET 2.2 — fire ONCE per position; re-fire ONLY on MATERIAL
    change.

    Kills the 15+/48h re-fire storm for a static position: the old 0.5% wiggle
    re-arm treated normal liq-price drift (funding) as "new condition".

    Material change (any → re-fire):
      * new position (no stored state),
      * SL modified (>0.1% move — a placed order does not drift),
      * leg margin changed (liq px moved >2% — margin add/remove moves liq far
        more than funding drift), or
      * the SL–liq gap COMPRESSED >10% vs the last alerted gap (danger grew).
    Otherwise silent — the condition still shows as a compact line in the
    /reporte POSITIONS block and in the once-per-day digest.
    """
    try:
        c = (coin or "?").upper()
        conn = _conn()
        try:
            cur = conn.execute(
                "SELECT sl_px, liq_px, alerted FROM sl_unreachable_state WHERE coin=?",
                (c,),
            )
            row = cur.fetchone()
            material = True
            if row is not None and row[0] and row[1] and row[2]:
                try:
                    old_sl, old_liq = float(row[0]), float(row[1])
                    sl_moved = abs(old_sl - sl_px) / max(abs(old_sl), 1e-9) > 0.001
                    liq_moved = abs(old_liq - liq_px) / max(abs(old_liq), 1e-9) > 0.02
                    old_gap = abs(old_sl - old_liq)
                    new_gap = abs(float(sl_px) - float(liq_px))
                    gap_compressed = (
                        old_gap > 1e-9 and new_gap < old_gap * 0.90
                    )
                    material = sl_moved or liq_moved or gap_compressed
                except (TypeError, ValueError, ZeroDivisionError):
                    material = True
            if material:
                # Store the NEW baseline only when (re)alerting, so drift
                # accumulates against the last ALERTED snapshot.
                conn.execute(
                    "INSERT INTO sl_unreachable_state (coin, sl_px, liq_px, alerted) "
                    "VALUES (?, ?, ?, 1) "
                    "ON CONFLICT(coin) DO UPDATE SET sl_px=excluded.sl_px, "
                    "liq_px=excluded.liq_px, alerted=1",
                    (c, float(sl_px), float(liq_px)),
                )
                conn.commit()
            return material
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        log.exception("sl_validator should_alert failed for %s", coin)
        return False


_DIGEST_KEY = "__DIGEST_TS__"


def _digest_due(now: float | None = None) -> bool:
    """True max once per 24h (persisted). Marks sent when returning True."""
    import time as _time
    t = _time.time() if now is None else float(now)
    try:
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT sl_px FROM sl_unreachable_state WHERE coin=?",
                (_DIGEST_KEY,),
            ).fetchone()
            last = float(row[0]) if row and row[0] else 0.0
            if (t - last) < 24 * 3600.0:
                return False
            conn.execute(
                "INSERT INTO sl_unreachable_state (coin, sl_px, liq_px, alerted) "
                "VALUES (?, ?, 0, 1) "
                "ON CONFLICT(coin) DO UPDATE SET sl_px=excluded.sl_px",
                (_DIGEST_KEY, t),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return False


def clear_condition(coin: str) -> None:
    """Condition resolved (SL reachable / position closed) — re-arm."""
    try:
        conn = _conn()
        try:
            conn.execute(
                "DELETE FROM sl_unreachable_state WHERE coin=?",
                ((coin or "?").upper(),),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def find_unreachable(
    wallets: list[dict[str, Any]] | None,
    market: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Scan every classified position; return unreachable-SL findings.

    Each finding: {coin, side, sl_px, liq_px}. Positions WITH a reachable SL
    (or no SL / no liq data) produce nothing — zero false positives by
    construction. NEVER raises.
    """
    out: list[dict[str, Any]] = []
    seen_ok: list[str] = []
    try:
        from modules.position_classifier import classify_portfolio
        tags = classify_portfolio(wallets, market)
        for t in tags:
            if t.has_sl and t.sl_px and t.liq_px:
                if sl_unreachable(t.side, t.sl_px, t.liq_px):
                    out.append({
                        "coin": t.coin,
                        "side": t.side,
                        "sl_px": float(t.sl_px),
                        "liq_px": float(t.liq_px),
                    })
                else:
                    seen_ok.append(t.coin)
        for c in seen_ok:
            clear_condition(c)
    except Exception:  # noqa: BLE001
        log.exception("find_unreachable failed")
    return out


async def run_sl_reachability_alerts(
    bot,
    wallets: list[dict[str, Any]] | None,
    market: dict[str, Any] | None = None,
) -> int:
    """Send one-time alerts for unreachable SLs. Returns alerts sent."""
    sent = 0
    try:
        from config import TELEGRAM_CHAT_ID
        from utils.telegram import send_bot_message
    except Exception:  # noqa: BLE001
        return 0
    if not TELEGRAM_CHAT_ID:
        return 0
    try:
        unchanged: list[dict[str, Any]] = []
        for f in find_unreachable(wallets, market):
            if should_alert(f["coin"], f["sl_px"], f["liq_px"]):
                msg = (
                    f"🚫 SL UNREACHABLE — {f['side']} {f['coin']}\n"
                    f"El SL nativo ${f['sl_px']:,.2f} está MÁS ALLÁ del liq price "
                    f"${f['liq_px']:,.2f}: la liquidación ejecuta primero y ese "
                    "stop nunca puede ejecutarse.\n"
                    "Acción sugerida (manual, BCD ejecuta): mover el SL dentro "
                    "del rango alcanzable o agregar margen a la pata."
                )
                try:
                    await send_bot_message(bot, TELEGRAM_CHAT_ID, msg)
                    sent += 1
                except Exception:  # noqa: BLE001
                    log.exception("SL-unreachable alert send failed")
            else:
                unchanged.append(f)
        # R-LTV65-QUIET 2.2 — unchanged conditions: ONE compact digest line,
        # max once per day (the condition stays visible in /reporte anyway).
        if unchanged and _digest_due():
            items = " · ".join(
                f"{f['side']} {f['coin']} (SL ${f['sl_px']:,.2f} vs liq "
                f"${f['liq_px']:,.2f})"
                for f in unchanged
            )
            digest = (
                f"🚫 SL UNREACHABLE (digest diario, sin cambios): {items}. "
                "Detalle en /reporte."
            )
            try:
                await send_bot_message(bot, TELEGRAM_CHAT_ID, digest)
                sent += 1
            except Exception:  # noqa: BLE001
                log.exception("SL-unreachable digest send failed")
    except Exception:  # noqa: BLE001
        log.exception("run_sl_reachability_alerts failed")
    return sent
