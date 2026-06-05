"""LMEC state persistence — R-BOT-LMEC-AUTOFEED (2026-05-07).

Persistent JSON state at ``$DATA_DIR/lmec_state.json`` that:

1. Auto-tracks how many ISO weeks consecutively BTC has closed above /
   below the 50-week MA (Leg 4 of the LMEC bear-invalidation triggers).
2. Persists the last-known leg snapshot so the weekly scheduler can
   detect "flips" (a leg moving from INVALIDA/NEUTRO/UNKNOWN → VALIDA)
   and emit a critical alert exactly once per flip.
3. Tracks consecutive TraderMap scrape failures so the LMEC evaluator
   can self-heal back to the LMEC_* env vars when the scraper is
   misbehaving (and surface a warning banner in /reporte).

Schema
------
{
    "ma50w_consecutive_weeks": int,      # 0 if BTC currently above MA50w
    "ma50w_first_break_iso": str|None,   # ISO timestamp when last streak started
    "last_iso_week": str,                # "2026-W19" — to detect new-week ticks
    "last_btc_below_ma": bool,           # True if BTC was BELOW MA on last update
    "last_check_iso": str,
    "last_legs": list[dict],             # [{id, status, detail}, ...]
    "tradermap_failure_streak": int,     # consecutive scrape failures
    "last_flip_iso": str|None,           # last leg flip event detected
    "last_flip_legs": list[str],         # ids of legs flipped on last_flip_iso
}

Public API
----------
* ``load() -> dict``                 — read state, never raises
* ``save(state) -> None``            — atomic write, never raises
* ``update_weeks_counter(price, ma50w, *, now_iso=None) -> dict``
    Auto-managed counter. Returns the post-update state.
* ``record_legs_snapshot(conditions) -> dict``
    Persists the current leg statuses, returns flip events
    (legs that just became VALIDA).
* ``record_tradermap_outcome(success: bool) -> int``
    Increment / reset failure streak, returns post-update value.
* ``status_summary() -> dict``       — for /lmec_status command
* ``LMEC_TRADERMAP_FAILURE_THRESHOLD`` = 3 (configurable env var)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# Configurable thresholds (env vars)
def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    if not str(raw).strip():
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


# When tradermap scraper fails this many times in a row, evaluate_lmec_triggers
# falls back to LMEC_* env vars and surfaces a warning banner.
LMEC_TRADERMAP_FAILURE_THRESHOLD = _env_int(
    "LMEC_TRADERMAP_FAILURE_THRESHOLD", 3
)
LMEC_MA50W_BROKEN_THRESHOLD_WEEKS = _env_int(
    "LMEC_MA50W_BROKEN_THRESHOLD_WEEKS", 2
)


def _path() -> str:
    try:
        from config import DATA_DIR  # type: ignore

        base = DATA_DIR
    except Exception:  # noqa: BLE001
        base = os.path.join(os.path.dirname(__file__), "..", "data")
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:  # noqa: BLE001
        log.warning("lmec_state: makedirs(%s) failed", base)
    return os.path.join(base, "lmec_state.json")


def _empty_state() -> dict[str, Any]:
    return {
        "ma50w_consecutive_weeks": 0,
        "ma50w_first_break_iso": None,
        "last_iso_week": "",
        "last_btc_below_ma": False,
        "last_check_iso": "",
        "last_legs": [],
        "tradermap_failure_streak": 0,
        "last_flip_iso": None,
        "last_flip_legs": [],
        # P1.9: BCD's manually-entered TradingView inputs (MACD weekly /
        # RSI weekly / MA50w), persisted on the Volume via /setlmec so they
        # survive restarts without an env-var redeploy. None = awaiting input.
        "manual_inputs": {},
    }


# P1.9 — manual LMEC input persistence (set via /setlmec). These are the
# weekly-TA values BCD reads off TradingView; the bot has no first-class TA
# feed, so they're entered by hand and stored here as an override layer.
_MANUAL_KEYS = ("macd_weekly_positive", "rsi_weekly", "ma50w_usd")


def get_manual_inputs() -> dict[str, Any]:
    """Return the persisted manual LMEC inputs (possibly empty). Never raises."""
    try:
        mi = load().get("manual_inputs") or {}
        return {k: mi[k] for k in _MANUAL_KEYS if k in mi and mi[k] is not None}
    except Exception:  # noqa: BLE001
        return {}


def set_manual_input(key: str, value: Any) -> dict[str, Any]:
    """Persist one manual LMEC input. ``value=None`` clears it. Returns the
    updated manual-inputs dict. Never raises."""
    key = (key or "").strip().lower()
    if key not in _MANUAL_KEYS:
        raise ValueError(f"unknown LMEC input {key!r}; valid: {', '.join(_MANUAL_KEYS)}")
    state = load()
    mi = dict(state.get("manual_inputs") or {})
    if value is None:
        mi.pop(key, None)
    else:
        mi[key] = value
    state["manual_inputs"] = mi
    save(state)
    return mi


def load() -> dict[str, Any]:
    """Read persisted state, never raises (returns empty defaults on failure)."""
    p = _path()
    if not os.path.isfile(p):
        return _empty_state()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("lmec_state.json is not a dict")
            # Fill in any missing keys (forward-compat).
            base = _empty_state()
            base.update({k: v for k, v in data.items() if k in base})
            return base
    except Exception:  # noqa: BLE001
        log.exception("lmec_state: read failed, returning empty defaults")
        return _empty_state()


def save(state: dict[str, Any]) -> None:
    """Atomic write of state. Never raises."""
    p = _path()
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp, p)
    except Exception:  # noqa: BLE001
        log.exception("lmec_state: write failed (non-fatal)")


def _iso_week(now: datetime) -> str:
    iso = now.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def update_weeks_counter(
    btc_price: float | None,
    ma50w: float | None,
    *,
    now: datetime | None = None,
    state: dict[str, Any] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Update Leg-4 weeks-broken counter. Auto-managed.

    Logic
    -----
    * If we don't have both inputs (price + ma50w): no-op, return current state.
    * If BTC < MA50w (BCD's "broken" interpretation differs — see note below):
        - If state already says we're below and we're in the same ISO week:
          no change.
        - If we are in a NEW ISO week (or this is the first time): increment
          consecutive_weeks. Set ma50w_first_break_iso if streak starts.
    * If BTC >= MA50w: reset the streak to 0 and clear the first-break marker.

    Note on direction
    -----------------
    The LMEC Leg-4 spec is "MA50w broken with sustained force 2-3 weeks".
    Bear thesis is INVALIDATED when BTC reclaims MA50w sustainedly. So
    "broken" here is a direction-agnostic streak — we track consecutive
    weeks where BTC is on the BULL side (above MA50w). The flip-to-VALIDA
    happens when consecutive_weeks ≥ LMEC_MA50W_BROKEN_THRESHOLD_WEEKS
    AND price > MA50w. The legacy LMEC_MA50W_BROKEN_WEEKS env var is still
    honoured by lmec_triggers as a manual override when this counter is
    not yet warm.
    """
    now = now or datetime.now(timezone.utc)
    s = state if state is not None else load()
    if btc_price is None or ma50w is None:
        s["last_check_iso"] = now.isoformat()
        if persist:
            save(s)
        return s

    cur_week = _iso_week(now)
    above_ma = btc_price >= float(ma50w)

    if above_ma:
        # Streak runs over weeks where BTC is ABOVE MA50w.
        if cur_week == s.get("last_iso_week"):
            # Same week — no-op (don't double-count this week).
            pass
        else:
            if not s.get("last_btc_below_ma", False) and s.get(
                "ma50w_consecutive_weeks", 0
            ) > 0:
                # Continuing an existing streak across weeks.
                s["ma50w_consecutive_weeks"] = (
                    int(s.get("ma50w_consecutive_weeks", 0)) + 1
                )
            else:
                # New streak: reset and start at 1.
                s["ma50w_consecutive_weeks"] = 1
                s["ma50w_first_break_iso"] = now.isoformat()
        s["last_btc_below_ma"] = False
    else:
        # Below MA — reset the bull-side streak.
        s["ma50w_consecutive_weeks"] = 0
        s["ma50w_first_break_iso"] = None
        s["last_btc_below_ma"] = True

    s["last_iso_week"] = cur_week
    s["last_check_iso"] = now.isoformat()
    if persist:
        save(s)
    return s


