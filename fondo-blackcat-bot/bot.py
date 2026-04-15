"""Fondo Black Cat — Telegram bot entry point.

Commands (only authorised chat):
  /start       welcome + command list
  /reporte     full daily report (Claude-generated)
  /posiciones  quick portfolio + HF snapshot
  /hf          HyperLend health factor
  /mercado     market data snapshot
  /unlocks     token unlocks next 7d
  /tesis       thesis status with validators/invalidators
  /alertas     toggle automatic alerts on/off

Scheduler:
  - check_alerts every ALERT_INTERVAL_MINUTES
  - daily_report at DAILY_REPORT_UTC_HOUR:00 UTC
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from config import (
    ALERT_INTERVAL_MINUTES,
    DAILY_REPORT_UTC_HOUR,
    ENABLE_AUTO_ALERTS,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from modules import alerts, analysis, hyperlend, market, portfolio, telegram_intel, unlocks
from modules.portfolio import format_quick_positions
from modules.hyperlend import format_hyperlend
from modules.market import format_market_quick
from modules.unlocks import format_unlocks
from templates.daily_report import build_fallback_report
from templates.telegram_report import format_intel_summary

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("fondo-blackcat")

# Global mutable state for the toggle
STATE = {"auto_alerts_enabled": ENABLE_AUTO_ALERTS}


# --- Decorator: only authorised chat ------------------------------------------------
def authorized(handler):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat is None:
            return
        if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
            log.info("Ignoring message from unauthorised chat_id=%s", update.effective_chat.id)
            return
        return await handler(update, context)
    return wrapper


# --- Helpers -----------------------------------------------------------------------
async def _send_long(update: Update, text: str, chunk_size: int = 3900) -> None:
    for i in range(0, len(text), chunk_size):
        await update.message.reply_text(text[i:i + chunk_size])


async def _send_to_owner(app: Application, text: str, chunk_size: int = 3900) -> None:
    for i in range(0, len(text), chunk_size):
        try:
            await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text[i:i + chunk_size])
        except Exception as e:  # noqa: BLE001
            log.warning("send_message failed: %s", e)


# --- Command handlers --------------------------------------------------------------
@authorized
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🐈‍⬛ FONDO BLACK CAT — bot activo.\n\n"
        "Comandos:\n"
        "  /reporte — reporte completo (Claude)\n"
        "  /posiciones — snapshot portfolio + HF\n"
        "  /hf — HyperLend health factor\n"
        "  /mercado — market data\n"
        "  /unlocks — unlocks próximos 7d\n"
        "  /tesis — estado de la tesis\n"
        "  /alertas — toggle alertas automáticas\n"
    )


@authorized
async def cmd_posiciones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Leyendo wallets...")
    snap = await portfolio.fetch_all_wallets()
    hl = await hyperlend.get_account_data()
    hf = hl.get("hf") if hl.get("hf") != float("inf") else None
    text = format_quick_positions(snap, hf)
    await _send_long(update, text)


@authorized
async def cmd_hf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Leyendo HyperLend on-chain...")
    hl = await hyperlend.get_account_data()
    await update.message.reply_text(format_hyperlend(hl))


@authorized
async def cmd_mercado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Fetching market data...")
    m = await market.fetch_market_data()
    await _send_long(update, format_market_quick(m))


@authorized
async def cmd_unlocks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Consultando DefiLlama unlocks...")
    u = await unlocks.fetch_unlocks(days=7)
    await _send_long(update, format_unlocks(u))


@authorized
async def cmd_reporte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Generando reporte completo (portfolio + market + unlocks + telegram + Claude)...")

    # 1. Portfolio + HyperLend + market + unlocks + telegram — todo en paralelo
    p_task = asyncio.create_task(portfolio.fetch_all_wallets())
    h_task = asyncio.create_task(hyperlend.get_account_data())
    m_task = asyncio.create_task(market.fetch_market_data())
    u_task = asyncio.create_task(unlocks.fetch_unlocks(days=7))
    t_task = asyncio.create_task(telegram_intel.fetch_telegram_intel(hours=24))

    p_data, h_data, m_data, u_data, t_data = await asyncio.gather(
        p_task, h_task, m_task, u_task, t_task,
    )

    intel_text = telegram_intel.summarize_for_prompt(t_data)
    intel_summary = format_intel_summary(t_data)
    await update.message.reply_text(intel_summary)

    # 2. Claude análisis
    await update.message.reply_text("🧠 Claude sintetizando...")
    try:
        report = await analysis.generate_report(p_data, h_data, m_data, u_data, intel_text)
    except Exception as e:  # noqa: BLE001
        log.exception("Claude failed, using fallback")
        report = build_fallback_report(p_data, h_data, m_data, u_data, intel_text)

    await _send_long(update, report)


@authorized
async def cmd_tesis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Evaluate thesis validators/invalidators."""
    await update.message.reply_text("⏳ Evaluando tesis...")
    snap = await portfolio.fetch_all_wallets()
    m = await market.fetch_market_data()
    hl = await hyperlend.get_account_data()

    btc_price = (m.get("btc") or {}).get("price")
    hype_price = (snap.get("mids") or {}).get("HYPE")
    try:
        hype_f = float(hype_price) if hype_price else None
    except (TypeError, ValueError):
        hype_f = None
    hf = hl.get("hf")
    fng = (m.get("fear_greed") or {}).get("current")

    # WAR TRADE: short alt basket status (UPnL aggregate across alt-short wallets)
    short_upnl = 0.0
    short_equity = 0.0
    for w in snap["wallets"]:
        if "Alt Short" in w.get("label", ""):
            short_upnl += w.get("upnl_total") or 0
            short_equity += w.get("account_value") or 0

    lines = [
        "🎯 TESIS — FONDO BLACK CAT",
        "",
        "✅ VALIDADORES",
        f"  · HF HyperLend: {'OK' if hf and hf > 1.20 else 'WARN'} ({hf:.3f})" if hf else "  · HF: sin data",
        f"  · BTC: ${btc_price:,.0f}" if btc_price else "  · BTC: sin data",
        f"  · F&G: {fng}" if fng is not None else "",
        f"  · Alt Short Bleed UPnL agregado: ${short_upnl:+,.0f} sobre ${short_equity:,.0f} equity",
        "",
        "⚠️ INVALIDADORES A VIGILAR",
        "  · Ceasefire Iran/Israel sostenido → cubrir SHORT alts + WAR TRADE",
        "  · Fed pivot dovish (Warsh) → idem",
        "  · HYPE < $30 → verificar HF HyperLend",
        "  · BTC < $62K → target ZordXBT $46K activo",
        "",
        f"  HYPE ahora: ${hype_f:,.2f}" if hype_f else "  HYPE: sin data HL",
    ]
    await _send_long(update, "\n".join(l for l in lines if l is not None))


