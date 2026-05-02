"""Round 18 — Macro analyst convergence detector.

Three analysts BCD follows:
    - norber       (CriptoNorberBTC channel)
    - lady_market  (theLadyMarket channel)
    - lmec         (lmec_oficial channel)

Every CONVERGENCE_INTERVAL_MIN minutes (default 60), pull recent posts
from each channel via Telethon (last 24h), feed them to the LLM, ask for
a structured directional call (BULL/BEAR/NEUTRAL/NO_CALL + confidence
0-100 + key quote). Persist the snapshot.

Convergence = ≥2 analysts with the same non-NO_CALL direction *and*
average confidence ≥ CONFIDENCE_THRESHOLD. Edge-triggered (no spam).

Telegram alert + /convergence on-demand.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from config import DATA_DIR, TELEGRAM_CHAT_ID

log = logging.getLogger(__name__)

DB_PATH = os.path.join(DATA_DIR, "macro_convergence.db")

ANALYSTS: dict[str, dict[str, str]] = {
    "norber": {"channel": "@criptonorberbtc", "label": "Cripto Norber"},
    "lady_market": {"channel": "@theladymarket", "label": "Lady Market"},
    "lmec": {"channel": "@lmec_oficial", "label": "LMEC"},
}

DIRECTIONS = ("BULL", "BEAR", "NEUTRAL", "NO_CALL")
CONFIDENCE_THRESHOLD = float(os.getenv("MACRO_CONVERGENCE_CONFIDENCE", "70"))


def is_enabled() -> bool:
    return os.getenv("MACRO_CONVERGENCE_ENABLED", "true").strip().lower() != "false"


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute(
        """CREATE TABLE IF NOT EXISTS macro_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            analyst TEXT NOT NULL,
            direction TEXT NOT NULL,
            confidence INTEGER NOT NULL,
            key_quote TEXT,
            sources_json TEXT
        )"""
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS idx_calls_analyst_ts "
        "ON macro_calls(analyst, ts_utc DESC)"
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS convergence_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            direction TEXT NOT NULL,
            avg_confidence REAL NOT NULL,
            analysts_csv TEXT NOT NULL,
            notified INTEGER DEFAULT 0
        )"""
    )
    return c


def _store_call(analyst: str, direction: str, confidence: int, quote: str, sources: list[Any]) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO macro_calls(ts_utc,analyst,direction,confidence,key_quote,sources_json) "
            "VALUES (?,?,?,?,?,?)",
            (
                datetime.now(timezone.utc).isoformat(),
                analyst,
                direction,
                int(confidence),
                quote[:500],
                json.dumps(sources)[:2000],
            ),
        )


def _last_call_for(analyst: str, max_age_hours: int = 24) -> dict[str, Any] | None:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    with _conn() as c:
        row = c.execute(
            "SELECT id,ts_utc,direction,confidence,key_quote FROM macro_calls "
            "WHERE analyst=? AND ts_utc>=? ORDER BY id DESC LIMIT 1",
            (analyst, cutoff),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "ts_utc": row[1],
        "direction": row[2],
        "confidence": int(row[3]),
        "key_quote": row[4],
    }


async def _fetch_channel_messages(channel: str, hours: int = 24) -> list[str]:
    """Best-effort fetch of recent messages from a Telegram channel via Telethon."""
    try:
        from modules.telegram_intel import get_client
    except Exception:
        return []
    try:
        client = await get_client()
    except Exception:
        log.exception("macro_convergence: telethon get_client failed")
        return []
    if client is None:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out: list[str] = []
    try:
        async for msg in client.iter_messages(channel, limit=80):
            if msg is None or not msg.message:
                continue
            mts = msg.date.replace(tzinfo=timezone.utc) if msg.date and not msg.date.tzinfo else msg.date
            if mts and mts < cutoff:
                break
            text = (msg.message or "").strip()
            if text:
                out.append(text)
    except Exception:
        log.exception("macro_convergence: iter_messages failed for %s", channel)
    return out


