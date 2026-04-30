"""Round 20 — Calendar refresh module.

Ensures the macro calendar is always fresh when schedulers fire alerts.
Prevents stale cached events from triggering alerts on the wrong day.

Adapter is wired to modules.macro_calendar (existing SQLite-backed store).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from time_awareness import now_utc, should_fire_catalyst_alert

logger = logging.getLogger(__name__)


_CALENDAR_LAST_REFRESH: datetime | None = None
_CALENDAR_REFRESH_INTERVAL_SECONDS = 300  # 5 min max staleness
_CACHED_EVENTS: list[Dict[str, Any]] = []


def _load_calendar_from_source() -> List[Dict[str, Any]]:
    """Load calendar from primary source (SQLite via modules.macro_calendar).

    Returns list of event dicts with keys:
        id, name, timestamp_utc (datetime), category, impact, notes,
        affects, pre_event_action
    """
    try:
        # Lazy import to avoid circular deps; modules.macro_calendar is the
        # authoritative SQLite-backed store added in R17.
        from modules.macro_calendar import upcoming_events
    except Exception:  # noqa: BLE001
        logger.exception("calendar_refresh: failed to import macro_calendar")
        return []

    out: list[Dict[str, Any]] = []
    try:
        for ev in upcoming_events(limit=50):
            out.append({
                "id": ev.event_id,
                "name": ev.name,
                "timestamp_utc": ev.timestamp_utc,
                "category": ev.category,
                "impact": ev.impact_level,
                "notes": ev.notes or "",
                "affects": ", ".join(ev.affects_positions or []) or "fund posiciones",
                "pre_event_action": (
                    "Pre-evento: revisar HF, basket UPnL, kill triggers."
                ),
            })
    except Exception:  # noqa: BLE001
        logger.exception("calendar_refresh: upcoming_events() raised")
        return []

    return out


def refresh_calendar_if_stale(force: bool = False) -> List[Dict[str, Any]]:
    """Refresh calendar from source if cached version is stale.

    Returns: list of upcoming events (next 48h)
    """
    global _CALENDAR_LAST_REFRESH, _CACHED_EVENTS

    now = now_utc()
    needs_refresh = (
        force
        or _CALENDAR_LAST_REFRESH is None
        or (now - _CALENDAR_LAST_REFRESH).total_seconds()
        > _CALENDAR_REFRESH_INTERVAL_SECONDS
    )

    if needs_refresh:
        logger.info("calendar_refresh: refreshing calendar at %s", now.isoformat())
        _CACHED_EVENTS = _load_calendar_from_source()
        _CALENDAR_LAST_REFRESH = now

    return list(_CACHED_EVENTS)


def filter_events_for_alerts(
    events: List[Dict[str, Any]],
    alert_type: str,
) -> List[Dict[str, Any]]:
    """Filter events that should currently trigger alerts of given type.

    Uses time_awareness.should_fire_catalyst_alert which:
    - Skips past events
    - Validates we're in the firing window

    Args:
        events: list of dicts with at minimum 'timestamp_utc' (datetime)
        alert_type: 'T-24h' | 'T-2h' | 'T-30min'

    Returns: filtered list of events that should fire NOW
    """
    eligible: list[Dict[str, Any]] = []

    for event in events:
        event_utc = event.get("timestamp_utc")
        if isinstance(event_utc, str):
            try:
                event_utc = datetime.fromisoformat(event_utc.replace("Z", "+00:00"))
            except Exception:  # noqa: BLE001
                continue
        if not isinstance(event_utc, datetime):
            continue

        if should_fire_catalyst_alert(event_utc, alert_type):
            eligible.append(event)

    return eligible
