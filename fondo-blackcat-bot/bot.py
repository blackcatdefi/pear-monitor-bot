"""Fondo Black Cat — Telegram bot entry point.

Runs python-telegram-bot v21 (commands) + Telethon userbot (channel reads)
+ APScheduler (alert loop) in the same asyncio event loop.
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
from modules.analysis import generate_report, generate_thesis_check, _load_thesis
from modules.hyperlend import fetch_all_hyperlend
from modules.kill_scenarios import compute_kill_scenarios
from modules.llm_providers import format_provider_status
from modules.market import fetch_market_data
from modules.portfolio import fetch_all_wallets
from modules.telegram_intel import (
    fetch_telegram_intel,
    get_client as get_telethon,
    scan_telegram_unread,
    stop_client as stop_telethon,
)
from modules.unlocks import fetch_unlocks
from modules.bounce_tech import detect_closes as bt_detect_closes, fetch_bounce_tech
from modules.gmail_intel import scan_gmail_unread
from modules.x_intel import fetch_x_intel, debug_x_status
from modules.flywheel import compute_flywheel
from modules.liq_calc import compute_liq_matrix
from modules.intel_memory import format_intel_summary, cleanup_old as intel_cleanup, get_unprocessed_count
from modules import pnl_tracker, position_log
from templates.formatters import format_hf, format_quick_positions
from templates.timeline import format_timeline
from utils.security import authorized
from utils.telegram import send_long_message


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("fondo-blackcat")


# Persistent keyboard — todos los comandos accesibles con un tap.
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("/reporte"), KeyboardButton("/posiciones")],
        [KeyboardButton("/flywheel"), KeyboardButton("/liqcalc")],
        [KeyboardButton("/timeline"), KeyboardButton("/tesis")],
        [KeyboardButton("/hf"), KeyboardButton("/kill")],
        [KeyboardButton("/pnl"), KeyboardButton("/log")],
        [KeyboardButton("/intel"), KeyboardButton("/alertas")],
        [KeyboardButton("/providers"), KeyboardButton("/debug_x")],
        [KeyboardButton("/start")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

# Runtime state for /alertas toggle
_alerts_enabled = {"value": ENABLE_ALERTS}

# Set to False if Telethon fails to init — commands skip channel intel gracefully
_telethon_ok = True


# ─── Commands ──────────────────────────────────────────────────────────────


@authorized
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🐱‍⬛ Fondo Black Cat — analista personal\n\n"
        "Keyboard — todos los comandos:\n"
        "/reporte — TODO-EN-UNO: timeline + posiciones + análisis\n"
        "/posiciones — snapshot rápido (wallets + HF)\n"
        "/flywheel — pair trade HL (LONG HYPE / SHORT UETH)\n"
        "/liqcalc — matriz liq HYPE × deuda\n"
        "/timeline — timeline X 48h (154 cuentas)\n"
        "/tesis — estado de la tesis macro\n"
        "/hf — Health Factor de HyperLend\n"
        "/kill — kill scenarios de cada posición\n"
        "/pnl — realized PnL 7D / 30D / YTD\n"
        "/log — últimas 20 entradas del position log\n"
        "/intel — resumen de intel memory (últimas 24h)\n"
        "/providers — status de los LLM providers\n"
        "/debug_x — diagnóstico de conectividad X/Twitter\n"
        "/alertas — toggle alertas automáticas (on/off)\n"
    )
    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_posiciones(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ Snapshot...", reply_markup=MAIN_KEYBOARD)
    wallets, hl, bt = await asyncio.gather(fetch_all_wallets(), fetch_all_hyperlend(), fetch_bounce_tech())

    # Detect Bounce Tech position closes
    bt_closes = bt_detect_closes(bt)
    for close in bt_closes:
        close_msg = (
            f"🔔 Bounce Tech {close['direction']} {close['asset']} "
            f"{close['leverage']} CERRADA.\n"
            f"Último valor registrado: ${close['last_value_usd']:,.2f}"
        )
        await update.message.reply_text(close_msg, reply_markup=MAIN_KEYBOARD)
        position_log.append(
            kind="CLOSE",
            message=f"Bounce Tech {close['direction']} {close['asset']} {close['leverage']} closed. Last value: ${close['last_value_usd']:,.2f}",
            asset=close["asset"],
            amount_usd=0,
            wallet_label="Bounce Tech",
        )

    await send_long_message(update, format_quick_positions(wallets, hl, bounce_tech=bt), reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_hf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    hl = await fetch_all_hyperlend()
    await update.message.reply_text(format_hf(hl), reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_reporte(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reporte TODO-EN-UNO: timeline X + posiciones + análisis LLM.

    Emite 3 mensajes secuenciales:
    1. Timeline — top 40 tweets por engagement de las últimas 48h (154 cuentas curadas)
    2. Posiciones — snapshot rápido de wallets + HyperLend + Bounce Tech
    3. Análisis — reporte completo generado por LLM cascade (market + intel + tesis)
    """
    await update.message.reply_text(
        "⏳ Generando reporte completo: timeline + posiciones + análisis (30-90s)...",
        reply_markup=MAIN_KEYBOARD,
    )

    # Todos los fetches en paralelo (Telethon separado — puede estar deshabilitado).
    portfolio, hl, market, unlocks, x_intel, gmail_intel, bt = await asyncio.gather(
        fetch_all_wallets(),
        fetch_all_hyperlend(),
        fetch_market_data(),
        fetch_unlocks(),
        fetch_x_intel(hours=48),
        scan_gmail_unread(),
        fetch_bounce_tech(),
    )

    if _telethon_ok:
        intel_legacy, intel_unread = await asyncio.gather(
            fetch_telegram_intel(hours=24),
            scan_telegram_unread(max_per_dialog=100),
        )
    else:
        intel_legacy = {"status": "error", "error": "telethon_disabled"}
        intel_unread = {"status": "error", "error": "telethon_disabled"}

    # ─── Sección 1: Timeline X (48h) — omitir si todas las fuentes fallaron ──
    x_intel_ok = isinstance(x_intel, dict) and x_intel.get("status") == "ok"

    if x_intel_ok:
        timeline_text = format_timeline(x_intel, top_n=40)
        await send_long_message(
            update,
            "📡 TIMELINE X — 48H\n" + ("─" * 30) + "\n\n" + timeline_text,
            reply_markup=MAIN_KEYBOARD,
        )

    # ─── Sección 2: Posiciones ────────────────────────────────────────────────
    positions_text = format_quick_positions(portfolio, hl, bounce_tech=bt)
    await send_long_message(
        update,
        "💼 POSICIONES\n" + ("─" * 30) + "\n\n" + positions_text,
        reply_markup=MAIN_KEYBOARD,
    )

    # ─── Sección 3: Análisis LLM cascade ──────────────────────────────────────
    merged_intel: dict = {}
    if isinstance(intel_legacy, dict):
        merged_intel.update(intel_legacy)
    if isinstance(intel_unread, dict) and intel_unread.get("status") == "ok":
        merged_intel["unread_scan"] = intel_unread
    if isinstance(x_intel, dict):
        merged_intel["x_intel"] = x_intel
    if isinstance(gmail_intel, dict) and gmail_intel.get("status") == "ok":
        merged_intel["gmail_intel"] = gmail_intel
    if bt:
        merged_intel["bounce_tech"] = bt

    report, thesis_update = await generate_report(portfolio, hl, market, unlocks, merged_intel)

    await send_long_message(
        update,
        "🧠 ANÁLISIS COMPLETO\n" + ("─" * 30) + "\n\n" + report,
        reply_markup=MAIN_KEYBOARD,
    )
    if thesis_update:
        await send_long_message(update, thesis_update, reply_markup=MAIN_KEYBOARD)

    # Nota si timeline X no disponible
    if not x_intel_ok:
        await update.message.reply_text(
            "ℹ️ Nota: Timeline X no disponible en este reporte (todas las fuentes fallaron). "
            "Verificar X_BEARER_TOKEN y disponibilidad de Nitter/RSSHub.",
            reply_markup=MAIN_KEYBOARD,
        )