def _heuristic_direction(messages: list[str]) -> tuple[str, int, str]:
    """Cheap fallback when the LLM is unavailable: keyword tally."""
    if not messages:
        return ("NO_CALL", 0, "")
    bull_kw = ("long", "bull", "alcista", "compra", "rally", "ath", "rebote", "rip", "buy")
    bear_kw = ("short", "bear", "bajista", "venta", "selloff", "dump", "crash", "sell")
    bull = bear = 0
    sample = ""
    for m in messages:
        low = m.lower()
        if any(k in low for k in bull_kw):
            bull += 1
            if not sample:
                sample = m[:200]
        if any(k in low for k in bear_kw):
            bear += 1
            if not sample:
                sample = m[:200]
    if bull == 0 and bear == 0:
        return ("NO_CALL", 0, "")
    if bull >= 2 * bear:
        conf = min(50 + bull * 5, 80)
        return ("BULL", conf, sample)
    if bear >= 2 * bull:
        conf = min(50 + bear * 5, 80)
        return ("BEAR", conf, sample)
    return ("NEUTRAL", 50, sample)


async def _llm_direction(analyst: str, messages: list[str]) -> tuple[str, int, str]:
    """Ask Gemini (cheap router) for a structured direction. Falls back to
    heuristic if LLM is unavailable or returns garbage.
    """
    if not messages:
        return ("NO_CALL", 0, "")
    try:
        from modules.llm_router import route_request
    except Exception:
        return _heuristic_direction(messages)

    snippet = "\n---\n".join(m[:600] for m in messages[:12])
    system_prompt = (
        "Sos un analista financiero. Devolvés EXACTAMENTE un JSON válido con keys "
        "direction, confidence, key_quote. Sin texto fuera del JSON."
    )
    user_msg = (
        f"Analizá estos mensajes del analista {analyst} de las \u00faltimas 24h. "
        "Determiná su direction call sobre BTC en horizonte 24-72h. "
        "Direction debe ser BULL, BEAR, NEUTRAL o NO_CALL. "
        "Confidence integer 0-100 (0 = sin convicción, 100 = total).\n\n"
        f"MENSAJES:\n{snippet}"
    )
    try:
        text, _ = await route_request(
            "macro_convergence", system_prompt, user_msg, max_tokens=300
        )
    except Exception:
        return _heuristic_direction(messages)
    if not text:
        return _heuristic_direction(messages)

    # Extract JSON between first { and last }
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return _heuristic_direction(messages)
    try:
        payload = json.loads(text[start: end + 1])
        direction = (payload.get("direction") or "NO_CALL").upper()
        if direction not in DIRECTIONS:
            direction = "NO_CALL"
        conf = int(payload.get("confidence") or 0)
        quote = str(payload.get("key_quote") or "")[:500]
        return (direction, conf, quote)
    except Exception:
        return _heuristic_direction(messages)


async def parse_recent_calls() -> dict[str, dict[str, Any]]:
    """For each analyst, fetch + classify + persist. Returns a dict keyed by id."""
    out: dict[str, dict[str, Any]] = {}
    for analyst_id, cfg in ANALYSTS.items():
        try:
            messages = await _fetch_channel_messages(cfg["channel"], hours=24)
        except Exception:
            log.exception("macro_convergence: fetch %s failed", analyst_id)
            messages = []
        try:
            direction, confidence, quote = await _llm_direction(analyst_id, messages)
        except Exception:
            log.exception("macro_convergence: classify %s failed", analyst_id)
            direction, confidence, quote = ("NO_CALL", 0, "")
        _store_call(analyst_id, direction, confidence, quote, messages[:5])
        out[analyst_id] = {
            "analyst": analyst_id,
            "label": cfg["label"],
            "direction": direction,
            "confidence": confidence,
            "key_quote": quote,
            "messages_count": len(messages),
        }
    return out


def _record_alert(direction: str, avg_conf: float, analysts_csv: str) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO convergence_alerts(ts_utc,direction,avg_confidence,analysts_csv) "
            "VALUES (?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), direction, avg_conf, analysts_csv),
        )
        return int(cur.lastrowid or 0)


