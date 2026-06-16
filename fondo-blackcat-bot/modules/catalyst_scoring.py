"""Round 18 — Catalyst proximity scoring.

Score each upcoming macro_calendar event 0-10 based on:
    - base impact (event category)
    - whether the fund currently holds positions affected by the event
    - manual override stored in macro_events.notes (TBD; for now uses the
      impact_level that already exists)

Used by:
    - Enhanced /calendar output (replace cal_format with format_calendar_with_scoring)
    - pre_event_brief.py to identify T-1h dispatch candidates (score >= 7)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from modules import macro_calendar as cal

log = logging.getLogger(__name__)

BASE_SCORES = {
    "fomc": 9,
    "earnings": 7,
    "unlock": 6,
    "tge": 5,
    "geopolitical": 8,
    "crypto": 5,
    "other": 3,
}

IMPACT_BOOST = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "critical": 3,
}

POSITION_BOOST_PER_MATCH = 1
POSITION_BOOST_CAP = 3


def is_enabled() -> bool:
    return os.getenv("CATALYST_SCORING_ENABLED", "true").strip().lower() != "false"


def _humanize_until(event_time: datetime) -> str:
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=timezone.utc)
    delta = event_time - datetime.now(timezone.utc)
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


def calculate_impact_score(
    event: cal.MacroEvent,
    active_position_keys: set[str] | None = None,
) -> int:
    base = BASE_SCORES.get(event.category, 3)
    base += IMPACT_BOOST.get((event.impact_level or "").lower(), 0)
    if active_position_keys and event.affects_positions:
        matches = sum(
            1 for ap in event.affects_positions if ap and ap in active_position_keys
        )
        base += min(matches * POSITION_BOOST_PER_MATCH, POSITION_BOOST_CAP)
    return min(max(base, 0), 10)


def _score_emoji(score: int) -> str:
    if score >= 8:
        return "\U0001f534"
    if score >= 5:
        return "\U0001f7e1"
    return "\u26aa"


async def _active_position_keys() -> set[str]:
    keys: set[str] = set()
    try:
        from fund_state import BASKET_STATUS, BASKET_V5_STATUS
        if BASKET_STATUS.get("active"):
            keys.add("basket_v5")
        v5_status = (BASKET_V5_STATUS or "").upper()
        if v5_status in ("ACTIVE", "DEPLOYING", "PENDING_CAPITAL"):
            keys.add("basket_v5")
    except Exception:
        pass

    # R-BOT-DEFINITIVE-KILLCLEAN (2026-06-15): the HyperLend flywheel lookup was
    # removed (dead protocol). The live HYPE-collateral exposure is the native
    # Portfolio Margin — key on it via compute_pm_state, never HyperLend.
    try:
        from modules.portfolio import fetch_all_wallets
        from modules.pm_context import select_primary_pm_state
        wallets = await fetch_all_wallets()
        pm = select_primary_pm_state(wallets, None)
        if pm is not None and pm.has_data and pm.collateral_usd > 0:
            keys.add("portfolio_margin")
            # Back-compat: existing catalyst events may tag the HYPE-collateral
            # core exposure as "flywheel" (the word now describes the live PM
            # mechanic, not the dead HyperLend pair-trade).
            keys.add("flywheel")
    except Exception:
        log.exception("catalyst_scoring: PM state lookup failed")

    # R-NOPRELIQ + REMOVE BLOFIN (2026-05-15): Trade del Ciclo (Blofin) ELIMINADO.

    return keys


async def format_calendar_with_scoring(limit: int = 12) -> str:
    if not is_enabled():
        return cal.format_calendar(limit=limit)
    events = cal.upcoming_events(limit=limit)
    if not events:
        return (
            "\U0001f5d3 UPCOMING CATALYSTS\n"
            "(calendar empty — use /add_event)"
        )
    keys = await _active_position_keys()
    rows: list[tuple[int, str]] = []
    for ev in events:
        score = calculate_impact_score(ev, active_position_keys=keys)
        emoji = _score_emoji(score)
        ts = ev.timestamp_utc
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        when = ts.strftime("%Y-%m-%d %H:%M UTC")
        until = _humanize_until(ts)
        line = (
            f"{emoji} [{score}/10] {ev.name}\n"
            f"   {when} · in {until} · {ev.category} ({ev.impact_level})"
        )
        if ev.affects_positions:
            relevant = [a for a in ev.affects_positions if a in keys]
            if relevant:
                line += f"\n   \u26a0\ufe0f Affects: {', '.join(relevant)}"
        rows.append((score, line))

    rows.sort(key=lambda r: (-r[0],))
    out: list[str] = ["\U0001f5d3 UPCOMING CATALYSTS (with scoring)", "\u2500" * 30]
    for _score, line in rows:
        out.append(line)
        out.append("")
    return "\n".join(out).rstrip()


async def upcoming_high_impact(min_score: int = 7, hours_ahead: int = 36) -> list[tuple[cal.MacroEvent, int]]:
    """Return events scheduled within the next `hours_ahead` whose score
    meets `min_score` (used by pre_event_brief)."""
    events = cal.upcoming_events(limit=30)
    keys = await _active_position_keys()
    out: list[tuple[cal.MacroEvent, int]] = []
    now = datetime.now(timezone.utc)
    for ev in events:
        ts = ev.timestamp_utc
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if (ts - now).total_seconds() > hours_ahead * 3600:
            continue
        score = calculate_impact_score(ev, active_position_keys=keys)
        if score >= min_score:
            out.append((ev, score))
    return out
