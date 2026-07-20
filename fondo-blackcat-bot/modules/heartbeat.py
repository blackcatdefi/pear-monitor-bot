"""R-SIGNAL-DIET (2026-07-20) — heartbeat es ON-DEMAND, nunca push.

Antes (Round 18.3.4) este módulo empujaba un "I'm alive" cada 6h a Telegram.
Ese push scheduler fue ELIMINADO: era ruido que ahogaba las alertas reales.
``build_heartbeat()`` queda como builder del snapshot (uptime, capital, HF,
BTC) y lo consume el comando on-demand ``/health`` en bot.py. NO existe más
``send_heartbeat`` ni ningún job programado que use este módulo.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_PROCESS_START = time.monotonic()


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
        "💓 /health — bot alive (on-demand)",
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


# R-SIGNAL-DIET: ``send_heartbeat`` ELIMINADO — este módulo no empuja nada.
