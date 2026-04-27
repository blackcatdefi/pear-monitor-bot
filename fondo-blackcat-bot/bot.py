"""Fondo Black Cat — Telegram bot entry point.

Runs python-telegram-bot v21 (commands) + Telethon userbot (channel reads)
+ APScheduler (alert loop + intel processor) in the same asyncio event loop.

Round 16 additions:
    - commands_registry.COMMANDS as single source of truth
    - sync_commands_with_telegram on startup → BotFather autocomplete
    - /reload_commands manual sync
    - /version, /errors, /metrics, /test_alerts (admin/debug)
    - errors_log persistence + with_error_logging decorator on every handler
    - throttle on /reporte (60s per user)
    - aiohttp /health endpoint for Railway probes
    - daily SQLite backup + weekly cleanup
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import BotCommand as TGBotCommand
from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes

from commands_registry import (
    COMMANDS,
    render_start_menu,
    telegram_command_payload,
    validate_commands_match_handlers,
)
from config import (
    ENABLE_ALERTS,
    POLL_INTERVAL_MIN,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from modules.alerts import run_alert_cycle
from modules.analysis import (
    generate_report,
    generate_thesis_check,
    _load_thesis,
    load_tesis_latest,
)
from modules.errors_log import (
    cleanup_old as errors_cleanup,
    format_recent as format_recent_errors,
    with_error_logging,
)
from modules.health_server import start_health_server, stop_health_server
from modules.hyperlend import fetch_all_hyperlend, fetch_reserve_rates
from modules.kill_scenarios import compute_kill_scenarios
from modules.llm_providers import format_provider_status
from modules.market import fetch_market_data
from modules.metrics import format_metrics
from modules.portfolio import fetch_all_wallets, fetch_all_recent_fills, get_spot_price
from modules.sqlite_backup import backup_sqlite, cleanup_sqlite_weekly
from modules.telegram_intel import (
    fetch_telegram_intel,
    get_client as get_telethon,
    scan_telegram_unread,
    stop_client as stop_telethon,
)
from modules.throttle import throttle
from modules.unlocks import fetch_unlocks
from modules.bounce_tech import detect_closes as bt_detect_closes, fetch_bounce_tech
from modules.gmail_intel import scan_gmail_unread
from modules.version_info import format_version_block
from modules.x_intel import (
    cache_banner_for_report,
    debug_x_status,
    fetch_x_intel,
    format_intel_sources,
    format_x_costos,
    format_x_status,
    get_cache_state,
    get_cached_timeline,
    poll_and_cache_timeline,
    X_SCHEDULER_ENABLED,
)
from modules.flywheel import compute_flywheel
from modules.liq_calc import compute_liq_matrix
from fund_state import BCD_DCA_PLAN
from modules.cycle_trade import (
    apply_cycle_update,
    parse_cycle_update_args,
    render_cycle_status,
)
from modules.intel_memory import format_intel_summary, cleanup_old as intel_cleanup, get_unprocessed_count
from modules.intel_processor import process_pending_intel
from modules import pnl_tracker, position_log
# Round 17 modules
from modules.status_quick import build_status_block
from modules.macro_calendar import (
    add_event as cal_add_event,
    check_and_dispatch_alerts as cal_check_alerts,
    format_calendar as cal_format,
    parse_add_event_args as cal_parse_args,
    remove_event as cal_remove_event,
    seed_initial_events as cal_seed,
)
from modules.fund_state_reconciler import (
    format_reconcile_report,
    reconcile_fund_state,
    scheduled_reconcile,
)
from modules.basket_killer import (
    evaluate_all as kill_evaluate_all,
    format_kill_status,
    scheduled_check as kill_scheduled_check,
)
from modules.rates_monitor import scheduled_check as rates_scheduled_check
from modules.pretrade_checklist import build_pretrade_checklist
from modules.intel_search import format_search_results, search_intel
from modules.exports import export_dispatch
from modules.weekly_summary import scheduled_summary as weekly_scheduled_summary
from templates.formatters import format_hf, format_quick_positions
from templates.timeline import format_timeline
from utils.security import authorized
from utils.telegram import send_bot_message, send_long_message


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s \u2014 %(message)s",
)
log = logging.getLogger("fondo-blackcat")


# Persistent keyboard — dynamic, derived from COMMANDS so adding a new command
# auto-adds it here too. Built in pairs per row, prioritising core/trading.
def _build_main_keyboard() -> ReplyKeyboardMarkup:
    priority = ["core", "trading", "intel", "admin", "debug"]
    ordered: list[str] = []
    seen: set[str] = set()
    for cat in priority:
        for c in COMMANDS:
            if c.category == cat and c.command not in seen:
                if c.command in ("help",):
                    continue
                ordered.append(c.command)
                seen.add(c.command)

    rows: list[list[KeyboardButton]] = []
    pair: list[KeyboardButton] = []
    for cmd in ordered:
        pair.append(KeyboardButton(f"/{cmd}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


MAIN_KEYBOARD = _build_main_keyboard()

# Runtime state for /alertas toggle
_alerts_enabled = {"value": ENABLE_ALERTS}

# Set to False if Telethon fails to init — commands skip channel intel gracefully
_telethon_ok = True


# ─── Commands ────────────────────────────────────────────────────────────────


@authorized
@with_error_logging
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(render_start_menu(), reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_posiciones(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("\u23f3 Snapshot...", reply_markup=MAIN_KEYBOARD)
    wallets, hl, bt, market, recent_fills = await asyncio.gather(
        fetch_all_wallets(),
        fetch_all_hyperlend(),
        fetch_bounce_tech(),
        fetch_market_data(),
        fetch_all_recent_fills(hours=24),
    )

    # Detect Bounce Tech position closes
    bt_closes = bt_detect_closes(bt)
    for close in bt_closes:
        close_msg = (
            f"\U0001f514 Bounce Tech {close['direction']} {close['asset']} "
            f"{close['leverage']} CERRADA.\n"
            f"\u00daltimo valor registrado: ${close['last_value_usd']:,.2f}"
        )
        await update.message.reply_text(close_msg, reply_markup=MAIN_KEYBOARD)
        position_log.append(
            kind="CLOSE",
            message=f"Bounce Tech {close['direction']} {close['asset']} {close['leverage']} closed. Last value: ${close['last_value_usd']:,.2f}",
            asset=close["asset"],
            amount_usd=0,
            wallet_label="Bounce Tech",
        )

    await send_long_message(
        update,
        format_quick_positions(
            wallets, hl,
            bounce_tech=bt,
            recent_fills=recent_fills,
            market=market,
        ),
        reply_markup=MAIN_KEYBOARD,
    )


@authorized
@with_error_logging
async def cmd_hf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    hl = await fetch_all_hyperlend()
    await update.message.reply_text(format_hf(hl), reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
@throttle(min_interval_s=60, key_prefix="cmd_reporte")
async def cmd_reporte(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reporte TODO-EN-UNO: timeline X + posiciones + análisis LLM.

    Round 16: throttled at 60s/user to avoid stacking concurrent runs.
    """
    await update.message.reply_text(
        "\u23f3 Generando reporte completo: timeline + posiciones + an\u00e1lisis (30-90s)...",
        reply_markup=MAIN_KEYBOARD,
    )

    portfolio, hl, market, unlocks, x_intel, gmail_intel, bt, recent_fills = await asyncio.gather(
        fetch_all_wallets(),
        fetch_all_hyperlend(),
        fetch_market_data(),
        fetch_unlocks(),
        fetch_x_intel(hours=48, caller="reporte", app=context.application),
        scan_gmail_unread(),
        fetch_bounce_tech(),
        fetch_all_recent_fills(hours=24),
    )

    if _telethon_ok:
        intel_legacy, intel_unread = await asyncio.gather(
            fetch_telegram_intel(hours=24),
            scan_telegram_unread(max_per_dialog=100),
        )
    else:
        intel_legacy = {"status": "error", "error": "telethon_disabled"}
        intel_unread = {"status": "error", "error": "telethon_disabled"}

    # ─── Sección 1: Timeline X (48h) ─────────────────────────────────────────
    x_intel_ok = isinstance(x_intel, dict) and x_intel.get("status") == "ok"
    x_intel_fallback_note = ""

    if not x_intel_ok:
        cached = get_cached_timeline()
        if cached and cached.get("status") == "ok":
            cs = get_cache_state()
            last_ok = cs.get("last_success_at") or "—"
            live_err = ""
            if isinstance(x_intel, dict):
                live_err = str(x_intel.get("error") or "")[:200]
            x_intel_fallback_note = (
                f"\u26a0\ufe0f Live API fall\u00f3: {live_err}\n"
                f"Mostrando cache scheduler (last success UTC: {last_ok}).\n"
            )
            x_intel = cached
            x_intel_ok = True

    if x_intel_ok:
        timeline_text = format_timeline(x_intel, top_n=40)
        banner = cache_banner_for_report()
        header = (
            "\U0001f4e1 TIMELINE X \u2014 48H\n"
            + ("\u2500" * 30) + "\n"
            + banner + "\n\n"
        )
        if x_intel_fallback_note:
            header = (
                "\U0001f4e1 TIMELINE X \u2014 48H (cache fallback)\n"
                + ("\u2500" * 30) + "\n"
                + banner + "\n"
                + x_intel_fallback_note + "\n"
            )
        await send_long_message(
            update,
            header + timeline_text,
            reply_markup=MAIN_KEYBOARD,
        )

    # ─── Sección 2: Posiciones ──────────────────────────────────────────────────
    positions_text = format_quick_positions(
        portfolio, hl,
        bounce_tech=bt,
        recent_fills=recent_fills,
        market=market,
    )
    await send_long_message(
        update,
        "\U0001f4bc POSICIONES\n" + ("\u2500" * 30) + "\n\n" + positions_text,
        reply_markup=MAIN_KEYBOARD,
    )

    # ─── Sección 3: Análisis LLM (Sonnet primary) ───────────────────────────────
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
        "\U0001f9e0 AN\u00c1LISIS COMPLETO\n" + ("\u2500" * 30) + "\n\n" + report,
        reply_markup=MAIN_KEYBOARD,
    )
    if thesis_update:
        await send_long_message(update, thesis_update, reply_markup=MAIN_KEYBOARD)

    if not x_intel_ok:
        live_err = ""
        if isinstance(x_intel, dict):
            live_err = str(x_intel.get("error") or "")[:300]
        await update.message.reply_text(
            "\u2139\ufe0f Timeline X no disponible (live + cache fallaron).\n"
            f"   Error live: {live_err or '—'}\n"
            "   Diagn\u00f3stico: correr /debug_x para probe en vivo (bypass cooldown).",
            reply_markup=MAIN_KEYBOARD,
        )