@authorized
async def cmd_tesis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current thesis state from disk — no fresh API call."""
    state = _load_thesis()
    if not state.get("components"):
        await update.message.reply_text(
            "📊 No hay tesis guardada aún. Ejecutar /reporte primero.",
            reply_markup=MAIN_KEYBOARD
        )
        return

    from modules.analysis import _thesis_context
    text = _thesis_context(state)

    unprocessed = get_unprocessed_count()
    if unprocessed > 0:
        text += f"\n\n⏳ {unprocessed} items de intel pendientes de procesar"

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
    estado = "ON ✅" if _alerts_enabled["value"] else "OFF 🚫"
    await update.message.reply_text(f"Alertas automáticas: {estado}", reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_intel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show intel memory from last 24h (or custom hours)."""
    args = context.args or []
    hours = 24
    source_filter = None
    for a in args:
        if a.isdigit():
            hours = int(a)
        elif a in ("telegram", "x", "gmail", "onchain", "macro"):
            source_filter = a
    text = format_intel_summary(hours, source_filter)
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_debug_x(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show X/Twitter connectivity diagnostics."""
    text = await debug_x_status()
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_providers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show LLM provider status dashboard."""
    text = format_provider_status()
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_flywheel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ Calculando flywheel pair trade...", reply_markup=MAIN_KEYBOARD)
    try:
        text = await compute_flywheel()
    except Exception as exc:  # noqa: BLE001
        log.exception("flywheel failed")
        text = f"❌ /flywheel falló: {exc}"
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_liqcalc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ Calculando matriz de liquidación...", reply_markup=MAIN_KEYBOARD)
    try:
        text = await compute_liq_matrix()
    except Exception as exc:  # noqa: BLE001
        log.exception("liqcalc failed")
        text = f"❌ /liqcalc falló: {exc}"
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_kill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ Evaluando kill scenarios...", reply_markup=MAIN_KEYBOARD)
    try:
        text = await compute_kill_scenarios()
    except Exception as exc:  # noqa: BLE001
        log.exception("kill scenarios failed")
        text = f"❌ /kill falló: {exc}"
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if args and args[0].lower() == "ciclo":
        text = pnl_tracker.build_cycle_summary()
        await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)
        return

    if args and args[0].lower() == "add":
        try:
            params = pnl_tracker.parse_manual_add(args[1:])
            row_id = pnl_tracker.record_event(**params)
            await update.message.reply_text(
                f"✅ PnL event #{row_id} registered ({params['category']} "
                f"{params['asset']} ${params['amount_usd']:.2f}).",
                reply_markup=MAIN_KEYBOARD,
            )
        except ValueError as exc:
            await update.message.reply_text(f"❌ {exc}", reply_markup=MAIN_KEYBOARD)
        return

    try:
        text = pnl_tracker.build_summary()
    except Exception as exc:  # noqa: BLE001
        log.exception("pnl failed")
        text = f"❌ /pnl falló: {exc}"
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if args and args[0].lower() == "add":
        try:
            params = position_log.parse_manual_add(args[1:])
            row_id = position_log.append(**params)
            await update.message.reply_text(
                f"✅ Log entry #{row_id} agregada ({params['kind']}).",
                reply_markup=MAIN_KEYBOARD,
            )
        except ValueError as exc:
            await update.message.reply_text(f"❌ {exc}", reply_markup=MAIN_KEYBOARD)
        return

    try:
        entries = position_log.last_n(20)
        text = position_log.format_log(entries)
    except Exception as exc:  # noqa: BLE001
        log.exception("log failed")
        text = f"❌ /log falló: {exc}"
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