def record_legs_snapshot(
    conditions: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    state: dict[str, Any] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Persist the current leg snapshot, return diff = newly VALIDA legs.

    Returns a dict::

        {
            "flips": [<id>, ...],          # legs that just became VALIDA
            "previous": [<dict>, ...],     # legs as we last saw them
            "current": [<dict>, ...],      # the new snapshot
            "state": <updated state>,
        }
    """
    now = now or datetime.now(timezone.utc)
    s = state if state is not None else load()

    prev_by_id: dict[str, str] = {}
    for c in s.get("last_legs") or []:
        if isinstance(c, dict):
            cid = c.get("id")
            cs = c.get("status")
            if isinstance(cid, str):
                prev_by_id[cid] = str(cs or "UNKNOWN")

    flips: list[str] = []
    cur_legs: list[dict[str, Any]] = []
    for c in conditions or []:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id", ""))
        if not cid:
            continue
        cs = str(c.get("status", "UNKNOWN"))
        cd = str(c.get("detail", ""))
        cur_legs.append({"id": cid, "status": cs, "detail": cd})
        prev = prev_by_id.get(cid)
        if prev is not None and prev != "VALIDA" and cs == "VALIDA":
            flips.append(cid)

    s["last_legs"] = cur_legs
    if flips:
        s["last_flip_iso"] = now.isoformat()
        s["last_flip_legs"] = flips
    if persist:
        save(s)
    return {
        "flips": flips,
        "previous": [{"id": k, "status": v} for k, v in prev_by_id.items()],
        "current": cur_legs,
        "state": s,
    }


def record_tradermap_outcome(
    success: bool,
    *,
    state: dict[str, Any] | None = None,
    persist: bool = True,
) -> int:
    """Track consecutive TraderMap scrape failures; return current streak."""
    s = state if state is not None else load()
    if success:
        s["tradermap_failure_streak"] = 0
    else:
        s["tradermap_failure_streak"] = (
            int(s.get("tradermap_failure_streak", 0)) + 1
        )
    if persist:
        save(s)
    return int(s["tradermap_failure_streak"])


def is_tradermap_unhealthy(state: dict[str, Any] | None = None) -> bool:
    """Return True if scraper has failed ≥ threshold times in a row."""
    s = state if state is not None else load()
    return int(s.get("tradermap_failure_streak", 0)) >= LMEC_TRADERMAP_FAILURE_THRESHOLD


def status_summary() -> dict[str, Any]:
    """Compact dict for /lmec_status command."""
    s = load()
    return {
        "path": _path(),
        "ma50w_consecutive_weeks": int(s.get("ma50w_consecutive_weeks", 0)),
        "ma50w_first_break_iso": s.get("ma50w_first_break_iso"),
        "last_iso_week": s.get("last_iso_week", ""),
        "last_btc_below_ma": bool(s.get("last_btc_below_ma", False)),
        "last_check_iso": s.get("last_check_iso", ""),
        "last_legs": s.get("last_legs", []),
        "tradermap_failure_streak": int(s.get("tradermap_failure_streak", 0)),
        "tradermap_unhealthy": is_tradermap_unhealthy(s),
        "last_flip_iso": s.get("last_flip_iso"),
        "last_flip_legs": s.get("last_flip_legs", []),
        "thresholds": {
            "ma50w_broken_weeks": LMEC_MA50W_BROKEN_THRESHOLD_WEEKS,
            "tradermap_failure": LMEC_TRADERMAP_FAILURE_THRESHOLD,
        },
    }
