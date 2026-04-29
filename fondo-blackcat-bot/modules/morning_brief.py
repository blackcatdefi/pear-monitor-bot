"""Round 18 — Morning brief.

Daily 11:00 UTC (8am Argentina) summary that BCD doesn't have to ask for.

Combines:
    - Fund snapshot (capital, HF flywheel, basket activa, UPnL)
    - Overnight changes (12h price/F&G deltas)
    - Today's catalyst events (next 24h from macro_calendar)
    - Recent macro analyst updates (last 24h from intel_memory)
    - Active alerts count

Triggered by APScheduler cron (hour=MORNING_BRIEF_HOUR_UTC, minute=0) and
on-demand via /brief.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _safe_call(fn, *args, default=None, **kwargs):
    try:
        return await fn(*args, **kwargs)
    except Exception:
        log.exception("morning_brief: call failed: %s", getattr(fn, "__name__", "?"))
        return default


async def _fetch_overnight_market() -> dict[str, Any]:
    """Compute 12h change for BTC/ETH/HYPE + F&G snapshot."""
    from modules import market as mkt
    try:
        data = await mkt.fetch_market_data()
    except Exception:
        return {}
    out: dict[str, Any] = {"prices": {}, "fng": None, "fng_change": None}
    prices = (data or {}).get("prices") or {}
    for sym in ("BTC", "ETH", "HYPE"):
        block = prices.get(sym) or {}
        out["prices"][sym] = {
            "price": block.get("usd"),
            "change_24h": block.get("usd_24h_change"),
        }
    fng = (data or {}).get("fear_greed") or {}
    if isinstance(fng, dict):
        out["fng"] = fng.get("value")
        out["fng_label"] = fng.get("value_classification")
    return out


async def _fetch_active_alerts_count() -> int:
    try:
        from modules import errors_log
        return errors_log.count_last_24h()
    except Exception:
        return 0


async def _fetch_today_events() -> list[dict[str, Any]]:
    try:
        from modules import macro_calendar
        upcoming = macro_calendar.upcoming_events(limit=20)
        cutoff = datetime.now(timezone.utc) + timedelta(hours=24)
        out: list[dict[str, Any]] = []
        for ev in upcoming:
            ts = ev.timestamp_utc
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts <= cutoff:
                out.append(
                    {
                        "name": ev.name,
                        "event_id": ev.event_id,
                        "timestamp_utc": ts.isoformat(),
                        "impact": ev.impact_level,
                        "category": ev.category,
                        "affects_positions": ev.affects_positions,
                    }
                )
        return out
    except Exception:
        log.exception("morning_brief: macro_calendar.upcoming_events failed")
        return []


async def _fetch_recent_macro_updates(hours: int = 24) -> str:
    try:
        from modules.intel_memory import format_intel_summary
        return format_intel_summary(hours=hours, source_filter="telegram")
    except Exception:
        return ""


async def _fetch_fund_snapshot() -> dict[str, Any]:
    """Build a compact fund snapshot for the brief."""
    out: dict[str, Any] = {
        "total_capital": 0.0,
        "flywheel_hf": None,
        "basket_active": False,
        "basket_status": None,
        "basket_upnl": 0.0,
    }

    # Fund state defaults
    try:
        import fund_state as fs
        out["basket_active"] = bool(getattr(fs, "BASKET_STATUS", {}).get("active"))
        out["basket_status"] = (getattr(fs, "BASKET_STATUS", {}) or {}).get("last_basket")
    except Exception:
        pass

    # Wallet snapshot — fetch_all_wallets returns list[dict]
    try:
        from modules.portfolio import fetch_all_wallets
        wallets = await fetch_all_wallets()
        total = 0.0
        upnl_sum = 0.0
        for w in (wallets or []):
            if not isinstance(w, dict):
                continue
            try:
                total += float(w.get("account_value") or 0)
            except Exception:
                pass
            for pos in (w.get("positions") or []):
                try:
                    upnl_sum += float(pos.get("unrealized_pnl") or pos.get("unrealizedPnl") or 0)
                except Exception:
                    continue
        out["total_capital"] = total
        out["basket_upnl"] = upnl_sum
    except Exception:
        log.exception("morning_brief: fetch_all_wallets failed")

    # HF flywheel — fetch_all_hyperlend returns list[dict]
    try:
        from modules.hyperlend import fetch_all_hyperlend
        hl = await fetch_all_hyperlend()
        if isinstance(hl, list):
            hfs = []
            for e in hl:
                if not isinstance(e, dict):
                    continue
                hf = e.get("hf") or e.get("health_factor")
                if isinstance(hf, (int, float)) and hf > 0:
                    hfs.append(float(hf))
            if hfs:
                out["flywheel_hf"] = max(hfs)
    except Exception:
        log.exception("morning_brief: fetch_all_hyperlend failed")

    return out


def _format_events(events: list[dict[str, Any]]) -> str:
    if not events:
        return "  (sin catalysts próximos en 24h)"
    out: list[str] = []
    for e in events[:8]:
        name = e.get("name") or e.get("event_id") or "?"
        ts = e.get("timestamp_utc") or e.get("ts_utc") or ""
        impact = (e.get("impact") or "?").upper()
        out.append(f"  \u2022 {ts[:16]} \u2014 [{impact}] {name}")
    return "\n".join(out)


def _format_overnight(market: dict[str, Any]) -> str:
    if not market or not market.get("prices"):
        return "  (no hay datos de mercado)"
    lines: list[str] = []
    for sym in ("BTC", "ETH", "HYPE"):
        b = (market.get("prices") or {}).get(sym) or {}
        price = b.get("price")
        change = b.get("change_24h")
        if price is None:
            lines.append(f"  {sym}: n/a")
            continue
        chg_str = f"{change:+.2f}%" if isinstance(change, (int, float)) else "n/a"
        lines.append(f"  {sym}: ${price:,.2f} ({chg_str})")
    fng = market.get("fng")
    fng_label = market.get("fng_label")
    if fng is not None:
        lines.append(f"  F&G: {fng}{' (' + fng_label + ')' if fng_label else ''}")
    return "\n".join(lines)


async def build_morning_brief() -> str:
    snapshot, market, events, alerts = await asyncio.gather(
        _fetch_fund_snapshot(),
        _fetch_overnight_market(),
        _fetch_today_events(),
        _fetch_active_alerts_count(),
    )
    macro_updates = await _fetch_recent_macro_updates(hours=24)

    now = datetime.now(timezone.utc)
    hf = snapshot.get("flywheel_hf")
    hf_str = f"{hf:.3f}" if isinstance(hf, (int, float)) else "n/a"
    cap = snapshot.get("total_capital", 0.0) or 0.0
    basket_active = snapshot.get("basket_active")
    basket_status = snapshot.get("basket_status") or "n/a"
    basket_upnl = snapshot.get("basket_upnl", 0.0)

    lines: list[str] = [
        "\U0001f408\u200d\u2b1b MORNING BRIEF \u2014 " + now.strftime("%a %d %b %H:%M UTC"),
        "\u2500" * 30,
        "",
        "\U0001f4ca ESTADO DEL FONDO",
        f"Capital total: ${cap:,.0f}",
        f"HF flywheel: {hf_str}",
        f"Basket activa: {'sí (' + basket_status + ')' if basket_active else 'no'}",
    ]
    if basket_active:
        lines.append(f"Basket UPnL: ${basket_upnl:+,.2f}")

    lines.extend([
        "",
        "\U0001f4c8 OVERNIGHT (12h)",
        _format_overnight(market),
        "",
        f"\U0001f3af AGENDA DEL DÍA ({len(events)} eventos)",
        _format_events(events),
        "",
        "\U0001f4e1 MACRO STACK (24h)",
        macro_updates.strip()[:1500] if macro_updates else "  (sin updates capturadas)",
        "",
        f"\u26a0\ufe0f Errores 24h: {alerts}",
        "",
        "Tipea /reporte para análisis completo o /status para vista rápida.",
    ])
    return "\n".join(lines)


async def send_morning_brief(application=None, bot=None, chat_id=None) -> None:
    if os.getenv("MORNING_BRIEF_ENABLED", "true").strip().lower() == "false":
        log.info("morning_brief disabled (MORNING_BRIEF_ENABLED=false)")
        return
    text = await build_morning_brief()
    target_chat = chat_id
    target_bot = bot
    if target_bot is None and application is not None:
        target_bot = application.bot
    if target_chat is None:
        from config import TELEGRAM_CHAT_ID
        target_chat = TELEGRAM_CHAT_ID
    if not target_bot or not target_chat:
        log.warning("morning_brief: no bot or chat_id available")
        return
    try:
        from utils.telegram import send_bot_message
        await send_bot_message(target_bot, target_chat, text)
    except Exception:
        log.exception("morning_brief: send failed")
