"""Fondo Black Cat — Telegram bot entry point.

Runs python-telegram-bot v21 (commands) + Telethon userbot (channel reads) +
APScheduler (alert loop) in the same asyncio event loop.

Round 3 (2026-04-19):
  - /ciclo y /ciclo_update (Trade del Ciclo manual tracking — Blofin BTC LONG).
  - Snapshot-based delta tracking: /posiciones y /reporte muestran cambios vs snapshot anterior.
  - /reporte usa asyncio.Lock para evitar re-ejecuciones concurrentes.
  - Mensaje nota para X timeline (A.1): si falla, se informa pero no bloquea el reporte.
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
from modules.bounce_tech import fetch_bounce_tech
from modules import cycle_trade
from modules.gmail_intel import scan_gmail_unread
from modules.hyperlend import fetch_all_hyperlend
from modules.kill_scenarios import compute_kill_scenarios
from modules.liq_calc import compute_liq_matrix
from modules.market import fetch_market_data
from modules import pnl_tracker, position_log, snapshots
from modules.portfolio import fetch_all_wallets
from modules.telegram_intel import (
    fetch_telegram_intel,
    get_client as get_telethon,
    scan_telegram_unread,
    stop_client as stop_telethon,
)
from modules.unlocks import fetch_unlocks
from modules.flywheel import compute_flywheel
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

# Persistent keyboard — core commands. Extras via typing.
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("/reporte"), KeyboardButton("/posiciones")],
        [KeyboardButton("/ciclo"), KeyboardButton("/kill")],
        [KeyboardButton("/flywheel"), KeyboardButton("/liqcalc")],
        [KeyboardButton("/tesis"), KeyboardButton("/alertas")],
        [KeyboardButton("/start")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

# Runtime state
_alerts_enabled = {"value": ENABLE_ALERTS}
_telethon_ok = True
_reporte_lock = asyncio.Lock()


# ─── Commands ───────────────────────────────────────────────────────────────
@authorized
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🐈‍⬛ Fondo Black Cat — Co-Gestor personal\n\n"
        "Keyboard principal:\n"
        "/reporte — TODO-EN-UNO: timeline X + posiciones + deltas + análisis Claude\n"
        "/posiciones — snapshot wallets + HF + BT + Trade del Ciclo + deltas\n"
        "/ciclo — estado Trade del Ciclo (BTC LONG Blofin)\n"
        "/kill — kill scenarios: invalidadores de cada tesis\n"
        "/flywheel — pair trade HL (LONG HYPE / SHORT UETH)\n"
        "/liqcalc — matriz liq HYPE × deuda\n"
        "/tesis — estado de la tesis macro\n"
        "/alertas — toggle alertas automáticas (on/off)\n\n"
        "Extras por tipeo:\n"
        "/ciclo_update margin=500 entry=77200 size=0.0065 mark=77300\n"
        "/ciclo_update close=true → cerrar posición\n"
        "/hf — HF HyperLend | /timeline — X 48h\n"
        "/pnl [add ...] | /log [add ...]\n"
    )
    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)


async def _gather_positions_data():
    """Shared fetch for /posiciones and /reporte."""
    return await asyncio.gather(
        fetch_all_wallets(),
        fetch_all_hyperlend(),
        fetch_bounce_tech(),
    )


@authorized
async def cmd_posiciones(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ Snapshot...", reply_markup=MAIN_KEYBOARD)
    wallets, hl, bt = await _gather_positions_data()
    cs = cycle_trade.get_state()
    cycle_block = cycle_trade.format_status_short(cs)
    # Build + persist snapshot; returns delta block vs previous
    cycle_snapshot = {
        "active": cs.get("active", False),
        "margin_usd": cs.get("margin_usd", 0.0),
        "upnl_usd": cycle_trade.compute_upnl(cs),
    }
    delta_block = snapshots.take_and_format(wallets, hl, bt, cycle_snapshot)
    text = format_quick_positions(
        wallets, hl,
        bounce_tech=bt,
        cycle_trade_block=cycle_block,
        delta_block=delta_block,
    )
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_hf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    hl = await fetch_all_hyperlend()
    await update.message.reply_text(format_hf(hl), reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_reporte(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reporte TODO-EN-UNO: timeline X + posiciones+deltas + análisis Claude.

    Protected by an asyncio.Lock so duplicate triggers (double-tap) don't
    race each other. If another /reporte is in flight we respond fast.
    """
    if _reporte_lock.locked():
        await update.message.reply_text(
            "⏳ Ya hay un /reporte en curso. Esperá a que termine (30-90s).",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    async with _reporte_lock:
        await update.message.reply_text(
            "⏳ Generando reporte completo: timeline + posiciones + deltas + análisis (30-90s)...",
            reply_markup=MAIN_KEYBOARD,
        )
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
        else:
            # A.1 — si no hay timeline no mostramos una sección rota,
            # solo una línea informativa al final del reporte.
            err_detail = ""
            if isinstance(x_intel, dict):
                e = x_intel.get("error") or ""
                err_detail = f" ({e})" if e else ""
            log.warning("X timeline unavailable: %s", x_intel)

        # ─── Sección 2: Posiciones + deltas + Trade del Ciclo ──
        cs = cycle_trade.get_state()
        cycle_block = cycle_trade.format_status_short(cs)
        cycle_snapshot = {
            "active": cs.get("active", False),
            "margin_usd": cs.get("margin_usd", 0.0),
            "upnl_usd": cycle_trade.compute_upnl(cs),
        }
        delta_block = snapshots.take_and_format(portfolio, hl, bt, cycle_snapshot)
        positions_text = format_quick_positions(
            portfolio, hl,
            bounce_tech=bt,
            cycle_trade_block=cycle_block,
            delta_block=delta_block,
        )
        await send_long_message(
            update,
            "💼 POSICIONES\n" + ("─" * 30) + "\n\n" + positions_text,
            reply_markup=MAIN_KEYBOARD,
        )

        # ─── Sección 3: Análisis Claude ──
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
        merged_intel["cycle_trade"] = cs
        report, thesis_update = await generate_report(portfolio, hl, market, unlocks, merged_intel)
        await send_long_message(
            update,
            "🧠 ANÁLISIS COMPLETO\n" + ("─" * 30) + "\n\n" + report,
            reply_markup=MAIN_KEYBOARD,
        )
        if thesis_update:
            await send_long_message(update, thesis_update, reply_markup=MAIN_KEYBOARD)

        if not x_intel_ok:
            await update.message.reply_text(
                "ℹ️ Timeline X no disponible en este reporte "
                "(x_api + Nitter + RSSHub fallaron). Log detallado en Railway.",
                reply_markup=MAIN_KEYBOARD,
            )


@authorized
async def cmd_tesis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("⏳ Analizando estado de la tesis...", reply_markup=MAIN_KEYBOARD)
    portfolio, hl, market = await asyncio.gather(
        fetch_all_wallets(),
        fetch_all_hyperlend(),
        fetch_market_data(),
    )
    text = await generate_thesis_check(portfolio, hl, market)
    # Append Trade del Ciclo snapshot at the tail
    cs = cycle_trade.get_state()
    if cs.get("active"):
        upnl = cycle_trade.compute_upnl(cs)
        sign = "+" if upnl >= 0 else ""
        text += (
            f"\n\nTRADE DEL CICLO: LONG BTC {cs.get('leverage', 10)}x en Blofin."
            f" Margin ${cs.get('margin_usd', 0):,.0f} de ${cycle_trade.TOTAL_DEPLOYABLE:,.0f}."
            f" UPnL {sign}${upnl:,.2f}. Horizonte 12-18m. → MANTENER (NO intervenir hasta liq o TP)"
        )
    else:
        text += "\n\nTRADE DEL CICLO: pendiente apertura. Esperar bonus Blofin + entry ~$77K."
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
    await update.message.reply_text("⏳ Calculando kill scenarios...", reply_markup=MAIN_KEYBOARD)
    try:
        text = await compute_kill_scenarios()
    except Exception as exc:  # noqa: BLE001
        log.exception("kill_scenarios failed")
        text = f"❌ /kill falló: {exc}"
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_ciclo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = cycle_trade.get_state()
    text = cycle_trade.format_full_status(state)
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_ciclo_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    try:
        _, summary = cycle_trade.update_from_args(args)
        await update.message.reply_text(summary, reply_markup=MAIN_KEYBOARD)
    except ValueError as exc:
        await update.message.reply_text(f"❌ {exc}", reply_markup=MAIN_KEYBOARD)
    except Exception as exc:  # noqa: BLE001
        log.exception("ciclo_update failed")
        await update.message.reply_text(f"❌ Error guardando estado: {exc}", reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
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


# ─── Lifecycle hooks ────────────────────────────────────────────────────────
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
    app.add_handler(CommandHandler("flywheel", cmd_flywheel))
    app.add_handler(CommandHandler("liqcalc", cmd_liqcalc))
    app.add_handler(CommandHandler("kill", cmd_kill))
    app.add_handler(CommandHandler("kill_scenarios", cmd_kill))
    app.add_handler(CommandHandler("ciclo", cmd_ciclo))
    app.add_handler(CommandHandler("ciclo_update", cmd_ciclo_update))
    app.add_handler(CommandHandler("pnl", cmd_pnl))
    app.add_handler(CommandHandler("log", cmd_log))

    log.info("Fondo Black Cat bot starting (Round 3)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
