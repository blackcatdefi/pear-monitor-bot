"""Round 20 — Time awareness module for bot reports.

Provides utilities to:
- Get current UTC time consistently across the bot (single source of truth)
- Format timestamps for messages (Spanish day/month names)
- Validate event timing before sending alerts
- Calculate relative times AT SEND TIME, not at calendar load time

Bug fix:
    BCD reported on 2026-04-30 receiving stale catalyst alerts whose
    "en 1h 59m" / "en 29m" relative-time strings were computed against
    the wrong reference clock (or rendered with a delta-from-load-time
    rather than delta-from-send-time). This module centralises the
    "what time is it" question so every render uses the same fresh now.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


def now_utc() -> datetime:
    """Get current UTC time. Single source of truth."""
    return datetime.now(timezone.utc)


def format_timestamp(dt: Optional[datetime] = None) -> str:
    """Format timestamp for inclusion in bot messages.

    Returns: '🕐 jue 30 abr 2026 - 13:42 UTC'
    """
    if dt is None:
        dt = now_utc()

    days_en = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    months_en = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ]

    day_name = days_en[dt.weekday()]
    month_name = months_en[dt.month - 1]

    return f"🕐 {day_name} {dt.day} {month_name} {dt.year} - {dt.strftime('%H:%M')} UTC"


def calculate_time_to_event(
    event_utc: datetime,
    reference: Optional[datetime] = None,
) -> dict:
    """Calculate time delta from now (or reference) to event.

    CRITICAL: Always called at SEND time, never cached.

    Returns dict with:
        - is_past: bool (event already happened)
        - is_future: bool
        - delta_seconds: int
        - human_readable: str ("in 1h 59m" / "15min ago" / "already happened")
        - should_send_alert: bool (filter to skip past events)
    """
    if reference is None:
        reference = now_utc()

    if event_utc.tzinfo is None:
        event_utc = event_utc.replace(tzinfo=timezone.utc)

    delta = event_utc - reference
    delta_seconds = int(delta.total_seconds())

    is_past = delta_seconds < 0
    is_future = delta_seconds > 0

    if is_past:
        abs_seconds = abs(delta_seconds)
        if abs_seconds < 3600:
            human = f"{abs_seconds // 60}min ago"
        elif abs_seconds < 86400:
            human = f"{abs_seconds // 3600}h ago"
        else:
            human = f"{abs_seconds // 86400}d ago"
        should_send = False
    else:
        if delta_seconds < 3600:
            human = f"in {delta_seconds // 60}min"
        elif delta_seconds < 86400:
            hours = delta_seconds // 3600
            mins = (delta_seconds % 3600) // 60
            human = f"in {hours}h {mins}m" if mins > 0 else f"in {hours}h"
        else:
            human = f"in {delta_seconds // 86400}d"
        should_send = True

    return {
        "is_past": is_past,
        "is_future": is_future,
        "delta_seconds": delta_seconds,
        "human_readable": human,
        "should_send_alert": should_send,
    }


def is_alert_window_active(
    event_utc: datetime,
    window_minutes: int,
    tolerance_minutes: int = 5,
) -> bool:
    """Check if we're currently in the alert window for an event.

    Args:
        event_utc: When the event happens
        window_minutes: How many minutes before event the alert should fire
                        (e.g. 120 for T-2h)
        tolerance_minutes: How wide the firing window is (default 5min)

    Returns: True if NOW is within
        [event - window - tolerance, event - window + tolerance]

    This prevents firing T-2h alerts at random times of day for events
    that already passed.
    """
    if event_utc.tzinfo is None:
        event_utc = event_utc.replace(tzinfo=timezone.utc)

    now = now_utc()
    target_send_time = event_utc - timedelta(minutes=window_minutes)
    delta_from_target = abs((now - target_send_time).total_seconds()) / 60

    return delta_from_target <= tolerance_minutes


def should_fire_catalyst_alert(event_utc: datetime, alert_type: str) -> bool:
    """Determine if a catalyst alert should fire RIGHT NOW.

    alert_type: 'T-24h' | 'T-2h' | 'T-30min' | 'T-1h'

    Returns True only if:
    - The event is in the future
    - We are currently within the firing window for this alert_type
    """
    if event_utc.tzinfo is None:
        event_utc = event_utc.replace(tzinfo=timezone.utc)

    if event_utc < now_utc():
        logger.warning(
            "Skipping %s alert for past event %s", alert_type, event_utc.isoformat()
        )
        return False

    windows = {
        "T-24h": 1440,
        "T-2h": 120,
        "T-1h": 60,
        "T-30min": 30,
    }

    if alert_type not in windows:
        logger.error("Unknown alert_type: %s", alert_type)
        return False

    return is_alert_window_active(event_utc, windows[alert_type], tolerance_minutes=5)


def add_message_timestamp(message: str, position: str = "bottom") -> str:
    """Add explicit timestamp to a bot message.

    Args:
        message: original bot message
        position: 'top' (header) or 'bottom' (footer)

    Returns: message with timestamp prepended/appended
    """
    ts = format_timestamp()

    if position == "top":
        return f"{ts}\n\n{message}"
    # Plain text (no markdown wrapping) so it renders correctly with parse_mode=None.
    return f"{message}\n\n{ts}"
