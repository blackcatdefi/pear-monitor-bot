"""Round 18.3.4 — Heartbeat every 6h.

Sends a minimal "I'm alive" snapshot to Telegram so BCD has positive
confirmation the bot is up even if no alerts fire. Includes uptime, capital
total, HF flywheel, and basket status.

Kill switch: ``HEARTBEAT_ENABLED=false``.
Cadence: every ``HEARTBEAT_INTERVAL_HOURS`` (default 6).
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

from config import TELEGRAM_CHAT_ID

log = logging.getLogger(__name__)

_PROCESS_START = time.monotonic()


def _is_enabled() -> bool:
    return os.getenv("HEARTBEAT_ENABLED", "true").strip().lower() != "false"


def _uptime_str() -> str:
    sec = int(time.monotonic() - _PROCESS_START)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


async def build_heartbeat() -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "💓 HEARTBEAT — bot alive",
        f"Hora: {now}",
        f"Uptime proceso: {_uptime_str()}",
    ]
    try:
        from modules.portfolio_snapshot import build_portfolio_snapshot
        snap = await build_portfolio_snapshot()
        lines.append(f"Capital: ${snap.capital_total:,.0f}")
        if snap.main_flywheel and snap.main_flywheel.health_factor is not None:
            lines.append(f"HF flywheel: {snap.main_flywheel.health_factor:.3f}")
        try:
            from fund_state import BASKET_V5_STATUS
            lines.append(f"Basket v5: {BASKET_V5_STATUS}")
        except Exception:
            pass
        if snap.market.btc:
            lines.append(f"BTC: ${snap.market.btc:,.0f}")
    except Exception:  # noqa: BLE001
        log.exception("heartbeat: snapshot failed")
        lines.append("⚠️ Snapshot unavailable (heartbeat still OK).")
    lines.append("")
    lines.append("ℹ️ /reporte for full analysis. /errors if there were failures.")
    return "\n".join(lines)


async def send_heartbeat(bot=None) -> int:
    if not _is_enabled() or not TELEGRAM_CHAT_ID or bot is None:
        return 0
    try:
        text = await build_heartbeat()
        from utils.telegram import send_bot_message
        await send_bot_message(bot, TELEGRAM_CHAT_ID, text)
        return 1
    except Exception:  # noqa: BLE001
        log.exception("heartbeat: send failed")
        return 0
