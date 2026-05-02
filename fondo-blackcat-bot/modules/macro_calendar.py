"""Round 17 — Macro/catalysts calendar with proactive Telegram alerts.

Datos:
    - SQLite tabla `macro_events` (DATA_DIR/macro_calendar.db)
    - Pre-seed de eventos del 28-30 abril 2026 + ETHFI/HYPE unlocks de mayo
    - BCD puede agregar eventos vía /add_event y borrar vía /remove_event

Alertas:
    - T-24h, T-2h, T-30m por evento (max 1 alerta por nivel x evento)
    - Solo dispara si MACRO_CALENDAR_ENABLED=true (default true)
    - Cada disparo se persiste en la fila (cols alerted_24h/_2h/_30m)
"""
from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from config import DATA_DIR, TELEGRAM_CHAT_ID

log = logging.getLogger(__name__)

DB_PATH = os.path.join(DATA_DIR, "macro_calendar.db")

CATEGORIES = {"fomc", "unlock", "tge", "geopolitical", "earnings", "crypto", "other"}
IMPACT_LEVELS = {"low", "medium", "high", "critical"}


@dataclass
class MacroEvent:
    event_id: str
    name: str
    timestamp_utc: datetime
    category: str
    impact_level: str
    notes: str
    affects_positions: list[str]
    alerted_24h: bool = False
    alerted_2h: bool = False
    alerted_30m: bool = False


