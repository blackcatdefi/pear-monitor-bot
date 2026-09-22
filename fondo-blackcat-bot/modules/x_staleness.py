"""R-X-FLIP (2026-09-22) — honest age for the X timeline.

WHY THIS EXISTS
---------------
On 22-sep the /reporte X section printed posts from 26-aug under the header
"X Timeline (last 48h)". The header was a hardcoded string: it described the
window the bot INTENDED to show, never the data it actually had. A reader had
no way to tell a live 48h feed from a 27-day-old corpse, so a dead feed looked
healthy for 27 days.

THE RULE: the header is computed from the newest post in the payload, never
asserted. Anything older than the window renders as a DEGRADED banner carrying
the real age. Pure stdlib, zero repo imports — every renderer (templates,
bot.py, the cache banner) shares one implementation and cannot drift.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# A timeline is degraded the moment its newest post falls outside the window
# it claims to cover. No grace period: "48h" has to mean 48h.
DEFAULT_WINDOW_HOURS = 48


def _parse_ts(raw: Any) -> datetime | None:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iter_tweets(payload: Any):
    """Yield tweet dicts from either payload shape (flat list or by_user)."""
    if not isinstance(payload, dict):
        return
    flat = payload.get("tweets")
    if isinstance(flat, list) and flat:
        for t in flat:
            if isinstance(t, dict):
                yield t
        return
    data = payload.get("data")
    if isinstance(data, dict):
        for tweets in data.values():
            for t in tweets or []:
                if isinstance(t, dict):
                    yield t


def newest_created_at(payload: Any) -> datetime | None:
    """UTC timestamp of the most recent post in the payload, or None."""
    newest: datetime | None = None
    for t in _iter_tweets(payload):
        ts = _parse_ts(t.get("created_at"))
        if ts is not None and (newest is None or ts > newest):
            newest = ts
    return newest


def age_hours(payload: Any, now: datetime | None = None) -> float | None:
    """Hours since the newest post. None when the payload carries no post."""
    newest = newest_created_at(payload)
    if newest is None:
        return None
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    return max((ref - newest).total_seconds() / 3600.0, 0.0)


def format_age(hours: float | None) -> str:
    """'3h 12min' / '27d 5h' — the real age, never rounded down to a lie."""
    if hours is None:
        return "n/d"
    try:
        h = float(hours)
    except (TypeError, ValueError):
        return "n/d"
    if h < 1:
        return f"{int(h * 60)}min"
    if h < 48:
        return f"{int(h)}h {int((h % 1) * 60)}min"
    days = int(h // 24)
    return f"{days}d {int(h % 24)}h"


def staleness(payload: Any, window_hours: int | None = None,
              now: datetime | None = None) -> dict[str, Any]:
    """Age verdict for a timeline payload.

    Returns ``degraded`` True when the newest post is outside the claimed
    window — and ALSO when the payload has no posts at all, because "nothing
    to show" must never be rendered as a healthy 48h feed either.
    """
    window = int(window_hours or (payload.get("hours")
                                  if isinstance(payload, dict) else None)
                 or DEFAULT_WINDOW_HOURS)
    h = age_hours(payload, now=now)
    empty = h is None
    return {
        "age_hours": h,
        "age_text": format_age(h),
        "window_hours": window,
        "empty": empty,
        "degraded": empty or h > window,
    }


def header_line(payload: Any, now_text: str, window_hours: int | None = None,
                now: datetime | None = None) -> str:
    """The one header both /timeline and /reporte print.

    Fresh  → "🐦 X Timeline (last 48h) — <now>"
    Stale  → "⚠️ X Timeline DEGRADADO — el post mas nuevo es de hace 27d 5h
              (ventana 48h) — <now>"
    """
    st = staleness(payload, window_hours=window_hours, now=now)
    window = st["window_hours"]
    if not st["degraded"]:
        return f"\U0001f426 X Timeline (last {window}h) — {now_text}"
    if st["empty"]:
        return (
            f"\u26a0\ufe0f X Timeline DEGRADADO — sin posts en la ventana "
            f"{window}h (no hay dato fresco que mostrar) — {now_text}"
        )
    return (
        f"\u26a0\ufe0f X Timeline DEGRADADO — el post mas nuevo es de hace "
        f"{st['age_text']} (ventana {window}h, NO son las ultimas {window}h) "
        f"— {now_text}"
    )
