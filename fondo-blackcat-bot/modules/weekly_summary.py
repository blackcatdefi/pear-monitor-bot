"""Round 17 — Weekly summary scheduled job (Sunday 18:00 UTC).

Aggrega:
  - Performance: capital inicial/final/Δ, PnL realizado/no realizado (snapshots table)
  - Eventos destacados: top errors, fills, intel
  - Próxima semana: macro_calendar.upcoming_events(7d)
  - Top intel mentions

Sale como UN único mensaje a TELEGRAM_CHAT_ID. No lleva botones.

Kill switch: WEEKLY_SUMMARY_ENABLED=false desactiva el scheduler.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from config import DATA_DIR

log = logging.getLogger(__name__)


def _enabled() -> bool:
    return os.getenv("WEEKLY_SUMMARY_ENABLED", "true").strip().lower() != "false"


def _db_path() -> str:
    return os.path.join(DATA_DIR, "intel_memory.db")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cur.fetchone() is not None


# ─── Capital snapshot delta ──────────────────────────────────────────────────


def _capital_delta(conn: sqlite3.Connection, since_iso: str) -> dict[str, Any]:
    """Read first snapshot at-or-after `since_iso` and the latest snapshot."""
    out = {"start": None, "end": None, "delta": None, "delta_pct": None}
    if not _table_exists(conn, "snapshots"):
        return out
    try:
        cur = conn.execute(
            "SELECT timestamp_utc, data_json FROM snapshots "
            "WHERE timestamp_utc >= ? ORDER BY timestamp_utc ASC LIMIT 1",
            (since_iso,),
        )
        row_start = cur.fetchone()
        cur = conn.execute(
            "SELECT timestamp_utc, data_json FROM snapshots ORDER BY timestamp_utc DESC LIMIT 1"
        )
        row_end = cur.fetchone()
        if not row_start or not row_end:
            return out

        import json

        def _capital_from(j: Any) -> float | None:
            try:
                d = json.loads(j) if isinstance(j, str) else j
                if not isinstance(d, dict):
                    return None
                # Heurístico: buscar 'capital_total' o suma hl - debt + perp
                if "capital_total" in d:
                    return float(d["capital_total"])
                hl_c = float(d.get("hl_collateral") or 0)
                hl_d = float(d.get("hl_debt") or 0)
                pa = float(d.get("perp_acct") or 0)
                if hl_c or hl_d or pa:
                    return hl_c - hl_d + pa
            except Exception:
                return None
            return None

        s = _capital_from(row_start[1])
        e = _capital_from(row_end[1])
        out["start"] = s
        out["end"] = e
        if s is not None and e is not None:
            out["delta"] = e - s
            if s > 0:
                out["delta_pct"] = (e - s) / s * 100
    except Exception:
        log.exception("capital_delta failed")
    return out


# ─── Fills + intel highlights ────────────────────────────────────────────────


def _highlight_fills(conn: sqlite3.Connection, since_iso: str) -> list[dict]:
    if not _table_exists(conn, "position_log"):
        return []
    try:
        cur = conn.execute(
            "SELECT timestamp_utc, kind, asset, amount_usd, message FROM position_log "
            "WHERE timestamp_utc >= ? ORDER BY ABS(amount_usd) DESC LIMIT 5",
            (since_iso,),
        )
        return [
            {
                "ts": r[0],
                "kind": r[1],
                "asset": r[2],
                "usd": r[3],
                "msg": r[4],
            }
            for r in cur.fetchall()
        ]
    except Exception:
        log.exception("highlight_fills failed")
        return []


def _highlight_intel(conn: sqlite3.Connection, since_iso: str) -> list[dict]:
    if not _table_exists(conn, "intel_memory"):
        return []
    try:
        cur = conn.execute(
            "SELECT timestamp_utc, source, raw_text FROM intel_memory "
            "WHERE timestamp_utc >= ? ORDER BY timestamp_utc DESC LIMIT 5",
            (since_iso,),
        )
        return [{"ts": r[0], "src": r[1], "text": r[2]} for r in cur.fetchall()]
    except Exception:
        log.exception("highlight_intel failed")
        return []


def _error_count(conn: sqlite3.Connection, since_iso: str) -> int:
    if not _table_exists(conn, "errors_log"):
        return 0
    try:
        cur = conn.execute(
            "SELECT COUNT(*) FROM errors_log WHERE timestamp_utc >= ?", (since_iso,)
        )
        r = cur.fetchone()
        return int(r[0]) if r else 0
    except Exception:
        return 0


# ─── Renderer ────────────────────────────────────────────────────────────────


def _fmt_usd(v: Any, dec: int = 2) -> str:
    try:
        return f"${float(v):,.{dec}f}"
    except Exception:
        return "—"


def _fmt_signed(v: Any, dec: int = 2) -> str:
    try:
        f = float(v)
        sign = "+" if f >= 0 else "-"
        return f"{sign}${abs(f):,.{dec}f}"
    except Exception:
        return "—"


def _fmt_pct(v: Any, dec: int = 2) -> str:
    try:
        f = float(v)
        sign = "+" if f >= 0 else ""
        return f"{sign}{f:.{dec}f}%"
    except Exception:
        return "—"


def build_summary() -> str:
    """Build text summary for the past 7 days (UTC)."""
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    since_iso = week_ago.isoformat()

    cap = {"start": None, "end": None, "delta": None, "delta_pct": None}
    fills: list[dict] = []
    intel: list[dict] = []
    errs = 0

    db = _db_path()
    if os.path.isfile(db):
        try:
            conn = sqlite3.connect(db)
            cap = _capital_delta(conn, since_iso)
            fills = _highlight_fills(conn, since_iso)
            intel = _highlight_intel(conn, since_iso)
            errs = _error_count(conn, since_iso)
            conn.close()
        except Exception:
            log.exception("summary read failed")

    upcoming: list = []
    try:
        from modules.macro_calendar import upcoming_events
        upcoming = upcoming_events(limit=8, within_days=8)
    except Exception:
        log.exception("upcoming events fetch failed")

    week_label = now.strftime("%G-W%V")
    range_label = f"{week_ago.strftime('%Y-%m-%d')} → {now.strftime('%Y-%m-%d')}"

    lines: list[str] = [
        f"📅 RESUMEN SEMANAL — {week_label}",
        f"({range_label})",
        "─" * 42,
        "",
        "📊 PERFORMANCE",
    ]
    if cap["start"] is None or cap["end"] is None:
        lines.append("  • Capital: snapshots insuficientes (necesita ≥2 /reporte en la semana)")
    else:
        lines.append(f"  • Capital inicial: {_fmt_usd(cap['start'])}")
        lines.append(f"  • Capital final: {_fmt_usd(cap['end'])}")
        delta = _fmt_signed(cap["delta"])
        pct = _fmt_pct(cap["delta_pct"])
        lines.append(f"  • Δ semana: {delta} ({pct})")

    lines.append("")
    lines.append("🎯 EVENTOS DESTACADOS")
    if fills:
        for f in fills:
            ts = (f.get("ts") or "")[:10]
            kind = f.get("kind") or "?"
            asset = f.get("asset") or "?"
            usd = _fmt_signed(f.get("usd"))
            lines.append(f"  • {ts} {kind} {asset} {usd}")
    else:
        lines.append("  • Sin fills registrados (position_log vacío o no instrumentado)")

    if errs:
        lines.append(f"  ⚠️ Errores 7d: {errs} (ver /errors)")

    lines.append("")
    lines.append("📡 INTEL TOP 5")
    if intel:
        for it in intel:
            ts = (it.get("ts") or "")[:10]
            src = it.get("src") or "?"
            txt = (it.get("text") or "").replace("\n", " ").strip()[:140]
            lines.append(f"  • {ts} [{src}] {txt}")
    else:
        lines.append("  • Sin entradas en intel_memory esta semana")

    lines.append("")
    lines.append("🔮 PRÓXIMA SEMANA")
    if upcoming:
        for ev in upcoming:
            try:
                when = ev.timestamp_utc.strftime("%Y-%m-%d %H:%M UTC")
                lines.append(
                    f"  • {when} — {ev.name} [{ev.impact_level}/{ev.category}]"
                )
            except Exception:
                continue
    else:
        lines.append("  • Sin catalysts próximos (revisar /calendar)")

    lines.append("")
    lines.append("💡 Recordá: target ≤5 /reporte en 2 semanas. R17 trabaja por vos.")
    return "\n".join(lines)


async def scheduled_summary(bot) -> None:
    """APScheduler entry point — Sunday 18:00 UTC."""
    if not _enabled():
        log.info("weekly_summary disabled via WEEKLY_SUMMARY_ENABLED=false")
        return
    try:
        from config import TELEGRAM_CHAT_ID
        from utils.telegram import send_bot_message  # R20: auto-stamp timestamp
        text = build_summary()
        await send_bot_message(bot, TELEGRAM_CHAT_ID, text)
        log.info("weekly_summary sent (%d chars)", len(text))
    except Exception:
        log.exception("weekly_summary dispatch failed")
