"""R-REPORTE-LIVE (2026-06-03) — freshness guard for /reporte.

GOVERNING RULE: any data point older than ``REPORT_FRESHNESS_MAX_AGE_H`` (6h
default) must NOT be presented as current/live state. A fetch that fell back
to cache, or carries an ``age_seconds`` / ``last_known_at_iso`` past the
window, is annotated as STALE so the analysis layer never reports it as the
live number.

This module is intentionally tiny and pure (no I/O, NEVER raises) so it can
run inside the report path and be unit-tested deterministically.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any


def max_age_seconds() -> float:
    try:
        hours = float(os.getenv("REPORT_FRESHNESS_MAX_AGE_H", "6") or 6)
    except (TypeError, ValueError):
        hours = 6.0
    return hours * 3600.0


def _parse_iso(ts: Any) -> datetime | None:
    if not ts or not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def age_seconds_of(entry: dict[str, Any], now: datetime | None = None) -> float | None:
    """Best-effort age (seconds) of a data dict from common timestamp fields.

    Looks at ``age_seconds`` first (already-computed by readers), then
    ``last_known_at_iso`` / ``timestamp_utc`` / ``timestamp`` / ``fetched_at``.
    Returns None when no timestamp is present.
    """
    if not isinstance(entry, dict):
        return None
    direct = entry.get("age_seconds")
    if direct is not None:
        try:
            return float(direct)
        except (TypeError, ValueError):
            pass
    now = now or datetime.now(timezone.utc)
    for key in ("last_known_at_iso", "timestamp_utc", "timestamp", "fetched_at", "as_of"):
        dt = _parse_iso(entry.get(key))
        if dt is not None:
            return (now - dt).total_seconds()
    return None


def is_stale(entry: dict[str, Any], now: datetime | None = None) -> bool:
    """True if the entry is provably older than the freshness window."""
    age = age_seconds_of(entry, now=now)
    if age is None:
        return False
    return age > max_age_seconds()


def annotate_portfolio_freshness(
    portfolio: list[dict[str, Any]] | None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return a shallow copy of the portfolio with STALE wallets annotated.

    A wallet that fell back to cache (``stale=True``) or whose data is past the
    6h window is tagged so the LLM/raw-data blob never treats it as live:
    ``_freshness="STALE — cache fallback >6h, NO usar como estado actual"``.
    Fresh wallets are returned unchanged. NEVER raises.
    """
    if not portfolio:
        return portfolio or []
    out: list[dict[str, Any]] = []
    for w in portfolio:
        if not isinstance(w, dict):
            out.append(w)
            continue
        ww = dict(w)
        data = ww.get("data")
        stale_flag = bool(ww.get("stale"))
        data_stale = isinstance(data, dict) and is_stale(data, now=now)
        if ww.get("status") == "ok" and (stale_flag or data_stale):
            if isinstance(data, dict):
                data = dict(data)
                reason = ww.get("stale_reason") or "age>6h"
                data["_freshness"] = (
                    f"STALE — {reason}, dato NO live (>6h o cache fallback); "
                    "no reportar como estado actual"
                )
                ww["data"] = data
            ww["_stale_data"] = True
        out.append(ww)
    return out
