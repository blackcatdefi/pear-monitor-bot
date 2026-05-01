"""R-SILENT — Catalyst alert gate.

The legacy ``scheduler_calendar_v2`` fires alerts at T-24h / T-2h / T-30min
for every event whose ``impact_level`` is ``medium``, ``high``, or
``critical``. BCD's lived experience showed those alerts were noise — he
already has a calendar mental model + daily 7am report. The only material
moments are:

    * T-30min on **critical** events (FOMC press, war/peace pivots, large
      unlocks, whale liquidations on fund assets).
    * T+15min after a **critical** event so the bot can deliver a short
      post-event analysis.

Everything else is suppressed by default. Configurable via env vars:

    CATALYST_ALERT_IMPACTS=critical             # comma list — which impact_levels alert
    CATALYST_ALERT_TIMINGS=t30,t_post           # comma list — which alert types fire
    CATALYST_POSTEVENT_DELAY_MIN=15             # how long after the event T+ alert fires
    CATALYST_POSTEVENT_WINDOW_MIN=10            # tolerance window around T+

Public API
----------
``should_fire_pre(event, alert_type) -> bool``
    True iff this T-* (T-24h | T-2h | T-30min) alert is allowed.

``should_fire_post(event, *, now=None) -> bool``
    True iff a T+15min post-event alert is due RIGHT NOW for this event.

``post_alert_id(event) -> str``
    Persistent alert identifier for the post-event slot, used to dedup.

``mark_post_sent(event) -> None`` / ``was_post_sent(event) -> bool``
    SQLite-backed dedup for post-event alerts (one per event).

Kill switch: ``CATALYST_GATE_ENABLED=false`` → bypass (legacy behaviour).
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


def _envset(name: str, default: str) -> set[str]:
    raw = os.getenv(name, default).strip()
    parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    return set(parts)


ENABLED = os.getenv("CATALYST_GATE_ENABLED", "true").strip().lower() != "false"
ALLOW_IMPACTS = _envset("CATALYST_ALERT_IMPACTS", "critical")
ALLOW_TIMINGS = _envset("CATALYST_ALERT_TIMINGS", "t30,t_post")

POSTEVENT_DELAY_MIN = max(1, int(os.getenv("CATALYST_POSTEVENT_DELAY_MIN", "15") or 15))
POSTEVENT_WINDOW_MIN = max(1, int(os.getenv("CATALYST_POSTEVENT_WINDOW_MIN", "10") or 10))


_TIMING_ALIASES = {
    "T-24h": "t24",
    "T-2h": "t2",
    "T-30min": "t30",
    "t_post": "t_post",
}


def _timing_token(alert_type: str) -> str:
    return _TIMING_ALIASES.get(alert_type, str(alert_type).lower())


def _db_path() -> str:
    try:
        from config import DATA_DIR  # type: ignore

        base = DATA_DIR
    except Exception:  # noqa: BLE001
        base = os.path.join(os.path.dirname(__file__), "..", "data")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "catalyst_alerts.db")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_db_path())
    c.execute(
        """CREATE TABLE IF NOT EXISTS catalyst_post_alerts (
            event_id TEXT PRIMARY KEY,
            ts_epoch REAL NOT NULL
        )"""
    )
    return c


def _impact_of(event: dict[str, Any]) -> str:
    raw = event.get("impact_level") or event.get("impact") or "medium"
    return str(raw).lower()


def post_alert_id(event: dict[str, Any]) -> str:
    eid = event.get("event_id") or event.get("id") or ""
    return f"post:{eid}"


def should_fire_pre(event: dict[str, Any], alert_type: str) -> bool:
    """Should this *pre-event* alert be allowed through?

    ``alert_type`` is one of ``T-24h``, ``T-2h``, ``T-30min``.
    """
    if not ENABLED:
        return True
    timing = _timing_token(alert_type)
    if timing not in ALLOW_TIMINGS:
        log.debug("catalyst_gate: skip %s (timing not allowed)", alert_type)
        return False
    impact = _impact_of(event)
    if impact not in ALLOW_IMPACTS:
        log.debug(
            "catalyst_gate: skip %s for %s (impact=%s not allowed)",
            alert_type,
            event.get("event_id") or event.get("id"),
            impact,
        )
        return False
    return True


def _coerce_event_dt(event: dict[str, Any]) -> datetime | None:
    raw = event.get("timestamp_utc") or event.get("ts_utc") or event.get("ts")
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            return None
    return None


def should_fire_post(
    event: dict[str, Any], *, now: datetime | None = None
) -> bool:
    """Should the post-event T+POSTEVENT_DELAY_MIN alert fire right now?

    Returns True iff:
      * gate enabled, ``t_post`` in allowed timings, impact_level allowed
      * we are inside the [T+delay, T+delay+window) window
      * this event_id has not been marked post-sent yet
    """
    if not ENABLED:
        return False
    if "t_post" not in ALLOW_TIMINGS:
        return False
    impact = _impact_of(event)
    if impact not in ALLOW_IMPACTS:
        return False
    ev_dt = _coerce_event_dt(event)
    if ev_dt is None:
        return False
    if now is None:
        now = datetime.now(timezone.utc)
    delta_min = (now - ev_dt).total_seconds() / 60.0
    if delta_min < POSTEVENT_DELAY_MIN:
        return False
    if delta_min >= POSTEVENT_DELAY_MIN + POSTEVENT_WINDOW_MIN:
        return False
    if was_post_sent(event):
        return False
    return True


def was_post_sent(event: dict[str, Any]) -> bool:
    eid = post_alert_id(event)
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT ts_epoch FROM catalyst_post_alerts WHERE event_id=?",
                (eid,),
            ).fetchone()
    except Exception:  # noqa: BLE001
        log.exception("catalyst_gate: was_post_sent failed for %s", eid)
        return False
    return row is not None


def mark_post_sent(event: dict[str, Any]) -> None:
    eid = post_alert_id(event)
    try:
        with _conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO catalyst_post_alerts(event_id, ts_epoch) VALUES(?, ?)",
                (eid, time.time()),
            )
            # cap at last 200 entries
            c.execute(
                "DELETE FROM catalyst_post_alerts WHERE event_id NOT IN ("
                "  SELECT event_id FROM catalyst_post_alerts ORDER BY ts_epoch DESC LIMIT 200"
                ")"
            )
    except Exception:  # noqa: BLE001
        log.exception("catalyst_gate: mark_post_sent failed for %s", eid)


def _reset_for_tests() -> None:
    path = _db_path()
    if os.path.isfile(path):
        try:
            os.remove(path)
        except Exception:  # noqa: BLE001
            pass


def status_summary() -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    try:
        with _conn() as c:
            for eid, ts in c.execute(
                "SELECT event_id, ts_epoch FROM catalyst_post_alerts ORDER BY ts_epoch DESC LIMIT 20"
            ):
                rows.append({"event_id": eid, "age_s": int(time.time() - float(ts))})
    except Exception:  # noqa: BLE001
        log.exception("catalyst_gate: status_summary failed")
    return {
        "enabled": ENABLED,
        "allow_impacts": sorted(ALLOW_IMPACTS),
        "allow_timings": sorted(ALLOW_TIMINGS),
        "postevent_delay_min": POSTEVENT_DELAY_MIN,
        "postevent_window_min": POSTEVENT_WINDOW_MIN,
        "recent_post_alerts": rows,
    }