# ─── Pre-seed events (BCD's roadmap as of 2026-04-27) ───────────────────────
INITIAL_EVENTS: list[MacroEvent] = [
    MacroEvent(
        event_id="warsh_vote_2026_04_30",
        name="Warsh Senate Banking Committee vote (Fed Chair)",
        timestamp_utc=datetime(2026, 4, 30, 14, 0, tzinfo=timezone.utc),
        category="fomc",
        impact_level="high",
        notes="Hawkish Fed Chair confirmation. Bearish risk assets si confirma.",
        affects_positions=["basket_v5"],
    ),
    MacroEvent(
        event_id="powell_last_press_2026_04_30",
        name="Powell last FOMC conference",
        timestamp_utc=datetime(2026, 4, 30, 18, 30, tzinfo=timezone.utc),
        category="fomc",
        impact_level="critical",
        notes="High volatility. DO NOT add risk during.",
        affects_positions=["basket_v5", "flywheel", "trade_ciclo"],
    ),
    MacroEvent(
        event_id="mag7_earnings_2026_04_30",
        name="4 MAG7 earnings (incl. NVDA ATH)",
        timestamp_utc=datetime(2026, 4, 30, 20, 0, tzinfo=timezone.utc),
        category="earnings",
        impact_level="high",
        notes="Risk-on/off depending on results. NVDA ATH $5.2T.",
        affects_positions=["basket_v5"],
    ),
    MacroEvent(
        event_id="megaeth_tge_2026_04_30",
        name="MegaETH TGE",
        timestamp_utc=datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc),
        category="tge",
        impact_level="medium",
        notes="Possible capital rotation from L2s (favorable basket).",
        affects_positions=["basket_v5"],
    ),
    MacroEvent(
        event_id="eigen_unlock_2026_04_30",
        name="EIGEN unlock",
        timestamp_utc=datetime(2026, 4, 30, 0, 0, tzinfo=timezone.utc),
        category="unlock",
        impact_level="medium",
        notes="EIGEN selling pressure.",
        affects_positions=[],
    ),
    MacroEvent(
        event_id="hype_team_unlock_may_2026",
        name="HYPE team/founder unlock window",
        timestamp_utc=datetime(2026, 5, 6, 0, 0, tzinfo=timezone.utc),
        category="unlock",
        impact_level="critical",
        notes="9.9M HYPE/day (~$412M/day) during week of May 6-12. Monitor HF flywheel.",
        affects_positions=["flywheel"],
    ),
    MacroEvent(
        event_id="ethfi_unlock_2026_05_29",
        name="ETHFI unlock 95.7% dilution",
        timestamp_utc=datetime(2026, 5, 29, 0, 0, tzinfo=timezone.utc),
        category="unlock",
        impact_level="critical",
        notes="110M tokens unlock on top of 115M circulating. 95.7% dilution.",
        affects_positions=[],
    ),
]


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS macro_events (
            event_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            timestamp_utc TEXT NOT NULL,
            category TEXT NOT NULL,
            impact_level TEXT NOT NULL,
            notes TEXT,
            affects_positions TEXT,
            alerted_24h INTEGER DEFAULT 0,
            alerted_2h INTEGER DEFAULT 0,
            alerted_30m INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    return conn


def _row_to_event(r: sqlite3.Row) -> MacroEvent:
    affects = (r["affects_positions"] or "").split(",")
    affects = [a.strip() for a in affects if a.strip()]
    ts = r["timestamp_utc"]
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return MacroEvent(
        event_id=r["event_id"],
        name=r["name"],
        timestamp_utc=dt,
        category=r["category"],
        impact_level=r["impact_level"],
        notes=r["notes"] or "",
        affects_positions=affects,
        alerted_24h=bool(r["alerted_24h"]),
        alerted_2h=bool(r["alerted_2h"]),
        alerted_30m=bool(r["alerted_30m"]),
    )


def seed_initial_events() -> int:
    """Inserta INITIAL_EVENTS si la tabla está vacía. Idempotente: skip si event_id existe."""
    conn = _conn()
    inserted = 0
    try:
        for ev in INITIAL_EVENTS:
            cur = conn.execute(
                "SELECT 1 FROM macro_events WHERE event_id = ?", (ev.event_id,)
            )
            if cur.fetchone():
                continue
            conn.execute(
                """
                INSERT INTO macro_events
                  (event_id, name, timestamp_utc, category, impact_level, notes,
                   affects_positions)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ev.event_id,
                    ev.name,
                    ev.timestamp_utc.isoformat(),
                    ev.category,
                    ev.impact_level,
                    ev.notes,
                    ",".join(ev.affects_positions),
                ),
            )
            inserted += 1
        conn.commit()
    finally:
        conn.close()
    if inserted:
        log.info("macro_calendar: seeded %d initial events", inserted)
    return inserted


def add_event(
    event_id: str,
    name: str,
    timestamp_utc: datetime,
    category: str,
    impact_level: str,
    notes: str = "",
    affects_positions: Iterable[str] = (),
) -> bool:
    """Insert event. Returns True if new, False if event_id already exists."""
    if category not in CATEGORIES:
        category = "other"
    if impact_level not in IMPACT_LEVELS:
        impact_level = "medium"
    if timestamp_utc.tzinfo is None:
        timestamp_utc = timestamp_utc.replace(tzinfo=timezone.utc)
    conn = _conn()
    try:
        cur = conn.execute(
            "SELECT 1 FROM macro_events WHERE event_id = ?", (event_id,)
        )
        if cur.fetchone():
            return False
        conn.execute(
            """
            INSERT INTO macro_events
              (event_id, name, timestamp_utc, category, impact_level, notes,
               affects_positions)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                name,
                timestamp_utc.isoformat(),
                category,
                impact_level,
                notes,
                ",".join(affects_positions),
            ),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def remove_event(event_id: str) -> bool:
    conn = _conn()
    try:
        cur = conn.execute(
            "DELETE FROM macro_events WHERE event_id = ?", (event_id,)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def upcoming_events(limit: int = 10) -> list[MacroEvent]:
    """Próximos N eventos con timestamp >= now, ordenados por fecha asc."""
    now_iso = datetime.now(timezone.utc).isoformat()
    conn = _conn()
    try:
        cur = conn.execute(
            """
            SELECT * FROM macro_events
            WHERE timestamp_utc >= ?
            ORDER BY timestamp_utc ASC
            LIMIT ?
            """,
            (now_iso, limit),
        )
        return [_row_to_event(r) for r in cur.fetchall()]
    finally:
        conn.close()


def next_upcoming_event() -> MacroEvent | None:
    evs = upcoming_events(limit=1)
    return evs[0] if evs else None


def format_time_until(target: datetime) -> str:
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    delta = target - datetime.now(timezone.utc)
    total = int(delta.total_seconds())
    if total <= 0:
        return "already passed"
    days = total // 86400
    hours = (total % 86400) // 3600
    mins = (total % 3600) // 60
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins and not days:
        parts.append(f"{mins}m")
    return " ".join(parts) or f"{total}s"


def _impact_emoji(impact: str) -> str:
    return {
        "low": "🟢",
        "medium": "🟡",
        "high": "🟠",
        "critical": "🔴",
    }.get(impact, "⚪")


def _category_emoji(cat: str) -> str:
    return {
        "fomc": "🏛",
        "unlock": "🔓",
        "tge": "🚀",
        "geopolitical": "🌍",
        "earnings": "📈",
        "crypto": "₿",
        "other": "📌",
    }.get(cat, "📌")


def format_calendar(limit: int = 10) -> str:
    events = upcoming_events(limit=limit)
    if not events:
        return (
            "📅 Calendar EMPTY. Run /add_event to add events manually.\n"
            "Auto pre-seed: already available. It may have been deleted."
        )

    lines = [f"📅 UPCOMING {len(events)} CATALYSTS", "─" * 40]
    for ev in events:
        when = ev.timestamp_utc.strftime("%Y-%m-%d %H:%M UTC")
        until = format_time_until(ev.timestamp_utc)
        emoji = _impact_emoji(ev.impact_level)
        cat = _category_emoji(ev.category)
        lines.append(f"{emoji}{cat} {when} ({until})")
        lines.append(f"   {ev.name}")
        if ev.notes:
            lines.append(f"   📝 {ev.notes}")
        if ev.affects_positions:
            lines.append(f"   ⚠️ Affects: {', '.join(ev.affects_positions)}")
        lines.append(f"   id={ev.event_id}")
        lines.append("")

    lines.append("Comandos:")
    lines.append("  /add_event <id> <YYYY-MM-DDTHH:MMZ> <category> <impact> | <name>")
    lines.append("  /remove_event <id>")
    return "\n".join(lines)


def parse_add_event_args(args: list[str]) -> tuple[str, datetime, str, str, str]:
    """
    Format: <event_id> <YYYY-MM-DDTHH:MMZ> <category> <impact> | <name>
    Devuelve (event_id, ts, cat, impact, name).
    """
    text = " ".join(args)
    if "|" not in text:
        raise ValueError("Falta '|' separador. Formato: id ts cat impact | name")
    head, name = text.split("|", 1)
    parts = head.strip().split()
    if len(parts) < 4:
        raise ValueError(
            "Missing fields. Format: id ts cat impact | name"
        )
    event_id, ts_str, cat, impact = parts[0], parts[1], parts[2], parts[3]
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception as exc:
        raise ValueError(f"Invalid timestamp '{ts_str}': {exc}")
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return event_id, ts, cat.lower(), impact.lower(), name.strip()


# ─── Alert dispatcher (called by scheduler) ──────────────────────────────────


def _mark_alerted(event_id: str, level: str) -> None:
    col = {"24h": "alerted_24h", "2h": "alerted_2h", "30m": "alerted_30m"}[level]
    conn = _conn()
    try:
        conn.execute(
            f"UPDATE macro_events SET {col} = 1 WHERE event_id = ?", (event_id,)
        )
        conn.commit()
    finally:
        conn.close()


def _format_alert(ev: MacroEvent, level: str) -> str:
    until = format_time_until(ev.timestamp_utc)
    emoji = _impact_emoji(ev.impact_level)
    cat = _category_emoji(ev.category)
    when = ev.timestamp_utc.strftime("%Y-%m-%d %H:%M UTC")

    if level == "24h":
        head = f"📅 CATALYST T-24h — {ev.name}"
        body = (
            f"{emoji}{cat} {when} (in {until})\n"
            f"Category: {ev.category} | Impact: {ev.impact_level}\n"
        )
    elif level == "2h":
        head = f"⚠️ CATALYST T-2h — {ev.name}"
        body = (
            f"{emoji}{cat} {when} (in {until})\n"
            f"Category: {ev.category} | Impact: {ev.impact_level}\n"
            f"Pre-event: check HF, basket UPnL, kill triggers.\n"
        )
    else:  # 30m
        head = f"🚨 CATALYST T-30min — {ev.name}"
        body = (
            f"{emoji}{cat} {when} (in {until})\n"
            f"Impact: {ev.impact_level}\n"
            f"⛔ DO NOT open new positions in coming hours.\n"
        )

    if ev.notes:
        body += f"\n📝 {ev.notes}"
    if ev.affects_positions:
        body += f"\n⚠️ Afecta: {', '.join(ev.affects_positions)}"
    return f"{head}\n{body}"


async def check_and_dispatch_alerts(bot) -> int:
    """Ejecutar 1× cada minuto. Devuelve cantidad de alertas enviadas."""
    if os.getenv("MACRO_CALENDAR_ENABLED", "true").strip().lower() == "false":
        return 0
    if not TELEGRAM_CHAT_ID:
        return 0

    from utils.telegram import send_bot_message

    sent = 0
    now = datetime.now(timezone.utc)

    conn = _conn()
    try:
        cur = conn.execute(
            "SELECT * FROM macro_events WHERE timestamp_utc >= ? ORDER BY timestamp_utc ASC",
            (now.isoformat(),),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    for r in rows:
        ev = _row_to_event(r)
        delta = (ev.timestamp_utc - now).total_seconds()

        # Order matters: trigger CRIT (30m) before WARN (2h) before INFO (24h)
        if 0 < delta <= 30 * 60 and not ev.alerted_30m:
            try:
                await send_bot_message(
                    bot, TELEGRAM_CHAT_ID, _format_alert(ev, "30m")
                )
                _mark_alerted(ev.event_id, "30m")
                sent += 1
            except Exception:
                log.exception("macro_calendar 30m alert failed: %s", ev.event_id)
        elif 30 * 60 < delta <= 2 * 3600 and not ev.alerted_2h:
            try:
                await send_bot_message(
                    bot, TELEGRAM_CHAT_ID, _format_alert(ev, "2h")
                )
                _mark_alerted(ev.event_id, "2h")
                sent += 1
            except Exception:
                log.exception("macro_calendar 2h alert failed: %s", ev.event_id)
        elif 2 * 3600 < delta <= 24 * 3600 and not ev.alerted_24h:
            try:
                await send_bot_message(
                    bot, TELEGRAM_CHAT_ID, _format_alert(ev, "24h")
                )
                _mark_alerted(ev.event_id, "24h")
                sent += 1
            except Exception:
                log.exception("macro_calendar 24h alert failed: %s", ev.event_id)

    if sent:
        log.info("macro_calendar dispatched %d alert(s)", sent)
    return sent
