"""Round 21 — Morning brief scheduler.

Sends a daily anchor message at 10:00 UTC (07:00 AR) listing every catalyst
event scheduled for the rest of the day, with explicit UTC times and impact
emojis. Lands BEFORE any T-2h alert and prevents day-confusion.

Toggle:
    MORNING_BRIEF_ENABLED=true        (default)
    MORNING_BRIEF_HOUR_UTC=10         (default)

Wired to the bot via:
    scheduler.add_job(send_morning_brief_job, 'cron', hour=…)

The implementation reuses calendar_refresh.refresh_calendar_if_stale (sync)
and utils.telegram.send_bot_message for the actual delivery.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any

from time_awareness import now_utc
from message_header import format_header

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return os.getenv("MORNING_BRIEF_ENABLED", "true").strip().lower() != "false"


def _hour() -> int:
    try:
        return int(os.getenv("MORNING_BRIEF_HOUR_UTC", "10"))
    except ValueError:
        return 10


def _coerce_event_dt(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            return None
    return None


def _build_brief_text() -> str:
    """Build the morning brief message body (without header)."""
    try:
        from calendar_refresh import refresh_calendar_if_stale
        events = refresh_calendar_if_stale(force=True)
    except Exception:  # noqa: BLE001
        logger.exception("morning_brief: calendar refresh failed")
        events = []

    now = now_utc()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)

    today_events: list[tuple[datetime, dict]] = []
    for event in events:
        ev_utc = _coerce_event_dt(event.get("timestamp_utc"))
        if ev_utc is None:
            continue
        if today_start <= ev_utc < today_end:
            today_events.append((ev_utc, event))

    today_events.sort(key=lambda x: x[0])

    if today_events:
        impact_emoji = {
            "critical": "🔴", "high": "🟠",
            "medium": "🟡", "low": "🟢",
        }
        lines = []
        for ev_utc, ev in today_events:
            t_str = ev_utc.strftime("%H:%M UTC")
            name = ev.get("name", "evento")
            impact = (ev.get("impact") or "medium").lower()
            lines.append(f"  {impact_emoji.get(impact, '⚪')} {t_str} — {name}")
        events_section = "\n".join(lines)
    else:
        events_section = "  No catalyst events scheduled for today."

    return (
        "☀️ MORNING BRIEF\n\n"
        "📋 Catalyst events TODAY:\n"
        f"{events_section}\n\n"
        "🐈‍⬛ Operational reminder:\n"
        "- Pre-event high/critical: check HF, basket UPnL, kill triggers\n"
        "- DO NOT open new positions within T-30min of critical catalysts\n"
        "- Bot will alert at T-2h and T-30min for each event automatically\n\n"
        "System operational. Work in progress."
    )


async def send_morning_brief_job(bot) -> None:
    """Compose and send the morning brief.

    Args:
        bot: telegram.Bot instance (passed from APScheduler wrapper).
    """
    if not _enabled():
        logger.info("morning_brief: disabled by env (MORNING_BRIEF_ENABLED=false)")
        return

    try:
        from config import TELEGRAM_CHAT_ID
        from utils.telegram import send_bot_message
    except Exception:  # noqa: BLE001
        logger.exception("morning_brief: failed to import telegram helpers")
        return

    if not TELEGRAM_CHAT_ID:
        logger.warning("morning_brief: TELEGRAM_CHAT_ID empty — skipping send")
        return

    body = _build_brief_text()
    # Header is injected automatically by send_bot_message in R21,
    # but we prepend explicitly here to keep the brief well-anchored
    # even if the header toggle is flipped off.
    final = f"{format_header()}\n\n{body}"

    try:
        await send_bot_message(bot, TELEGRAM_CHAT_ID, final)
        logger.info("morning_brief: sent OK")
    except Exception:  # noqa: BLE001
        logger.exception("morning_brief: send failed")


def get_scheduled_hour_utc() -> int:
    """Public accessor used by bot.py when registering the cron job."""
    return _hour()
