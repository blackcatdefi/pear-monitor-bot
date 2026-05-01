"""Round 20 — Time-aware scheduler v2 for macro calendar alerts.

Key improvements over v1 (modules.macro_calendar.check_and_dispatch_alerts):
- Refreshes calendar at each run (no stale cache)
- Calculates "in X hours" at SEND time, not at calendar load time
- Filters past events explicitly (defence in depth vs SQL filter)
- Adds explicit absolute UTC timestamp to every alert message
- Logs every decision (sent / skipped / filtered) for forensics

Feature flag:
    TIME_AWARENESS_ENABLED=true  → bot.py routes scheduler to this v2
    TIME_AWARENESS_ENABLED=false → bot.py keeps v1 (rollback path)

Idempotency is shared with v1: this module reads/writes the same
alerted_24h/_2h/_30m SQLite columns as macro_calendar so a switch in
mid-day does not double-fire alerts already dispatched by v1.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict

from calendar_refresh import filter_events_for_alerts, refresh_calendar_if_stale
from time_awareness import (
    add_message_timestamp,
    calculate_time_to_event,
    now_utc,
)

logger = logging.getLogger(__name__)


_ALERT_TYPE_TO_COL = {
    "T-24h": "alerted_24h",
    "T-2h": "alerted_2h",
    "T-30min": "alerted_30m",
}

_ALERT_TYPE_TO_EMOJI = {
    "T-24h": "📅",
    "T-2h": "⚠️",
    "T-1h": "🔔",
    "T-30min": "🚨",
}


def _db_path() -> str:
    from config import DATA_DIR
    return os.path.join(DATA_DIR, "macro_calendar.db")


def _was_alert_sent(event_id: str, alert_type: str) -> bool:
    col = _ALERT_TYPE_TO_COL.get(alert_type)
    if not col:
        return True  # unknown type → don't fire
    try:
        conn = sqlite3.connect(_db_path())
        try:
            cur = conn.execute(
                f"SELECT {col} FROM macro_events WHERE event_id = ?",  # noqa: S608
                (event_id,),
            )
            row = cur.fetchone()
            return bool(row and row[0])
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        logger.exception("scheduler_v2: _was_alert_sent failed for %s", event_id)
        return True  # fail closed — don't double-fire on read error


def _mark_alert_sent(event_id: str, alert_type: str) -> None:
    col = _ALERT_TYPE_TO_COL.get(alert_type)
    if not col:
        return
    try:
        conn = sqlite3.connect(_db_path())
        try:
            conn.execute(
                f"UPDATE macro_events SET {col} = 1 WHERE event_id = ?",  # noqa: S608
                (event_id,),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        logger.exception("scheduler_v2: _mark_alert_sent failed for %s", event_id)


def _build_alert_body(event: Dict[str, Any], alert_type: str) -> str:
    event_utc = event.get("timestamp_utc")
    if isinstance(event_utc, str):
        event_utc = datetime.fromisoformat(event_utc.replace("Z", "+00:00"))

    # CRITICAL: calculate "in X hours" at SEND time, not load time
    timing = calculate_time_to_event(event_utc)

    emoji = _ALERT_TYPE_TO_EMOJI.get(alert_type, "⚠️")
    impact = event.get("impact", "medium")
    category = event.get("category", "macro")
    when_abs = event_utc.strftime("%Y-%m-%d %H:%M")
    pre_action = event.get(
        "pre_event_action",
        "Pre-evento: revisar HF, basket UPnL, kill triggers.",
    )

    body = (
        f"{emoji} CATALYST {alert_type} — {event['name']}\n\n"
        f"📅 Evento: {when_abs} UTC ({timing['human_readable']})\n"
        f"📂 Categoría: {category} | Impact: {impact}\n\n"
        f"{pre_action}\n"
    )
    if event.get("notes"):
        body += f"\n📝 {event['notes']}"
    if event.get("affects"):
        body += f"\n⚠️ Afecta: {event['affects']}"

    return add_message_timestamp(body, position="bottom")


def _build_post_alert_body(event: Dict[str, Any]) -> str:
    """Render the T+post (post-event) alert body for a critical catalyst.

    Pure post-event summary: reminds BCD to check positions/HF/basket
    UPnL, reads the event metadata. The LLM-driven analytical block lives
    elsewhere — this is a hook that BCD can act on.
    """
    event_utc = event.get("timestamp_utc")
    if isinstance(event_utc, str):
        event_utc = datetime.fromisoformat(event_utc.replace("Z", "+00:00"))
    when_abs = event_utc.strftime("%Y-%m-%d %H:%M") if event_utc else "?"
    name = event.get("name", "evento")
    impact = event.get("impact") or event.get("impact_level", "critical")
    category = event.get("category", "macro")
    affects = event.get("affects") or "—"
    notes = event.get("notes") or ""
    body = (
        f"🔴 CATALYST T+post — {name}\n\n"
        f"📅 Evento: {when_abs} UTC (hace ~15min)\n"
        f"📂 Categoría: {category} | Impact: {impact}\n"
        f"⚠️ Afecta: {affects}\n\n"
        "Post-evento checklist:\n"
        "  • Re-evaluar HF de cada wallet\n"
        "  • UPnL de basket vs pre-evento\n"
        "  • Triggers de kill (BTC, basket DD, UETH APY)\n"
        "  • Re-leer la tesis si la dirección quedó comprometida\n"
    )
    if notes:
        body += f"\n📝 {notes}"
    return add_message_timestamp(body, position="bottom")


async def _send_telegram(bot, message: str) -> None:
    """Adapter to existing send_bot_message helper."""
    try:
        from config import TELEGRAM_CHAT_ID
        from utils.telegram import send_bot_message
        if not TELEGRAM_CHAT_ID:
            logger.warning("scheduler_v2: TELEGRAM_CHAT_ID empty, skip send")
            return
        await send_bot_message(bot, TELEGRAM_CHAT_ID, message)
    except Exception:  # noqa: BLE001
        logger.exception("scheduler_v2: _send_telegram failed")


async def run_calendar_alert_check(application) -> int:
    """Main scheduler entry point. Runs every 1 min. Returns alerts sent.

    Honours MACRO_CALENDAR_ENABLED kill switch (same as v1).
    """
    if os.getenv("MACRO_CALENDAR_ENABLED", "true").strip().lower() == "false":
        return 0

    current_time = now_utc()
    logger.info("scheduler_v2: alert check at %s", current_time.isoformat())

    all_events = refresh_calendar_if_stale()
    if not all_events:
        return 0

    sent = 0
    bot = getattr(application, "bot", application)

    # R-SILENT: catalyst gate filters by impact_level + timing.
    try:
        from auto import catalyst_alert_gate as cgate  # noqa: WPS433
    except Exception:  # noqa: BLE001
        cgate = None  # type: ignore[assignment]
    try:
        from auto import silent_mode as _silent  # noqa: WPS433
    except Exception:  # noqa: BLE001
        _silent = None  # type: ignore[assignment]

    for alert_type in ("T-24h", "T-2h", "T-30min"):
        eligible = filter_events_for_alerts(all_events, alert_type)
        for event in eligible:
            event_id = event.get("id") or event.get("event_id")
            if not event_id:
                continue

            # R-SILENT gate: only critical T-30min by default.
            if cgate is not None and not cgate.should_fire_pre(event, alert_type):
                logger.info(
                    "scheduler_v2: gate skip %s for %s (impact=%s, gate denied)",
                    alert_type, event_id, event.get("impact_level") or event.get("impact"),
                )
                # Mark as sent so we don't keep evaluating this slot every minute.
                _mark_alert_sent(event_id, alert_type)
                continue

            # Silent-mode override: even allowed pre-alerts (T-30min critical) skipped.
            if _silent is not None and _silent.is_silent():
                logger.info(
                    "scheduler_v2: silent_mode ON, suppress %s for %s",
                    alert_type, event_id,
                )
                _mark_alert_sent(event_id, alert_type)
                continue

            if _was_alert_sent(event_id, alert_type):
                logger.debug(
                    "scheduler_v2: %s already sent for %s, skip",
                    alert_type,
                    event_id,
                )
                continue

            # Defence in depth: re-verify event is in the future at send time
            ev_utc = event.get("timestamp_utc")
            if isinstance(ev_utc, str):
                ev_utc = datetime.fromisoformat(ev_utc.replace("Z", "+00:00"))
            if ev_utc and ev_utc < now_utc():
                logger.warning(
                    "scheduler_v2: skipping %s for past event %s @ %s",
                    alert_type,
                    event_id,
                    ev_utc.isoformat(),
                )
                continue

            try:
                msg = _build_alert_body(event, alert_type)
                await _send_telegram(bot, msg)
                _mark_alert_sent(event_id, alert_type)
                sent += 1
                logger.info(
                    "scheduler_v2: dispatched %s alert for %s",
                    alert_type,
                    event_id,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "scheduler_v2: failed to dispatch %s for %s",
                    alert_type,
                    event_id,
                )

    # R-SILENT: post-event T+15min critical analysis.
    if cgate is not None:
        for event in all_events:
            event_id = event.get("id") or event.get("event_id")
            if not event_id:
                continue
            if not cgate.should_fire_post(event):
                continue
            if _silent is not None and _silent.is_silent():
                if not _silent.catalyst_post_allowed():
                    logger.info(
                        "scheduler_v2: silent_mode suppresses post for %s", event_id,
                    )
                    cgate.mark_post_sent(event)
                    continue
            try:
                msg = _build_post_alert_body(event)
                await _send_telegram(bot, msg)
                cgate.mark_post_sent(event)
                sent += 1
                logger.info("scheduler_v2: dispatched T+post alert for %s", event_id)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "scheduler_v2: failed to dispatch T+post for %s", event_id,
                )

    if sent:
        logger.info("scheduler_v2: dispatched %d alert(s)", sent)
    return sent
