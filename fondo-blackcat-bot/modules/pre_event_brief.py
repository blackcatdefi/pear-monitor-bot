"""Round 18 — T-1h pre-event brief dispatcher.

Watches the macro calendar via catalyst_scoring.upcoming_high_impact() and
when a high-impact event (score >= 7) is between T-90min and T-30min, sends
a structured pre-event brief to the fund chat:

    - Event name, exact ETA, score
    - Current fund snapshot (capital, HF, active baskets, cycle trade)
    - At-risk positions (those tagged in event.affects_positions)
    - Bull/Bear/Base scenarios (placeholder text — BCD fills via /tesis)
    - Pre-event checklist

Edge-triggered: a tiny SQLite log records (event_id, ts) so the same event
is never briefed twice — even across bot restarts. We dispatch once per
event when the T-90min window opens, then quiet until the next event.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from config import DATA_DIR, TELEGRAM_CHAT_ID

log = logging.getLogger(__name__)

DB_PATH = os.path.join(DATA_DIR, "pre_event_brief.db")

WINDOW_OPEN_MIN = float(os.getenv("PRE_EVENT_WINDOW_OPEN_MIN", "90"))
WINDOW_CLOSE_MIN = float(os.getenv("PRE_EVENT_WINDOW_CLOSE_MIN", "30"))
MIN_SCORE = int(os.getenv("PRE_EVENT_MIN_SCORE", "7"))


def is_enabled() -> bool:
    return os.getenv("PRE_EVENT_BRIEF_ENABLED", "true").strip().lower() != "false"


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute(
        """CREATE TABLE IF NOT EXISTS dispatched (
            event_id INTEGER PRIMARY KEY,
            event_name TEXT,
            event_ts_utc TEXT,
            dispatched_ts_utc TEXT NOT NULL,
            score INTEGER
        )"""
    )
    return c


def _already_dispatched(event_id: int) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM dispatched WHERE event_id=? LIMIT 1", (event_id,)
        ).fetchone()
    return bool(row)


def _record_dispatch(event_id: int, name: str, event_ts: datetime, score: int) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO dispatched(event_id,event_name,event_ts_utc,"
            "dispatched_ts_utc,score) VALUES (?,?,?,?,?)",
            (
                event_id,
                name,
                event_ts.isoformat(),
                datetime.now(timezone.utc).isoformat(),
                score,
            ),
        )


async def _fund_snapshot_block() -> str:
    """Compact snapshot — capital, HF, baskets, cycle trade. Best-effort."""
    lines: list[str] = []
    try:
        from modules.portfolio import fetch_all_wallets
        wallets = await fetch_all_wallets()
        total_equity = 0.0
        for w in wallets or []:
            if not isinstance(w, dict) or w.get("status") != "ok":
                continue
            d = w.get("data") or {}
            eq = d.get("equity") or d.get("account_value") or d.get("total_value") or 0
            try:
                total_equity += float(eq or 0)
            except (TypeError, ValueError):
                pass
        if total_equity > 0:
            lines.append(f"  Capital total: ${total_equity:,.2f}")
    except Exception:
        log.exception("pre_event_brief: portfolio snapshot failed")

    try:
        from modules.hyperlend import fetch_all_hyperlend
        hl = await fetch_all_hyperlend()
        if isinstance(hl, list):
            hfs: list[float] = []
            for e in hl:
                if not isinstance(e, dict):
                    continue
                hf = e.get("hf") or e.get("health_factor")
                if isinstance(hf, (int, float)) and 0 < hf < 1000:
                    hfs.append(float(hf))
            if hfs:
                lines.append(
                    f"  HF flywheel: min={min(hfs):.3f} · max={max(hfs):.3f}"
                )
    except Exception:
        log.exception("pre_event_brief: HF snapshot failed")

    try:
        from fund_state import BASKET_V5_STATUS, TRADE_DEL_CICLO_STATUS
        v5 = (BASKET_V5_STATUS or "IDLE").upper()
        cyc = (TRADE_DEL_CICLO_STATUS or "CLOSED").upper()
        lines.append(f"  Basket V5: {v5} · Trade Ciclo: {cyc}")
    except Exception:
        pass

    if not lines:
        return "  (snapshot unavailable)"
    return "\n".join(lines)


def _at_risk_block(event: Any) -> str:
    affects = getattr(event, "affects_positions", None) or []
    if not affects:
        return "  (no positions flagged)"
    return "\n".join(f"  • {a}" for a in affects)


def _scenario_block(event: Any) -> str:
    """Generic placeholder scenarios — BCD layers thesis via /tesis manually.
    Kept neutral on purpose; the bot does not generate trade calls."""
    cat = (getattr(event, "category", "") or "").lower()
    if cat == "fomc":
        return (
            "  Bull: dovish cut → risk-on, bid HYPE/BTC, basket shorts hurt\n"
            "  Bear: hawkish hold/hike → risk-off, basket shorts comfort\n"
            "  Base: in-line decision → low vol, micro-rotation only"
        )
    if cat == "earnings":
        return (
            "  Bull: beat + raise → risk-on cascade in basket altcoins\n"
            "  Bear: miss + cut → liquidations in HYPE/major beta\n"
            "  Base: in-line → basket spreads with no directional change"
        )
    if cat == "geopolitical":
        return (
            "  Bull: de-escalation → risk-on, USD weak, BTC bid\n"
            "  Bear: escalation → flight-to-quality, BTC dump short term\n"
            "  Base: status quo → vol exhausted, basket ranges"
        )
    if cat == "unlock" or cat == "tge":
        return (
            "  Bull: float absorbed without spot dump\n"
            "  Bear: dump at unlock → asset falls 5-15%\n"
            "  Base: multi-day chop before recovery"
        )
    return (
        "  Bull / Bear / Base — define based on context\n"
        "  Recommended: review /tesis and /reporte before the event."
    )


def _checklist_block(event: Any) -> str:
    return (
        "  □ HF flywheel >= 1.20 with buffer\n"
        "  □ /reporte fresh (last <30min)\n"
        "  □ /tesis reviewed vs catalyst\n"
        "  □ Stops updated on affected positions\n"
        "  □ Liq buffer in HyperLend confirmed (/liqcalc)"
    )


def build_pre_event_brief(event: Any, score: int, snapshot: str) -> str:
    name = getattr(event, "name", "(event)")
    ts = getattr(event, "timestamp_utc", datetime.now(timezone.utc))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta_min = int((ts - datetime.now(timezone.utc)).total_seconds() / 60)
    cat = getattr(event, "category", "?")
    impact = getattr(event, "impact_level", "?")
    eta = ts.strftime("%Y-%m-%d %H:%M UTC")
    return (
        "\u23f0 PRE-EVENT BRIEF (T-{0}min)\n"
        "{1}\n"
        "\U0001f4e2 {2}\n"
        "Cat: {3} · Impact: {4} · Score: {5}/10 · ETA: {6}\n\n"
        "FUND SNAPSHOT:\n{7}\n\n"
        "AT-RISK POSITIONS:\n{8}\n\n"
        "SCENARIOS:\n{9}\n\n"
        "PRE-CHECK:\n{10}"
    ).format(
        delta_min,
        "\u2500" * 30,
        name,
        cat,
        impact,
        score,
        eta,
        snapshot,
        _at_risk_block(event),
        _scenario_block(event),
        _checklist_block(event),
    )


async def check_and_dispatch(bot=None) -> list[dict[str, Any]]:
    """Main entry called by scheduler. Returns list of dispatched events."""
    if not is_enabled():
        return []
    try:
        from modules.catalyst_scoring import upcoming_high_impact
        candidates = await upcoming_high_impact(
            min_score=MIN_SCORE, hours_ahead=int(WINDOW_OPEN_MIN / 60) + 1
        )
    except Exception:
        log.exception("pre_event_brief: scoring lookup failed")
        return []

    if not candidates:
        return []

    now = datetime.now(timezone.utc)
    snapshot_built = False
    snapshot = ""
    dispatched: list[dict[str, Any]] = []

    for event, score in candidates:
        ev_id = getattr(event, "id", None)
        if ev_id is None:
            continue
        ts = getattr(event, "timestamp_utc", None)
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta_min = (ts - now).total_seconds() / 60.0
        if delta_min < WINDOW_CLOSE_MIN or delta_min > WINDOW_OPEN_MIN:
            continue
        if _already_dispatched(int(ev_id)):
            continue

        if not snapshot_built:
            snapshot = await _fund_snapshot_block()
            snapshot_built = True

        text = build_pre_event_brief(event, score, snapshot)

        if bot is not None and TELEGRAM_CHAT_ID:
            try:
                from utils.telegram import send_bot_message
                await send_bot_message(bot, TELEGRAM_CHAT_ID, text)
            except Exception:
                log.exception("pre_event_brief: send failed for %s", ev_id)
                continue

        _record_dispatch(int(ev_id), getattr(event, "name", "?"), ts, score)
        dispatched.append(
            {
                "event_id": ev_id,
                "name": getattr(event, "name", "?"),
                "score": score,
                "minutes_to": delta_min,
            }
        )

    return dispatched


async def scheduled_check(application=None) -> None:
    bot = application.bot if application is not None else None
    try:
        await check_and_dispatch(bot=bot)
    except Exception:
        log.exception("pre_event_brief scheduled_check failed")