@authorized
@with_error_logging
async def cmd_tesis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _load_thesis()
    has_components = bool(state.get("components"))

    if has_components:
        from modules.analysis import _thesis_context
        text = _thesis_context(state)
    else:
        content, last_modified = load_tesis_latest()
        if content:
            sep = "\u2500" * 30
            last_mod = last_modified or "?"
            text = (
                "\U0001f4ca TESIS (snapshot plain-text \u2014 fallback)\n"
                f"\u00daltima actualizaci\u00f3n: {last_mod}\n"
                f"{sep}\n\n{content}"
            )
        else:
            await update.message.reply_text(
                "\U0001f4ca No hay tesis guardada a\u00fan. Ejecutar /reporte primero.",
                reply_markup=MAIN_KEYBOARD,
            )
            return

    unprocessed = get_unprocessed_count()
    if unprocessed > 0:
        text += f"\n\n\u23f3 {unprocessed} items de intel pendientes de procesar"

    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_timeline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "\u23f3 Leyendo \u00faltimas 48h de tu X list...",
        reply_markup=MAIN_KEYBOARD,
    )
    x_intel = await fetch_x_intel(hours=48, caller="timeline", app=context.application)
    banner = cache_banner_for_report()
    if isinstance(x_intel, dict) and x_intel.get("status") != "ok":
        cached = get_cached_timeline()
        if cached and cached.get("status") == "ok":
            prefix = (
                f"\u26a0\ufe0f Live fall\u00f3: {x_intel.get('error','')[:200]}\n"
                f"{banner}\n"
                + ("\u2500" * 30) + "\n\n"
            )
            text = prefix + format_timeline(cached, top_n=40)
        else:
            text = format_timeline(x_intel, top_n=40)
    else:
        text = banner + "\n" + ("\u2500" * 30) + "\n\n" + format_timeline(x_intel, top_n=40)
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_alertas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _alerts_enabled["value"] = not _alerts_enabled["value"]
    estado = "ON \u2705" if _alerts_enabled["value"] else "OFF \U0001f6ab"
    await update.message.reply_text(f"Alertas autom\u00e1ticas: {estado}", reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_intel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
@with_error_logging
async def cmd_debug_x(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = await debug_x_status()
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_x_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = await format_x_status()
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_costos_x(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = await format_x_costos()
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_intel_sources(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "\u23f3 Leyendo la list X \u2014 top 20 cuentas \u00faltimas 24h...",
        reply_markup=MAIN_KEYBOARD,
    )
    text = await format_intel_sources(hours=24)
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_providers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = format_provider_status()
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_flywheel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("\u23f3 Calculando flywheel pair trade...", reply_markup=MAIN_KEYBOARD)
    text = await compute_flywheel()
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_debug_flywheel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if os.getenv("DEBUG_MODE", "").strip().lower() != "true":
        await update.message.reply_text(
            "\u26a0\ufe0f /debug_flywheel est\u00e1 deshabilitado. Set "
            "DEBUG_MODE=true en Railway vars para activar.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    await update.message.reply_text(
        "\u23f3 Dump de reservas HyperLend...", reply_markup=MAIN_KEYBOARD
    )
    payload = await fetch_reserve_rates(force=True)
    if payload.get("status") != "ok":
        err = payload.get("error") or "unknown"
        await send_long_message(
            update, f"\u274c RPC read fall\u00f3: {err}",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    rates_map = payload.get("rates") or {}
    ts = payload.get("fetched_at_iso") or "—"
    lines: list[str] = []
    lines.append("\U0001f50d DEBUG /flywheel \u2014 HyperLend reserves raw dump")
    lines.append(f"Fetched: {ts}  (cache bypass)")
    lines.append(f"Reserves: {len(rates_map)}")
    lines.append("\u2500" * 40)
    items = list(rates_map.values())
    items.sort(key=lambda v: (bool(v.get("deprecated")), float(v.get("apy_borrow") or 0.0)))
    for v in items:
        sym = v.get("symbol") or "?"
        chain_sym = v.get("chain_symbol") or sym
        addr = v.get("asset") or ""
        addr_short = (addr[:10] + "\u2026" + addr[-4:]) if addr else ""
        apr_b = float(v.get("apr_borrow") or 0.0) * 100
        apy_b = float(v.get("apy_borrow") or 0.0) * 100
        apr_s = float(v.get("apr_supply") or 0.0) * 100
        apy_s = float(v.get("apy_supply") or 0.0) * 100
        dep = "\U0001f6ab DEP" if v.get("deprecated") else "\u2705 active"
        chain_tag = f" chain='{chain_sym}'" if chain_sym != sym else ""
        lines.append(
            f"{dep}  {sym:<10}{chain_tag}\n"
            f"    addr: {addr_short}\n"
            f"    borrow: {apr_b:6.2f}% APR / {apy_b:6.2f}% APY\n"
            f"    supply: {apr_s:6.2f}% APR / {apy_s:6.2f}% APY"
        )
    await send_long_message(update, "\n".join(lines), reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_liqcalc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("\u23f3 Calculando matriz de liquidaci\u00f3n...", reply_markup=MAIN_KEYBOARD)
    text = await compute_liq_matrix()
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_kill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("\u23f3 Evaluando kill scenarios...", reply_markup=MAIN_KEYBOARD)
    text = await compute_kill_scenarios()
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_ciclo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = render_cycle_status()
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_ciclo_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    try:
        status, entry = parse_cycle_update_args(args)
    except ValueError as exc:
        await update.message.reply_text(f"\u274c {exc}", reply_markup=MAIN_KEYBOARD)
        return

    await update.message.reply_text(
        f"\u23f3 Aplicando /ciclo_update STATUS={status}"
        + (f" entry=${entry:,.2f}" if entry is not None else "")
        + "...",
        reply_markup=MAIN_KEYBOARD,
    )
    result = apply_cycle_update(status, entry)
    icon = "\u2705" if result.get("ok") else "\u274c"
    pushed = "pushed" if result.get("pushed") else "NO pushed"
    text = (
        f"{icon} /ciclo_update STATUS={status}\n"
        f"   wrote={result.get('wrote')} · {pushed}\n\n"
        f"{result.get('message', '')}"
    )
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_dca(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from modules.alerts import _dca_alerted_within_window
    from modules.alerts import _load_state as load_alert_state
    state = load_alert_state()
    lines = ["\U0001f3af PLAN DCA TRAMIFICADO BCD", "\u2500" * 40]
    for asset in ("BTC", "ETH", "HYPE"):
        plan = BCD_DCA_PLAN.get(asset) or {}
        tranches = plan.get("tranches") or []
        if not tranches:
            continue
        try:
            px = await get_spot_price(asset)
        except Exception:
            px = None
        px_str = f"${px:,.2f}" if px else "(sin precio)"
        lines.append(f"\n{asset} — spot {px_str}")
        for idx, t in enumerate(tranches):
            rng = t.get("range") or [0, 0]
            low, high = float(rng[0]), float(rng[1])
            in_zone = (px is not None) and (low <= px <= high)
            alerted_key = f"dca_{asset}_{idx}_alerted_at"
            already_alerted = _dca_alerted_within_window(state, alerted_key)
            status = "pending"
            tag = ""
            if in_zone and already_alerted:
                status = "alerted"
                tag = " \U0001f514"
            elif in_zone:
                status = "IN ZONE"
                tag = " \u2705"
            lines.append(
                f"  [{idx+1}] {t.get('pct', 0):>3}% @ ${low:,.0f}-${high:,.0f} "
                f"\u2192 {status}{tag}"
            )
        if asset == "ETH":
            flip = plan.get("debt_flip_range")
            if flip and len(flip) == 2:
                in_flip = (px is not None) and (float(flip[0]) <= px <= float(flip[1]))
                tag = " \u2705 IN ZONE" if in_flip else ""
                lines.append(
                    f"  debt_flip (UETH\u2192stable): "
                    f"${float(flip[0]):,.0f}-${float(flip[1]):,.0f}{tag}"
                )
    lines.append("")
    lines.append(f"Cycle bottom esperado: {BCD_DCA_PLAN.get('cycle_bottom_expected', '?')}")
    sources = ", ".join(BCD_DCA_PLAN.get("sources") or [])
    if sources:
        lines.append(f"Fuentes: {sources}")
    lines.append("")
    lines.append(
        "Alertas autom\u00e1ticas edge-triggered cada "
        f"{POLL_INTERVAL_MIN}min si el precio entra en un range. Rearm 24h."
    )
    await send_long_message(update, "\n".join(lines), reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
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
                f"\u2705 PnL event #{row_id} registered ({params['category']} "
                f"{params['asset']} ${params['amount_usd']:.2f}).",
                reply_markup=MAIN_KEYBOARD,
            )
        except ValueError as exc:
            await update.message.reply_text(f"\u274c {exc}", reply_markup=MAIN_KEYBOARD)
        return

    text = pnl_tracker.build_summary()
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if args and args[0].lower() == "add":
        try:
            params = position_log.parse_manual_add(args[1:])
            row_id = position_log.append(**params)
            await update.message.reply_text(
                f"\u2705 Log entry #{row_id} agregada ({params['kind']}).",
                reply_markup=MAIN_KEYBOARD,
            )
        except ValueError as exc:
            await update.message.reply_text(f"\u274c {exc}", reply_markup=MAIN_KEYBOARD)
        return

    entries = position_log.last_n(20)
    text = position_log.format_log(entries)
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


# ─── Round 16 new commands ───────────────────────────────────────────────────


@authorized
@with_error_logging
async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Round 16: show commit SHA, deploy ID, uptime, providers status."""
    text = format_version_block(commands_count=len(COMMANDS))
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_errors(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Round 16: last 20 captured errors from errors_log SQLite table."""
    text = format_recent_errors(limit=20)
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_metrics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Round 16: 24h health dashboard (errors, llm cost, x api, db size)."""
    text = format_metrics()
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_test_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Round 16: fire a test alert through the alerts pipeline."""
    msg = (
        "\U0001f9ea TEST ALERT \u2014 sistema de alertas operativo.\n"
        f"Timestamp UTC: {datetime.now(timezone.utc).isoformat()}\n"
        "Si recibís este mensaje, el canal funciona OK."
    )
    if TELEGRAM_CHAT_ID:
        await send_bot_message(context.application.bot, TELEGRAM_CHAT_ID, msg)
    await update.message.reply_text("\u2705 Test alert enviado.", reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_reload_commands(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Round 16: re-sync command list with Telegram (BotFather)."""
    n = await sync_commands_with_telegram(context.application)
    await update.message.reply_text(
        f"\U0001f504 Comandos re-sincronizados con Telegram.\n"
        f"Total registrados: {n} (visibles en autocompletado).",
        reply_markup=MAIN_KEYBOARD,
    )


# ─── Round 17 handlers ──────────────────────────────────────────────────────


@authorized
@with_error_logging
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R17 — Quick status (no LLM, no X API, <3s)."""
    text = await build_status_block()
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_reconcile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R17 — Reconciliar fund_state vs on-chain."""
    await update.message.reply_text("⏳ Reconciliando fund_state vs on-chain...", reply_markup=MAIN_KEYBOARD)
    discrepancies = await reconcile_fund_state()
    text = format_reconcile_report(discrepancies)
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R17 — Próximos catalysts del macro calendar."""
    text = cal_format()
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_add_event(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R17 — Agregar evento al macro calendar.

    Uso: /add_event <event_id> <YYYY-MM-DDTHH:MMZ> <category> <impact> | <name>
    """
    raw = " ".join(context.args or [])
    if not raw.strip():
        await update.message.reply_text(
            "Uso: /add_event <event_id> <YYYY-MM-DDTHH:MMZ> <category> <impact> | <name>\n"
            "Ej: /add_event fomc_may7 2026-05-07T18:00Z fomc high | FOMC May rate decision",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    try:
        ev = cal_parse_args(raw)
        cal_add_event(ev)
        await update.message.reply_text(
            f"✅ Evento agregado: {ev.event_id} → {ev.timestamp_utc.isoformat()}",
            reply_markup=MAIN_KEYBOARD,
        )
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(f"❌ {exc}", reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_remove_event(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R17 — Borrar evento del macro calendar."""
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Uso: /remove_event <event_id>",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    event_id = args[0].strip()
    ok = cal_remove_event(event_id)
    if ok:
        await update.message.reply_text(f"🗑 {event_id} borrado.", reply_markup=MAIN_KEYBOARD)
    else:
        await update.message.reply_text(f"⚠️ {event_id} no encontrado.", reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_kill_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R17 — Estado de los 5 kill triggers."""
    await update.message.reply_text("⏳ Evaluando kill triggers...", reply_markup=MAIN_KEYBOARD)
    results = await kill_evaluate_all()
    text = format_kill_status(results)
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_intel_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R17 — Búsqueda full-text en intel_memory."""
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Uso: /intel_search <keyword>\n"
            "Ej: /intel_search hormuz | /intel_search BTC ATH",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    query = " ".join(args).strip()
    results = await asyncio.to_thread(search_intel, query, 15)
    text = format_search_results(query, results)
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R17 — Export CSV.

    Uso: /export <tipo> <periodo>
        tipos: fills, pnl, positions, intel, errors
        periodos: 7d, 30d, 90d, ytd, all
    """
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Uso: /export <tipo> <periodo>\n"
            "  tipos: fills, pnl, positions, intel, errors\n"
            "  periodos: 7d, 30d, 90d, ytd, all\n"
            "Ej: /export fills 30d",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    tipo, periodo = args[0].strip().lower(), args[1].strip().lower()
    try:
        path, count = await asyncio.to_thread(export_dispatch, tipo, periodo)
    except ValueError as exc:
        await update.message.reply_text(f"❌ {exc}", reply_markup=MAIN_KEYBOARD)
        return
    except Exception:  # noqa: BLE001
        log.exception("/export failed")
        await update.message.reply_text("❌ Export fallido — ver /errors.", reply_markup=MAIN_KEYBOARD)
        return

    caption = f"📊 {tipo} ({periodo}) — {count} filas"
    try:
        with open(path, "rb") as f:
            await update.message.reply_document(document=f, filename=os.path.basename(path), caption=caption)
    except Exception:  # noqa: BLE001
        log.exception("send_document failed")
        await update.message.reply_text(
            f"{caption}\n📁 Archivo en server: {path}",
            reply_markup=MAIN_KEYBOARD,
        )


@authorized
@with_error_logging
async def cmd_pretrade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R17 — Checklist pre-trade de 5 puntos."""
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Uso: /pretrade <SYMBOL>\nEj: /pretrade DYDX",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    symbol = args[0]
    await update.message.reply_text(f"⏳ Pre-trade {symbol.upper()}...", reply_markup=MAIN_KEYBOARD)
    text = await build_pretrade_checklist(symbol)
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


# ─── Scheduler jobs ──────────────────────────────────────────────────────────


async def _alert_job(application: Application) -> None:
    if not _alerts_enabled["value"]:
        return
    try:
        await run_alert_cycle(application.bot)
    except Exception:  # noqa: BLE001
        log.exception("Alert cycle failed")


async def _intel_processor_job() -> None:
    try:
        count = await process_pending_intel(limit=50)
        if count > 0:
            log.info("Intel processor job completed: %d items processed", count)
    except Exception:  # noqa: BLE001
        log.exception("Intel processor job failed")


async def _x_timeline_cache_job(application: Application | None = None) -> None:
    try:
        await poll_and_cache_timeline(app=application)
    except Exception:  # noqa: BLE001
        log.exception("X timeline cache job failed")


async def _backup_job() -> None:
    """Round 16: nightly SQLite backup."""
    try:
        result = await backup_sqlite()
        log.info("Backup job: %s", result)
    except Exception:  # noqa: BLE001
        log.exception("Backup job failed")


async def _weekly_cleanup_job() -> None:
    """Round 16: weekly purge of old rows + VACUUM."""
    try:
        deleted = cleanup_sqlite_weekly(days=90)
        errs = errors_cleanup(days=90)
        log.info("Weekly cleanup: rows=%s errors_log=%s", deleted, errs)
    except Exception:  # noqa: BLE001
        log.exception("Weekly cleanup failed")


# ─── Round 17 scheduler jobs ────────────────────────────────────────────────


async def _macro_calendar_job(application: Application) -> None:
    """R17: every 1 min — fire T-24h/T-2h/T-30m alerts for upcoming events."""
    if os.getenv("MACRO_CALENDAR_ENABLED", "true").strip().lower() == "false":
        return
    try:
        await cal_check_alerts(application.bot)
    except Exception:  # noqa: BLE001
        log.exception("macro_calendar job failed")


async def _reconcile_job(application: Application) -> None:
    """R17: every 15 min — auto-detect PHANTOM/GHOST_BASKET, alert + (opt) auto-commit."""
    if os.getenv("AUTO_RECONCILE_ENABLED", "true").strip().lower() == "false":
        return
    try:
        await scheduled_reconcile(application.bot)
    except Exception:  # noqa: BLE001
        log.exception("reconcile job failed")


async def _kill_triggers_job(application: Application) -> None:
    """R17: every 5 min — evaluate 5 kill triggers, edge-trigger Telegram alerts."""
    if os.getenv("KILL_TRIGGERS_ENABLED", "true").strip().lower() == "false":
        return
    try:
        await kill_scheduled_check(application.bot)
    except Exception:  # noqa: BLE001
        log.exception("kill triggers job failed")


async def _rates_monitor_job(application: Application) -> None:
    """R17: every 30 min — UETH APY + funding + HF thresholds."""
    if os.getenv("RATES_MONITOR_ENABLED", "true").strip().lower() == "false":
        return
    try:
        await rates_scheduled_check(application.bot)
    except Exception:  # noqa: BLE001
        log.exception("rates monitor job failed")


async def _weekly_summary_job(application: Application) -> None:
    """R17: Sunday 18:00 UTC — weekly performance summary."""
    try:
        await weekly_scheduled_summary(application.bot)
    except Exception:  # noqa: BLE001
        log.exception("weekly summary job failed")


# ─── BotFather sync ──────────────────────────────────────────────────────────


async def sync_commands_with_telegram(application: Application) -> int:
    """Push the canonical command list to BotFather so they appear in autocomplete.

    Returns the number of commands registered. Idempotent — Telegram replaces
    the list each call. Failure is logged but never blocks startup.
    """
    payload = telegram_command_payload()
    tg_commands = [TGBotCommand(cmd, desc) for cmd, desc in payload]
    try:
        await application.bot.set_my_commands(tg_commands)
        log.info("\u2705 Sync %d comandos con Telegram (BotFather autocomplete)", len(tg_commands))
        return len(tg_commands)
    except Exception as exc:  # noqa: BLE001
        log.exception("\u274c set_my_commands fall\u00f3: %s", exc)
        return 0


# ─── Lifecycle hooks ─────────────────────────────────────────────────────────


async def post_init(application: Application) -> None:
    global _telethon_ok

    # Round 16: validate handler/command coherence (loud warning, not fatal)
    registered_names = {h.command for h in COMMANDS}  # purely structural — no asserts
    # The actual runtime check happens via main(): we record handler names there.
    issues = application.bot_data.get("validate_issues", [])
    if issues:
        log.warning("⚠️ commands_registry validation issues: %s", issues)

    # Round 16: sync to BotFather (autocomplete bar)
    if os.getenv("COMMANDS_AUTO_SYNC", "true").strip().lower() != "false":
        await sync_commands_with_telegram(application)
    else:
        log.info("COMMANDS_AUTO_SYNC=false → skipping BotFather sync")

    # Round 16: start aiohttp /health server (R17: also serves /dashboard)
    try:
        await start_health_server()
    except Exception:  # noqa: BLE001
        log.exception("health server start failed (non-fatal)")

    # Round 17: seed macro_calendar with the 7 pre-roadmap events on first boot
    try:
        cal_seed()
    except Exception:  # noqa: BLE001
        log.exception("macro_calendar seed failed (non-fatal)")

    # Telethon
    try:
        client = await get_telethon()
        if client is None:
            log.warning("Telethon NOT initialized \u2014 /reporte will run without channel intel.")
            _telethon_ok = False
        else:
            log.info("Telethon client connected.")
    except Exception:
        log.exception("Telethon init failed \u2014 Telegram intel disabled")
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
        scheduler.add_job(
            _intel_processor_job,
            "interval",
            minutes=30,
            id="intel_processor",
            max_instances=1,
            coalesce=True,
        )
        if X_SCHEDULER_ENABLED:
            x_cache_hours = float(os.getenv("X_CACHE_INTERVAL_HOURS", "6"))
            scheduler.add_job(
                _x_timeline_cache_job,
                "interval",
                hours=x_cache_hours,
                args=[application],
                id="x_timeline_cache",
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now(timezone.utc) + timedelta(seconds=30),
            )
            log.info(
                "X timeline cache scheduler ENABLED — every %.1fh (X_SCHEDULER_ENABLED=true)",
                x_cache_hours,
            )
        else:
            log.info(
                "X timeline cache scheduler DISABLED (Round 15 default). "
                "Set X_SCHEDULER_ENABLED=true to re-enable."
            )

        # Round 16: nightly SQLite backup at 03:00 UTC
        scheduler.add_job(
            _backup_job,
            "cron",
            hour=3,
            minute=0,
            id="sqlite_backup",
            max_instances=1,
            coalesce=True,
        )
        # Round 16: weekly cleanup Sundays at 04:00 UTC
        scheduler.add_job(
            _weekly_cleanup_job,
            "cron",
            day_of_week="sun",
            hour=4,
            minute=0,
            id="sqlite_cleanup",
            max_instances=1,
            coalesce=True,
        )

        # ─── Round 17 jobs ───────────────────────────────────────────────
        # Macro calendar alerts T-24h/T-2h/T-30m — every 1 min
        scheduler.add_job(
            _macro_calendar_job,
            "interval",
            minutes=1,
            args=[application],
            id="macro_calendar",
            max_instances=1,
            coalesce=True,
        )
        # Auto-reconcile fund_state vs on-chain — every 15 min
        scheduler.add_job(
            _reconcile_job,
            "interval",
            minutes=15,
            args=[application],
            id="fund_state_reconcile",
            max_instances=1,
            coalesce=True,
        )
        # Kill triggers (BTC>82k 4h, DCA zone, HF<1.10, basket DD<-2k, UETH>10%) — 5 min
        scheduler.add_job(
            _kill_triggers_job,
            "interval",
            minutes=5,
            args=[application],
            id="kill_triggers",
            max_instances=1,
            coalesce=True,
        )
        # Rates monitor (UETH APY + funding + HF) — every 30 min
        scheduler.add_job(
            _rates_monitor_job,
            "interval",
            minutes=30,
            args=[application],
            id="rates_monitor",
            max_instances=1,
            coalesce=True,
        )
        # Weekly summary — Sunday 18:00 UTC
        scheduler.add_job(
            _weekly_summary_job,
            "cron",
            day_of_week="sun",
            hour=18,
            minute=0,
            args=[application],
            id="weekly_summary",
            max_instances=1,
            coalesce=True,
        )

        scheduler.start()
        application.bot_data["scheduler"] = scheduler
        log.info(
            "Scheduler started: alerts %dmin, intel 30min, X %s, backup 03:00 UTC, cleanup Sun 04:00 UTC. "
            "R17: macro_cal 1min, reconcile 15min, kill 5min, rates 30min, weekly_summary Sun 18:00 UTC.",
            POLL_INTERVAL_MIN,
            "ON" if X_SCHEDULER_ENABLED else "OFF",
        )

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
    try:
        await stop_health_server()
    except Exception:  # noqa: BLE001
        log.exception("health server stop failed")
    await stop_telethon()


# ─── Main ────────────────────────────────────────────────────────────────────


# Mapping (command → handler) — used both for registration and validation.
HANDLER_MAP = {
    "start": cmd_start,
    "help": cmd_start,
    "posiciones": cmd_posiciones,
    "hf": cmd_hf,
    "reporte": cmd_reporte,
    "tesis": cmd_tesis,
    "timeline": cmd_timeline,
    "alertas": cmd_alertas,
    "intel": cmd_intel,
    "debug_x": cmd_debug_x,
    "x_status": cmd_x_status,
    "costos_x": cmd_costos_x,
    "intel_sources": cmd_intel_sources,
    "providers": cmd_providers,
    "flywheel": cmd_flywheel,
    "debug_flywheel": cmd_debug_flywheel,
    "liqcalc": cmd_liqcalc,
    "kill": cmd_kill,
    "ciclo": cmd_ciclo,
    "ciclo_update": cmd_ciclo_update,
    "dca": cmd_dca,
    "pnl": cmd_pnl,
    "log": cmd_log,
    # Round 16
    "version": cmd_version,
    "errors": cmd_errors,
    "metrics": cmd_metrics,
    "test_alerts": cmd_test_alerts,
    "reload_commands": cmd_reload_commands,
    # Round 17
    "status": cmd_status,
    "reconcile": cmd_reconcile,
    "calendar": cmd_calendar,
    "add_event": cmd_add_event,
    "remove_event": cmd_remove_event,
    "kill_status": cmd_kill_status,
    "intel_search": cmd_intel_search,
    "export": cmd_export,
    "pretrade": cmd_pretrade,
}


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

    # Register every handler from the mapping
    registered_handler_names: set[str] = set()
    for cmd_name, handler in HANDLER_MAP.items():
        app.add_handler(CommandHandler(cmd_name, handler))
        # Strip wrapper layers to get the actual function name
        underlying = handler
        while hasattr(underlying, "__wrapped__"):
            underlying = underlying.__wrapped__
        registered_handler_names.add(underlying.__name__)

    # Validate against COMMANDS registry (loud warning if drift)
    issues = validate_commands_match_handlers(registered_handler_names)
    if issues:
        log.warning("⚠️ commands_registry drift detected: %s", issues)
    app.bot_data["validate_issues"] = issues

    log.info("Fondo Black Cat bot starting (Round 17) — %d handlers, %d in registry",
             len(HANDLER_MAP), len(COMMANDS))
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
