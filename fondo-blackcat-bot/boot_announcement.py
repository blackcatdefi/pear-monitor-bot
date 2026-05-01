"""Round 21 — Boot announcement.

Sends an explicit ``🟢 BOT ONLINE`` Telegram message every time the bot
starts. Confirms UTC clock validation, calendar refresh and lists every
catalyst event still pending in the rest of the current day.

Toggle:
    BOOT_ANNOUNCEMENT_ENABLED=true   (default)
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
    return os.getenv("BOOT_ANNOUNCEMENT_ENABLED", "true").strip().lower() != "false"


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


def _build_boot_text() -> str:
    """Build the boot announcement body (without header)."""
    try:
        from calendar_refresh import refresh_calendar_if_stale
        events = refresh_calendar_if_stale(force=True)
        calendar_ok = True
    except Exception:  # noqa: BLE001
        logger.exception("boot_announcement: calendar refresh failed")
        events = []
        calendar_ok = False

    now = now_utc()
    today_end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

    pending: list[tuple[datetime, dict]] = []
    for event in events:
        ev_utc = _coerce_event_dt(event.get("timestamp_utc"))
        if ev_utc is None:
            continue
        if now <= ev_utc < today_end:
            pending.append((ev_utc, event))

    pending.sort(key=lambda x: x[0])

    # R-SILENT: filter to *critical* impact only — those are the events the bot
    # will actually alert on (catalyst gate). Anything else is noise on boot.
    critical_only = [
        (ev_utc, ev)
        for ev_utc, ev in pending
        if str(ev.get("impact_level", "")).lower() == "critical"
    ]

    if critical_only:
        lines = []
        for ev_utc, ev in critical_only:
            t_str = ev_utc.strftime("%H:%M UTC")
            name = ev.get("name", "evento")
            lines.append(f"  • {t_str} — {name}")
        events_section = "📋 Catalysts hoy:\n" + "\n".join(lines)
    else:
        events_section = "📋 Sin catalysts critical hoy."

    if not calendar_ok:
        events_section = "⚠️ Calendar refresh failed.\n" + events_section

    return events_section


async def announce_boot(bot) -> None:
    """Send the boot announcement message.

    Args:
        bot: telegram.Bot instance (already initialised when called).
    """
    if not _enabled():
        logger.info("boot_announcement: disabled by env (BOOT_ANNOUNCEMENT_ENABLED=false)")
        return

    try:
        from config import TELEGRAM_CHAT_ID
        from utils.telegram import send_bot_message
    except Exception:  # noqa: BLE001
        logger.exception("boot_announcement: failed to import telegram helpers")
        return

    if not TELEGRAM_CHAT_ID:
        logger.warning("boot_announcement: TELEGRAM_CHAT_ID empty — skipping send")
        return

    body = _build_boot_text()
    final = f"{format_header()}\n\n🟢 BOT ONLINE\n{body}"

    try:
        await send_bot_message(bot, TELEGRAM_CHAT_ID, final)
        logger.info("boot_announcement: sent OK")
    except Exception:  # noqa: BLE001
        logger.exception("boot_announcement: send failed")
