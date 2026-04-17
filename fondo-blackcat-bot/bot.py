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
from modules.hyperlend import fetch_all_hyperlend
from modules.market import fetch_market_data
from modules.portfolio import fetch_all_wallets
from modules.telegram_intel import (
    fetch_telegram_intel,
    get_client as get_telethon,
    scan_telegram_unread,
    stop_client as stop_telethon,
)
from modules.unlocks import fetch_unlocks
from modules.bounce_tech import fetch_bounce_tech
from modules.gmail_intel import scan_gmail_unread
from modules.x_intel import fetch_x_intel
from templates.formatters import format_hf, format_quick_positions
from templates.timeline import format_timeline
from utils.security import authorized
from utils.telegram import send_long_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("fondo-blackcat")

# Persistent keyboard — mínimo e impecable.
# /reporte = todo-en-uno (timeline + posiciones + análisis Claude).
# /alertas = único botón de estado (toggle ON/OFF) no cubierto por /reporte.
# El resto (/tesis, /hf, /posiciones, /timeline, /start) sigue funcionando si se tipea,
# pero no ocupa espacio en el keyboard porque son redundantes con /reporte o con
# el pear-monitor-bot (posiciones en tiempo real).
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("/reporte"), KeyboardButton("/alertas")],
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
        "Keyboard (2 botones, lo único que necesitás):\n"
        "/reporte — TODO-EN-UNO: timeline X (48h) + posiciones + análisis\n"
        "/alertas — toggle alertas automáticas (on/off)\n\n"
        "Comandos extra (tipear manual, sin botón):\n"
        "/tesis — estado de la tesis macro\n"
        "/hf — Health Factor de HyperLend\n"
        "/posiciones — snapshot rápido (wallets + HF)\n"
        "/timeline — sólo timeline X 48h\n"
    )
    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_posiciones(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ Snapshot...", reply_markup=MAIN_KEYBOARD)
    wallets, hl, bt = await asyncio.gather(fetch_all_wallets(), fetch_all_hyperlend(), fetch_bounce_tech())
    await send_long_message(update, format_quick_positions(wallets, hl, bounce_tech=bt), reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_hf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    hl = await fetch_all_hyperlend()
    await update.message.reply_text(format_hf(hl), reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_reporte(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reporte TODO-EN-UNO: timeline X + posiciones + análisis Claude.

    Emite 3 mensajes secuenciales:
      1. Timeline — top 40 tweets por engagement de las últimas 48h (154 cuentas curadas)
      2. Posiciones — snapshot rápido de wallets + HyperLend + Bounce Tech
      3. Análisis — reporte completo generado por Claude (market + intel + tesis)
    """
    await update.message.reply_text(
        "⏳ Generando reporte completo: timeline + posiciones + análisis (30-90s)...",
        reply_markup=MAIN_KEYBOARD,
    )
    # Todos los fetches en paralelo.
    portfolio, hl, market, unlocks, intel_legacy, intel_unread, x_intel, gmail_intel, bt = await asyncio.gather(
        fetch_all_wallets(),
        fetch_all_hyperlend(),
        fetch_market_data(),
        fetch_unlocks(),
        fetch_telegram_intel(hours=24),
        scan_telegram_unread(max_per_dialog=100),
        fetch_x_intel(hours=48),
        scan_gmail_unread(),
        fetch_bounce_tech(),
    )
    # ─── Sección 1: Timeline X (48h) ─────────────────────────────────────
    timeline_text = format_timeline(x_intel, top_n=40)
    await send_long_message(
        update,
        "📡 TIMELINE X — 48H\n" + ("─" * 30) + "\n\n" + timeline_text,
        reply_markup=MAIN_KEYBOARD,
    )
    # ─── Sección 2: Posiciones ───────────────────────────────────────────
    positions_text = format_quick_positions(portfolio, hl, bounce_tech=bt)
    await send_long_message(
        update,
        "💼 POSICIONES\n" + ("─" * 30) + "\n\n" + positions_text,
        reply_markup=MAIN_KEYBOARD,
    )
    # ─── Sección 3: Análisis Claude ──────────────────────────────────────
    merged_intel: dict = {}
    if isinstance(intel_legacy, dict):
        merged_intel.update(intel_legacy)
    if isinstance(intel_unread, dict) and intel_unread.get("status") == "ok":
        merged_intel["unread_scan"] = intel_unread
    if isinstance(x_intel, dict):
        merged_intel["x_intel"] = x_intel
    if isinstance(gmail_intel, dict) and gmail_intel.get("status") == "ok":
        merged_intel["gmail_intel"] = gmail_intel
    report, thesis_update = await generate_report(portfolio, hl, market, unlocks, merged_intel)
    await send_long_message(
        update,
        "🧠 ANÁLISIS COMPLETO\n" + ("─" * 30) + "\n\n" + report,
        reply_markup=MAIN_KEYBOARD,
    )
    if thesis_update:
        await send_long_message(update, thesis_update, reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_tesis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ Analizando estado de la tesis...", reply_markup=MAIN_KEYBOARD)
    portfolio, hl, market = await asyncio.gather(
        fetch_all_wallets(),
        fetch_all_hyperlend(),
        fetch_market_data(),
    )
    text = await generate_thesis_check(portfolio, hl, market)
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_timeline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "⏳ Leyendo últimas 48h de tu timeline X (154 cuentas)...",
        reply_markup=MAIN_KEYBOARD,
    )
    x_intel = await fetch_x_intel(hours=48)
    text = format_timeline(x_intel, top_n=40)
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_alertas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _alerts_enabled["value"] = not _alerts_enabled["value"]
    estado = "ON ✅" if _alerts_enabled["value"] else "OFF ⛔"
    await update.message.reply_text(f"Alertas automáticas: {estado}", reply_markup=MAIN_KEYBOARD)


# ─── Scheduler job ──────────────────────────────────────────────────────────
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
    app.add_handler(CommandHandler("timeline", cmd_timeline))
    app.add_handler(CommandHandler("alertas", cmd_alertas))

    log.info("Fondo Black Cat bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