@authorized
async def cmd_alertas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    STATE["auto_alerts_enabled"] = not STATE["auto_alerts_enabled"]
    status = "ON ✅" if STATE["auto_alerts_enabled"] else "OFF ❌"
    await update.message.reply_text(f"Alertas automáticas: {status}")


# --- Scheduler jobs ---------------------------------------------------------------
async def scheduled_alerts(app: Application) -> None:
    if not STATE["auto_alerts_enabled"]:
        return
    async def _send(msg: str) -> None:
        await _send_to_owner(app, msg)
    try:
        await alerts.run_checks(_send)
    except Exception as e:  # noqa: BLE001
        log.exception("scheduled_alerts failed: %s", e)


async def scheduled_daily_report(app: Application) -> None:
    """Generate full daily report without user command, send to owner."""
    try:
        p, h, m, u, t = await asyncio.gather(
            portfolio.fetch_all_wallets(),
            hyperlend.get_account_data(),
            market.fetch_market_data(),
            unlocks.fetch_unlocks(days=7),
            telegram_intel.fetch_telegram_intel(hours=24),
        )
        intel_text = telegram_intel.summarize_for_prompt(t)
        report = await analysis.generate_report(p, h, m, u, intel_text)
        await _send_to_owner(app, report)
    except Exception as e:  # noqa: BLE001
        log.exception("daily report failed: %s", e)
        await _send_to_owner(app, f"⚠️ Daily report falló: {e}")


# --- Main -------------------------------------------------------------------------
def build_app() -> Application:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reporte", cmd_reporte))
    app.add_handler(CommandHandler("posiciones", cmd_posiciones))
    app.add_handler(CommandHandler("hf", cmd_hf))
    app.add_handler(CommandHandler("mercado", cmd_mercado))
    app.add_handler(CommandHandler("unlocks", cmd_unlocks))
    app.add_handler(CommandHandler("tesis", cmd_tesis))
    app.add_handler(CommandHandler("alertas", cmd_alertas))
    return app


async def _post_init(app: Application) -> None:
    """Start scheduler once the app is initialised and event loop is running."""
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        scheduled_alerts, "interval", minutes=ALERT_INTERVAL_MINUTES,
        args=[app], id="alerts", replace_existing=True,
    )
    scheduler.add_job(
        scheduled_daily_report, "cron", hour=DAILY_REPORT_UTC_HOUR, minute=0,
        args=[app], id="daily_report", replace_existing=True,
    )
    scheduler.start()
    app.bot_data["scheduler"] = scheduler
    log.info(
        "Scheduler started — alerts every %sm, daily report %02d:00 UTC",
        ALERT_INTERVAL_MINUTES, DAILY_REPORT_UTC_HOUR,
    )
    try:
        await app.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"🐈‍⬛ Fondo Black Cat bot online — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("startup notification failed: %s", e)


def main() -> None:
    app = build_app()
    app.post_init = _post_init
    log.info("Starting Fondo Black Cat bot polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