# ─── Scheduler job ──────────────────────────────────────────────────────────


async def _alert_job(application: Application) -> None:
    if not _alerts_enabled["value"]:
        return
    try:
        await run_alert_cycle(application.bot)
    except Exception:  # noqa: BLE001
        log.exception("Alert cycle failed")


# ─── Lifecycle hooks ──────────────────────────────────────────────────────────


async def post_init(application: Application) -> None:
    global _telethon_ok
    try:
        client = await get_telethon()
        if client is None:
            log.warning("Telethon NOT initialized — /reporte will run without channel intel.")
            _telethon_ok = False
        else:
            log.info("Telethon client connected.")
    except Exception:
        log.exception("Telethon init failed — Telegram intel disabled")
        _telethon_ok = False

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

    # Cleanup old intel memory entries (7+ days old)
    try:
        deleted = intel_cleanup(days=7)
        log.info("Intel memory cleanup: deleted %d old entries", deleted)
    except Exception:
        log.exception("Intel memory cleanup failed")


async def post_shutdown(application: Application) -> None:
    sched = application.bot_data.get("scheduler")
    if sched:
        sched.shutdown(wait=False)
    await stop_telethon()


# ─── Main ──────────────────────────────────────────────────────────────────


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
    app.add_handler(CommandHandler("intel", cmd_intel))
    app.add_handler(CommandHandler("debug_x", cmd_debug_x))
    app.add_handler(CommandHandler("providers", cmd_providers))
    app.add_handler(CommandHandler("flywheel", cmd_flywheel))
    app.add_handler(CommandHandler("liqcalc", cmd_liqcalc))
    app.add_handler(CommandHandler("kill", cmd_kill))
    app.add_handler(CommandHandler("pnl", cmd_pnl))
    app.add_handler(CommandHandler("log", cmd_log))

    log.info("Fondo Black Cat bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