def _last_alert_direction() -> str | None:
    with _conn() as c:
        row = c.execute(
            "SELECT direction FROM convergence_alerts ORDER BY id DESC LIMIT 1",
        ).fetchone()
    return row[0] if row else None


async def detect_convergence(bot=None) -> dict[str, Any] | None:
    """Run a sweep, persist calls, optionally fire convergence alert."""
    if not is_enabled():
        return None
    calls = await parse_recent_calls()

    real_calls = [c for c in calls.values() if c["direction"] not in ("NO_CALL", "NEUTRAL")]
    if len(real_calls) < 2:
        return {"calls": calls, "convergence": False, "reason": "menos de 2 calls reales"}

    directions = [c["direction"] for c in real_calls]
    if len(set(directions)) != 1:
        return {"calls": calls, "convergence": False, "reason": "directions divergentes"}

    direction = directions[0]
    confidences = [c["confidence"] for c in real_calls]
    avg_conf = sum(confidences) / len(confidences)

    if avg_conf < CONFIDENCE_THRESHOLD:
        return {
            "calls": calls,
            "convergence": False,
            "reason": f"avg conf {avg_conf:.0f}<{CONFIDENCE_THRESHOLD:.0f}",
        }

    last_dir = _last_alert_direction()
    if last_dir == direction:
        return {"calls": calls, "convergence": True, "edge": False, "direction": direction}

    analysts_csv = ",".join(c["analyst"] for c in real_calls)
    alert_id = _record_alert(direction, avg_conf, analysts_csv)

    if bot is not None and TELEGRAM_CHAT_ID:
        msg = format_convergence_alert(calls, direction, avg_conf)
        try:
            from utils.telegram import send_bot_message
            await send_bot_message(bot, TELEGRAM_CHAT_ID, msg)
        except Exception:
            log.exception("macro_convergence: alert send failed")

    return {
        "calls": calls,
        "convergence": True,
        "edge": True,
        "alert_id": alert_id,
        "direction": direction,
        "avg_confidence": avg_conf,
    }


def format_convergence_alert(calls: dict[str, dict[str, Any]], direction: str, avg_conf: float) -> str:
    lines: list[str] = [
        "\U0001f3af CONVERGENCIA MACRO DETECTADA",
        f"Direction: {direction}",
        f"Avg confidence: {avg_conf:.0f}%",
        "",
        "Analistas alineados:",
    ]
    for c in calls.values():
        if c["direction"] == direction:
            lines.append(f"  \u2022 {c['label']}: {c['direction']} ({c['confidence']}%)")
            quote = (c.get("key_quote") or "").strip()
            if quote:
                lines.append(f"    '{quote[:200]}'")
    lines.append("")
    lines.append("\U0001f4a1 Convergence = high conviction. Consider adjusting exposure.")
    return "\n".join(lines)


def format_status() -> str:
    lines: list[str] = [
        "\U0001f3af MACRO CONVERGENCE STACK",
        "\u2500" * 30,
    ]
    for analyst_id, cfg in ANALYSTS.items():
        last = _last_call_for(analyst_id, max_age_hours=48)
        if not last:
            lines.append(f"  \u2022 {cfg['label']}: sin lecturas recientes")
            continue
        lines.append(
            f"  \u2022 {cfg['label']}: {last['direction']} "
            f"({last['confidence']}%) — {last['ts_utc'][:16]}"
        )
        quote = (last.get("key_quote") or "").strip()
        if quote:
            lines.append(f"      '{quote[:160]}'")
    last_alert = _last_alert_direction()
    if last_alert:
        lines.append("")
        lines.append(f"\u00daltima convergencia disparada: {last_alert}")
    lines.append("")
    lines.append("Toggle: MACRO_CONVERGENCE_ENABLED env var.")
    return "\n".join(lines)


async def scheduled_check(application=None) -> None:
    if not is_enabled():
        return
    bot = application.bot if application is not None else None
    try:
        await detect_convergence(bot=bot)
    except Exception:
        log.exception("macro_convergence scheduled_check failed")
