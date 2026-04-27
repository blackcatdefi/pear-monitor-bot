"""Fondo Black Cat — Telegram bot entry point.

Runs python-telegram-bot v21 (commands) + Telethon userbot (channel reads)
+ APScheduler (alert loop + intel processor) in the same asyncio event loop.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

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
from modules.analysis import (
    generate_report,
    generate_thesis_check,
    _load_thesis,
    load_tesis_latest,
)
from modules.hyperlend import fetch_all_hyperlend, fetch_reserve_rates
from modules.kill_scenarios import compute_kill_scenarios
from modules.llm_providers import format_provider_status
from modules.market import fetch_market_data
from modules.portfolio import fetch_all_wallets, fetch_all_recent_fills, get_spot_price
from modules.telegram_intel import (
    fetch_telegram_intel,
    get_client as get_telethon,
    scan_telegram_unread,
    stop_client as stop_telethon,
)
from modules.unlocks import fetch_unlocks
from modules.bounce_tech import detect_closes as bt_detect_closes, fetch_bounce_tech
from modules.gmail_intel import scan_gmail_unread
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
from templates.formatters import format_hf, format_quick_positions
from templates.timeline import format_timeline
from utils.security import authorized
from utils.telegram import send_long_message


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s \u2014 %(message)s",
)
log = logging.getLogger("fondo-blackcat")


# Persistent keyboard — todos los comandos accesibles con un tap.
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("/reporte"), KeyboardButton("/posiciones")],
        [KeyboardButton("/flywheel"), KeyboardButton("/liqcalc")],
        [KeyboardButton("/timeline"), KeyboardButton("/tesis")],
        [KeyboardButton("/hf"), KeyboardButton("/kill")],
        [KeyboardButton("/ciclo"), KeyboardButton("/ciclo_update")],
        [KeyboardButton("/dca"), KeyboardButton("/pnl")],
        [KeyboardButton("/log"), KeyboardButton("/intel")],
        [KeyboardButton("/alertas"), KeyboardButton("/help")],
        [KeyboardButton("/providers"), KeyboardButton("/debug_x")],
        [KeyboardButton("/intel_sources"), KeyboardButton("/start")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

# Runtime state for /alertas toggle
_alerts_enabled = {"value": ENABLE_ALERTS}

# Set to False if Telethon fails to init — commands skip channel intel gracefully
_telethon_ok = True


# ─── Commands ────────────────────────────────────────────────────────────────


@authorized
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "\U0001f431\u200d\u2b1b Fondo Black Cat \u2014 analista personal\n\n"
        "Keyboard \u2014 todos los comandos:\n"
        "/reporte \u2014 TODO-EN-UNO: timeline + posiciones + an\u00e1lisis\n"
        "/posiciones \u2014 snapshot r\u00e1pido (wallets + HF)\n"
        "/flywheel \u2014 pair trade HL (LONG HYPE / SHORT UETH)\n"
        "/liqcalc \u2014 matriz liq HYPE \u00d7 deuda\n"
        "/timeline \u2014 timeline X 48h (tu X list)\n"
        "/tesis \u2014 estado de la tesis macro\n"
        "/hf \u2014 Health Factor de HyperLend\n"
        "/kill \u2014 kill scenarios de cada posici\u00f3n\n"
        "/ciclo \u2014 estado del Trade del Ciclo (Blofin, manual)\n"
        "/ciclo_update \u2014 abrir/cerrar Trade del Ciclo (edita fund_state.py)\n"
        "/dca \u2014 plan DCA tramificado BTC/ETH/HYPE + zona actual\n"
        "/pnl \u2014 realized PnL 7D / 30D / YTD\n"
        "/log \u2014 \u00faltimas 20 entradas del position log\n"
        "/intel \u2014 resumen de intel memory (\u00faltimas 24h)\n"
        "/providers \u2014 status de los LLM providers\n"
        "/debug_x \u2014 diagn\u00f3stico de conectividad X/Twitter\n"
        "/intel_sources \u2014 top 20 cuentas activas en la list X (24h)\n"
        "/alertas \u2014 toggle alertas autom\u00e1ticas (on/off)\n"
    )
    await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)


@authorized
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
async def cmd_hf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    hl = await fetch_all_hyperlend()
    await update.message.reply_text(format_hf(hl), reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_reporte(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reporte TODO-EN-UNO: timeline X + posiciones + an\u00e1lisis LLM.

    Emite 3 mensajes secuenciales:
    1. Timeline \u2014 top 40 tweets por engagement de las \u00faltimas 48h (tu X list curada)
    2. Posiciones \u2014 snapshot r\u00e1pido de wallets + HyperLend + Bounce Tech
    3. An\u00e1lisis \u2014 reporte completo generado por Sonnet (market + intel + tesis)
    """
    await update.message.reply_text(
        "\u23f3 Generando reporte completo: timeline + posiciones + an\u00e1lisis (30-90s)...",
        reply_markup=MAIN_KEYBOARD,
    )

    # Todos los fetches en paralelo (Telethon separado — puede estar deshabilitado).
    # Round 15: pass app to fetch_x_intel so the 75pct daily-cap alert can fire
    # via Telegram immediately after the live fetch records the call.
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
    # Round 13: fallback a cache scheduler cuando el live fetch hit cooldown.
    # Pre-Round 13 bug: si fetch_x_intel devolvía status=error (ej: internal
    # 4h cooldown recién disparado por el scheduler), /reporte directamente
    # saltaba la sección y mostraba el mensaje legacy de "Nitter/RSSHub".
    # Ahora: intentar cache antes de rendirse — /timeline ya hacía esto.
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
        # Round 15: always show cache-state banner so BCD knows whether the
        # timeline data is fresh or cached, regardless of how it was obtained.
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

    # Nota si timeline X no disponible (ni live ni cache)
    # Round 13: mensaje actualizado — ya no usamos Nitter/RSSHub, el error
    # legacy era confuso. Ahora apunta al Spend Cap / balance X API / /debug_x.
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
async def cmd_tesis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current thesis state from disk — no fresh API call.

    Primary source: thesis_state.json (structured, populated when the LLM
    returns parseable JSON after /reporte). Fallback: tesis_latest.md
    (plain-text snapshot written unconditionally by /reporte — this saves us
    when the structured JSON parse fails).
    """
    state = _load_thesis()
    has_components = bool(state.get("components"))

    if has_components:
        from modules.analysis import _thesis_context
        text = _thesis_context(state)
    else:
        # Fallback to plain-text snapshot
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
async def cmd_timeline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "\u23f3 Leyendo \u00faltimas 48h de tu X list...",
        reply_markup=MAIN_KEYBOARD,
    )
    x_intel = await fetch_x_intel(hours=48, caller="timeline", app=context.application)
    banner = cache_banner_for_report()
    # Fallback to cached timeline when live fetch fails (SpendCap, cooldown,
    # kill switch, daily cap reached).
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
async def cmd_alertas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _alerts_enabled["value"] = not _alerts_enabled["value"]
    estado = "ON \u2705" if _alerts_enabled["value"] else "OFF \U0001f6ab"
    await update.message.reply_text(f"Alertas autom\u00e1ticas: {estado}", reply_markup=MAIN_KEYBOARD)


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
async def cmd_x_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Round 15: live dashboard for X API mode + counters + cache state."""
    text = await format_x_status()
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_costos_x(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Round 15: cost dashboard with 7d / 30d breakdown by caller."""
    text = await format_x_costos()
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_intel_sources(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Top 20 most active accounts in the X list over the last 24h."""
    await update.message.reply_text(
        "\u23f3 Leyendo la list X \u2014 top 20 cuentas \u00faltimas 24h...",
        reply_markup=MAIN_KEYBOARD,
    )
    try:
        text = await format_intel_sources(hours=24)
    except Exception as exc:  # noqa: BLE001
        log.exception("intel_sources failed")
        text = f"\u274c /intel_sources fall\u00f3: {exc}"
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_providers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show LLM provider status dashboard with cost tracking."""
    text = format_provider_status()
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_flywheel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("\u23f3 Calculando flywheel pair trade...", reply_markup=MAIN_KEYBOARD)
    try:
        text = await compute_flywheel()
    except Exception as exc:  # noqa: BLE001
        log.exception("flywheel failed")
        text = f"\u274c /flywheel fall\u00f3: {exc}"
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_debug_flywheel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dump raw HyperLend reserve rates — diagnostic for /flywheel matching.

    Gated by DEBUG_MODE=true env var. Shows every reserve's canonical symbol,
    raw chain symbol (from on-chain symbol() call), underlying asset address,
    borrow/supply APR+APY, and deprecated flag. Use this when /flywheel
    reports unexpected "no disponible en pool" entries to verify the
    address→symbol map is in sync with the live protocol.
    """
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
    try:
        payload = await fetch_reserve_rates(force=True)
    except Exception as exc:  # noqa: BLE001
        log.exception("debug_flywheel fetch failed")
        await send_long_message(
            update, f"\u274c fetch_reserve_rates fall\u00f3: {exc}",
            reply_markup=MAIN_KEYBOARD,
        )
        return

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
    # Sort deprecated last, primary by APY ASC within each group
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
async def cmd_liqcalc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("\u23f3 Calculando matriz de liquidaci\u00f3n...", reply_markup=MAIN_KEYBOARD)
    try:
        text = await compute_liq_matrix()
    except Exception as exc:  # noqa: BLE001
        log.exception("liqcalc failed")
        text = f"\u274c /liqcalc fall\u00f3: {exc}"
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_kill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("\u23f3 Evaluando kill scenarios...", reply_markup=MAIN_KEYBOARD)
    try:
        text = await compute_kill_scenarios()
    except Exception as exc:  # noqa: BLE001
        log.exception("kill scenarios failed")
        text = f"\u274c /kill fall\u00f3: {exc}"
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_ciclo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trade del Ciclo status (manual Blofin, sin API)."""
    try:
        text = render_cycle_status()
    except Exception as exc:  # noqa: BLE001
        log.exception("ciclo failed")
        text = f"\u274c /ciclo fall\u00f3: {exc}"
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_ciclo_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Edit fund_state.TRADE_DEL_CICLO_* constants from Telegram.

    Usage examples:
        /ciclo_update OPEN 77000
        /ciclo_update CLOSED

    Writes to fund_state.py, commits with bot identity, and pushes to master
    so Railway redeploys automatically (requires GITHUB_TOKEN env var).
    """
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
    try:
        result = apply_cycle_update(status, entry)
    except Exception as exc:  # noqa: BLE001
        log.exception("ciclo_update failed")
        await update.message.reply_text(
            f"\u274c /ciclo_update fall\u00f3: {exc}", reply_markup=MAIN_KEYBOARD,
        )
        return

    icon = "\u2705" if result.get("ok") else "\u274c"
    pushed = "pushed" if result.get("pushed") else "NO pushed"
    text = (
        f"{icon} /ciclo_update STATUS={status}\n"
        f"   wrote={result.get('wrote')} · {pushed}\n\n"
        f"{result.get('message', '')}"
    )
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
async def cmd_dca(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show BCD DCA plan + current prices + which zones are active right now.

    Round 13: no arguments. Muestra los 4 tranches por asset con su status
    computado sobre el precio spot actual (BTC/ETH/HYPE). Útil como sanity
    check antes de que las alertas automáticas disparen.
    """
    from modules.alerts import _dca_alerted_within_window  # runtime peek
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

    try:
        text = pnl_tracker.build_summary()
    except Exception as exc:  # noqa: BLE001
        log.exception("pnl failed")
        text = f"\u274c /pnl fall\u00f3: {exc}"
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
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

    try:
        entries = position_log.last_n(20)
        text = position_log.format_log(entries)
    except Exception as exc:  # noqa: BLE001
        log.exception("log failed")
        text = f"\u274c /log fall\u00f3: {exc}"
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
    """Scheduled job: process pending intel items via Gemini free."""
    try:
        count = await process_pending_intel(limit=50)
        if count > 0:
            log.info("Intel processor job completed: %d items processed", count)
    except Exception:  # noqa: BLE001
        log.exception("Intel processor job failed")


async def _x_timeline_cache_job(application: Application | None = None) -> None:
    """Scheduled job: refresh the X list timeline cache every N hours.

    Round 12: receives the Application so poll_and_cache_timeline can fire
    a Telegram cost alert when the 7d projection exceeds the threshold.
    """
    try:
        await poll_and_cache_timeline(app=application)
    except Exception:  # noqa: BLE001
        log.exception("X timeline cache job failed")


# ─── Lifecycle hooks ─────────────────────────────────────────────────────────


async def post_init(application: Application) -> None:
    global _telethon_ok
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
        # Intel processor — runs every 30 min, parses pending intel via Gemini free
        scheduler.add_job(
            _intel_processor_job,
            "interval",
            minutes=30,
            id="intel_processor",
            max_instances=1,
            coalesce=True,
        )
        # X list timeline cache — Round 15 (Apr 27 2026): scheduler is now
        # OPT-IN (default off). Pre-Round 15 it ran every 4h regardless,
        # which produced ~6 fetches/day automatically + every /reporte that
        # raced past the cooldown window. Net effect: $70+/7d cost overrun.
        # Round 15 strategy: only /reporte triggers a live fetch. Each fetch
        # is gated by X_LIVE_ENABLED + 2h cooldown + 15/day cap. The
        # in-memory cache is mirrored to SQLite so redeploys don't wipe it.
        # To reactivate the periodic refresh, set X_SCHEDULER_ENABLED=true
        # in Railway Variables (no redeploy needed — picked up at next boot).
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
        scheduler.start()
        application.bot_data["scheduler"] = scheduler
        log.info(
            "Scheduler started: alerts %dmin, intel processor 30min, X scheduler %s.",
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
    await stop_telethon()


# ─── Main ────────────────────────────────────────────────────────────────────


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
    app.add_handler(CommandHandler("x_status", cmd_x_status))
    app.add_handler(CommandHandler("costos_x", cmd_costos_x))
    app.add_handler(CommandHandler("intel_sources", cmd_intel_sources))
    app.add_handler(CommandHandler("providers", cmd_providers))
    app.add_handler(CommandHandler("flywheel", cmd_flywheel))
    app.add_handler(CommandHandler("debug_flywheel", cmd_debug_flywheel))
    app.add_handler(CommandHandler("liqcalc", cmd_liqcalc))
    app.add_handler(CommandHandler("kill", cmd_kill))
    app.add_handler(CommandHandler("ciclo", cmd_ciclo))
    app.add_handler(CommandHandler("ciclo_update", cmd_ciclo_update))
    app.add_handler(CommandHandler("dca", cmd_dca))
    app.add_handler(CommandHandler("pnl", cmd_pnl))
    app.add_handler(CommandHandler("log", cmd_log))

    log.info("Fondo Black Cat bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
