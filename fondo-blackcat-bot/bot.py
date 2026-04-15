"""
Fondo Black Cat — Telegram Bot (entry point).

Comandos:
  /reporte     — genera reporte diario completo (portfolio + market + intel + claude)
  /posiciones  — snapshot rápido de wallets + HyperLend HF
  /hf          — health factor de HyperLend
  /alertas     — toggle alertas automáticas on/off
  /tesis       — estado de la tesis (validaciones / invalidaciones / acción)
  /start, /help — ayuda

Seguridad: solo responde a TELEGRAM_CHAT_ID.
"""
from __future__ import annotations

import asyncio
import logging
from functools import wraps

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from config import (
    ALERT_INTERVAL_MINUTES,
    ENABLE_ALERTS,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from modules import alerts as alerts_mod
from modules import analysis as analysis_mod
from modules import hyperlend as hyperlend_mod
from modules import market as market_mod
from modules import portfolio as portfolio_mod
from modules import telegram_intel as intel_mod
from modules import unlocks as unlocks_mod

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("fondo-blackcat")

# Estado runtime
_alerts_enabled = ENABLE_ALERTS
_scheduler: AsyncIOScheduler | None = None


# ---------- Helpers ----------

def authorized(func):
    """Solo responde al chat ID autorizado; el resto se ignora silenciosamente."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not TELEGRAM_CHAT_ID:
            log.warning("TELEGRAM_CHAT_ID no configurado — ignorando")
            return
        if str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
            log.info("Mensaje ignorado de chat_id=%s", update.effective_chat.id)
            return
        return await func(update, context)
    return wrapper


async def send_long_message(update: Update, text: str, chunk_size: int = 4000) -> None:
    """Telegram limita a 4096 chars por mensaje — spliteamos en chunks."""
    for i in range(0, len(text), chunk_size):
        chunk = text[i:i + chunk_size]
        try:
            await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
        except Exception:  # noqa: BLE001
            # fallback sin markdown si hay caracteres problemáticos
            await update.message.reply_text(chunk)


# ---------- Commands ----------

@authorized
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "*Fondo Black Cat — Co-Gestor Bot*\n\n"
        "Comandos:\n"
        "/reporte — reporte diario completo\n"
        "/posiciones — snapshot portfolio + HF\n"
        "/hf — health factor HyperLend\n"
        "/tesis — status de la tesis\n"
        "/alertas — on/off alertas automáticas\n"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


@authorized
async def cmd_reporte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Generando reporte completo...")

    # Fetch data en paralelo donde podemos
    funding_ctx_task = asyncio.create_task(portfolio_mod.fetch_funding_context())
    portfolio_task = asyncio.create_task(portfolio_mod.fetch_all_wallets())
    unlocks_task = asyncio.create_task(unlocks_mod.fetch_upcoming_unlocks())
    intel_task = asyncio.create_task(intel_mod.fetch_telegram_intel())

    portfolio = await portfolio_task
    funding_ctx = await funding_ctx_task

    # HyperLend es sync pero rápido
    hyperlend = hyperlend_mod.get_account_data()
    market = await market_mod.fetch_market_data(funding_ctx)
    unlocks = await unlocks_task
    intel = await intel_task
    intel_text = intel_mod.compile_intel_for_claude(intel)

    await update.message.reply_text("🧠 Análisis con Claude...")
    report = await analysis_mod.generate_report(
        portfolio, hyperlend, market, unlocks, intel_text
    )
    await send_long_message(update, report)


@authorized
async def cmd_posiciones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    snapshots = await portfolio_mod.fetch_all_wallets()
    summary = portfolio_mod.format_quick_positions(snapshots)
    hl = hyperlend_mod.get_account_data()
    hl_summary = hyperlend_mod.format_hyperlend_summary(hl)
    await send_long_message(update, f"{summary}\n\n{hl_summary}")


@authorized
async def cmd_hf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hl = hyperlend_mod.get_account_data()
    if not hl:
        await update.message.reply_text("❌ HyperLend no disponible")
        return
    await update.message.reply_text(
        hyperlend_mod.format_hyperlend_summary(hl),
        parse_mode=ParseMode.MARKDOWN,
    )


@authorized
async def cmd_tesis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Evaluando tesis...")
    funding_ctx = await portfolio_mod.fetch_funding_context()
    portfolio = await portfolio_mod.fetch_all_wallets()
    hyperlend = hyperlend_mod.get_account_data()
    market = await market_mod.fetch_market_data(funding_ctx)
    result = await analysis_mod.generate_thesis_check(portfolio, hyperlend, market)
    await send_long_message(update, result)


@authorized
async def cmd_alertas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _alerts_enabled
    _alerts_enabled = not _alerts_enabled
    state = "ON ✅" if _alerts_enabled else "OFF ❌"
    await update.message.reply_text(f"Alertas automáticas: {state}")


# ---------- Alert scheduler ----------

async def _alert_send(app: Application, text: str) -> None:
    """Callback para alerts module — envía al chat autorizado."""
    if not _alerts_enabled:
        return
    if not TELEGRAM_CHAT_ID:
        return
    try:
        await app.bot.send_message(chat_id=int(TELEGRAM_CHAT_ID), text=text)
    except Exception:  # noqa: BLE001
        log.exception("Alert send failed")


def _start_scheduler(app: Application) -> None:
    global _scheduler
    _scheduler = AsyncIOScheduler()

    async def job():
        if not _alerts_enabled:
            return
        try:
            await alerts_mod.run_alert_cycle(lambda t: _alert_send(app, t))
        except Exception:  # noqa: BLE001
            log.exception("Alert cycle failed")

    _scheduler.add_job(job, "interval", minutes=ALERT_INTERVAL_MINUTES,
                       id="alert_cycle", max_instances=1, coalesce=True)
    _scheduler.start()
    log.info("Alert scheduler started: every %d min", ALERT_INTERVAL_MINUTES)


# ---------- Main ----------

def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN no configurado")
    if not TELEGRAM_CHAT_ID:
        log.warning("TELEGRAM_CHAT_ID vacío — el bot ignorará TODOS los mensajes")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("reporte", cmd_reporte))
    app.add_handler(CommandHandler("posiciones", cmd_posiciones))
    app.add_handler(CommandHandler("hf", cmd_hf))
    app.add_handler(CommandHandler("tesis", cmd_tesis))
    app.add_handler(CommandHandler("alertas", cmd_alertas))

    async def _post_init(application: Application) -> None:
        _start_scheduler(application)

    app.post_init = _post_init

    log.info("Fondo Black Cat bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
