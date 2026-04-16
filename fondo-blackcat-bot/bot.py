"""Fondo Black Cat — Telegram bot entry point.

Runs python-telegram-bot v21 (commands) + Telethon userbot (channel reads) +
APScheduler (alert loop) in the same asyncio event loop.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes

from config import (
    ENABLE_ALERTS,
    POLL_INTERVAL_MIN,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from modules.alerts import run_alert_cycle
from modules.analysis import generate_report, generate_thesis_check
from modules.hyperlend import fetch_hyperlend
from modules.market import fetch_market_data
from modules.portfolio import fetch_all_wallets
from modules.telegram_intel import (
    fetch_telegram_intel,
    get_client as get_telethon,
    scan_telegram_unread,
    stop_client as stop_telethon,
)
from modules.unlocks import fetch_unlocks
from modules.x_intel import fetch_x_intel
from templates.formatters import format_hf, format_quick_positions
from utils.security import authorized
from utils.telegram import send_long_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("fondo-blackcat")

# Persistent keyboard for main commands
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("/reporte"), KeyboardButton("/posiciones")],
        [KeyboardButton("/hf"), KeyboardButton("/tesis")],
        [KeyboardButton("/alertas"), KeyboardButton("/start")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

# Runtime state for /alertas toggle
_alerts_enabled = {"value": ENABLE_ALERTS}


# ─── Commands ───────────────────────────────────────────────────────────────
@authorized
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🐈‍⬛ Fondo Black Cat — analista personal\n\n"
        "Comandos:\n"
        "/reporte — reporte completo (portfolio + market + intel + análisis)\n"
        "/posiciones — snapshot rápido (wallets + HF)\n"
        "/hf — Health Factor de HyperLend\n"
        "/tesis — estado de la tesis macro\n"
        "/alertas — toggle alertas automáticas (on/off)\n"
    )
    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_posiciones(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ Snapshot...", reply_markup=MAIN_KEYBOARD)
    wallets, hl = await asyncio.gather(fetch_all_wallets(), fetch_hyperlend())
    await send_long_message(update, format_quick_positions(wallets, hl), reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_hf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    hl = await fetch_hyperlend()
    await update.message.reply_text(format_hf(hl), reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_reporte(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "⏳ Generando reporte completo (puede tardar 30-90s)...",
        reply_markup=MAIN_KEYBOARD,
    )
    # Run unread scan + X intel + all data fetches in parallel.
    # scan_telegram_unread reads unread channels in main folder and marks them read.
    portfolio, hl, market, unlocks, intel_legacy, intel_unread, x_intel = await asyncio.gather(
        fetch_all_wallets(),
        fetch_hyperlend(),
        fetch_market_data(),
        fetch_unlocks(),
        fetch_telegram_intel(hours=24),
        scan_telegram_unread(max_per_dialog=100),
        fetch_x_intel(hours=24),
    )
    # Merge tiered intel + unread scan + X intel into a single dict passed to analysis
    merged_intel: dict = {}
    if isinstance(intel_legacy, dict):
        merged_intel.update(intel_legacy)
    if isinstance(intel_unread, dict) and intel_unread.get("status") == "ok":
        merged_intel["unread_scan"] = intel_unread
    if isinstance(x_intel, dict):
        merged_intel["x_intel"] = x_intel
    report = await generate_report(portfolio, hl, market, unlocks, merged_intel)
    await send_long_message(update, report, reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_tesis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ Analizando estado de la tesis...", reply_markup=MAIN_KEYBOARD)
    portfolio, hl, market = await asyncio.gather(
        fetch_all_wallets(),
        fetch_hyperlend(),
        fetch_market_data(),
    )
    text = await generate_thesis_check(portfolio, hl, market)
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_alertas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _alerts_enabled["value"] = not _alerts_enabled["value"]
    estado = "ON ✅" if _alerts_enabled["value"] else "OFF ⛔"
    await update.message.reply_text(f"Alertas automáticas: {estado}", reply_markup=MAIN_KEYBOARD)


# ─── Scheduler job ───────────────────────────────────────────────────────────
async def _alert_job(application: Application) -> None:
    if not _alerts_enabled["value"]:
        return
    try:
        await run_alert_cycle(application.bot)
    except Exception:  # noqa: BLE001
        log.exception("Alert cycle failed")


# ─── Lifecycle hooks ────────────────────────────────────────────────────────
async def post_init(application: Application) -> None:
    client = await get_telethon()
    if client is None:
        log.warning("Telethon NOT initialized — /reporte will run without channel intel.")
    else:
        log.info("Telethon client connected.")

    if ENABLE_ALERTS:
        scheduler = AsyncIOScheduler()
        scheduler.add_job(
            _alert_job,
            "interval",
            minutes=POLL_INTERVAL_MIN,
            args=[application],
            id="alert_cycle",
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        application.bot_data["scheduler"] = scheduler
        log.info("Alert scheduler started (every %dmin).", POLL_INTERVAL_MIN)


async def post_shutdown(application: Application) -> None:
    sched = application.bot_data.get("scheduler")
    if sched:
        sched.shutdown(wait=False)
    await stop_telethon()


# ─── Main ───────────────────────────────────────────────────────────────────
def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN no configurado", file=sys.stderr)
        sys.exit(1)
    if not TELEGRAM_CHAT_ID:
        print("ERROR: TELEGRAM_CHAT_ID no configurado", file=sys.stderr)
        sys.exit(1)

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("posiciones", cmd_posiciones))
    app.add_handler(CommandHandler("hf", cmd_hf))
    app.add_handler(CommandHandler("reporte", cmd_reporte))
    app.add_handler(CommandHandler("tesis", cmd_tesis))
    app.add_handler(CommandHandler("alertas", cmd_alertas))

    log.info("Fondo Black Cat bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
