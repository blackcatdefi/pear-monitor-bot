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
from modules.hyperlend import fetch_all_hyperlend as _legacy_fetch_all_hyperlend, fetch_reserve_rates  # noqa: E402

# R-FINAL bug-2: route bot.py's fetch_all_hyperlend through the cache-aware
# reader so /reporte /hf /posiciones never show misleading "HF=∞" when the
# HyperEVM RPC rate-limits us. The legacy fetcher's synthetic-empty
# placeholder is replaced with the last-known HF + age. Other modules
# (alerts.py / flywheel.py) get their already-bound symbol patched at
# startup via _apply_hl_runtime_patch() (called from post_init).
from auto.hyperlend_reader import read_all_with_cache as _hl_read_with_cache  # noqa: E402


async def fetch_all_hyperlend():  # type: ignore[no-redef]
    return await _hl_read_with_cache(fetch_fn=_legacy_fetch_all_hyperlend)


def _apply_hl_runtime_patch() -> None:
    """Replace already-bound fetch_all_hyperlend symbols in downstream
    modules with the cache-aware wrapper. Called from post_init AFTER all
    module imports are complete so we don't fight import order.
    """
    import sys

    for mod_name in ("modules.hyperlend", "modules.flywheel", "modules.alerts"):
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        try:
            setattr(mod, "fetch_all_hyperlend", fetch_all_hyperlend)
        except Exception:  # noqa: BLE001
            pass
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
from modules.cryexc_intel import (
    fetch_cryexc,
    filter_new_events,
    format_for_telegram as format_cryexc_for_telegram,
    is_enabled as cryexc_is_enabled,
    is_monitor_enabled as cryexc_monitor_is_enabled,
    mark_event_seen,
)
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
# Round 18 — proactive modules (lazy imports so missing files don't crash boot)
try:
    from modules.morning_brief import (
        send_morning_brief as r18_send_morning_brief,
        scheduled as r18_morning_scheduled,
        is_enabled as r18_morning_enabled,
    )
except Exception:  # noqa: BLE001
    r18_send_morning_brief = None
    r18_morning_scheduled = None
    r18_morning_enabled = lambda: False
try:
    from modules.basket_close_detector import (
        maybe_emit_summary as r18_basket_close_emit,
        is_enabled as r18_basket_close_enabled,
    )
except Exception:  # noqa: BLE001
    r18_basket_close_emit = None
    r18_basket_close_enabled = lambda: False
try:
    from modules.compounding_detector import (
        scheduled as r18_compounding_scheduled,
        format_history as r18_compounding_history,
        is_enabled as r18_compounding_enabled,
    )
except Exception:  # noqa: BLE001
    r18_compounding_scheduled = None
    r18_compounding_history = None
    r18_compounding_enabled = lambda: False
try:
    from modules.macro_convergence import (
        scheduled_check as r18_convergence_scheduled,
        format_status as r18_convergence_status,
        is_enabled as r18_convergence_enabled,
    )
except Exception:  # noqa: BLE001
    r18_convergence_scheduled = None
    r18_convergence_status = None
    r18_convergence_enabled = lambda: False
try:
    from modules.predictive_alerts import (
        scheduled_check as r18_predictive_scheduled,
        is_enabled as r18_predictive_enabled,
    )
except Exception:  # noqa: BLE001
    r18_predictive_scheduled = None
    r18_predictive_enabled = lambda: False
try:
    from modules.pre_event_brief import (
        scheduled_check as r18_preevent_scheduled,
        is_enabled as r18_preevent_enabled,
    )
except Exception:  # noqa: BLE001
    r18_preevent_scheduled = None
    r18_preevent_enabled = lambda: False
try:
    from modules.risk_config_validator import (
        build_report as r18_risk_check_report,
        is_enabled as r18_risk_check_enabled,
    )
except Exception:  # noqa: BLE001
    r18_risk_check_report = None
    r18_risk_check_enabled = lambda: False
try:
    from modules.fund_state_auto_reconcile import (
        get_callback_handler as r18_auto_reconcile_handler,
    )
except Exception:  # noqa: BLE001
    r18_auto_reconcile_handler = None
try:
    from modules.pnl_extended import build_period_summary as r18_pnl_period
except Exception:  # noqa: BLE001
    r18_pnl_period = None
# Round 18 add-on: heartbeat + scheduler self-healing + perf attribution + aipear auto-prompt
try:
    from modules.heartbeat import send_heartbeat as r18_send_heartbeat
except Exception:  # noqa: BLE001
    r18_send_heartbeat = None
try:
    from modules.scheduler_self_healing import (
        format_health as r18_sched_health_format,
        wrap as r18_self_heal_wrap,
    )
except Exception:  # noqa: BLE001
    r18_sched_health_format = None
    r18_self_heal_wrap = None
try:
    from modules.performance_attribution import attribute_basket_close as r18_perf_attribute
except Exception:  # noqa: BLE001
    r18_perf_attribute = None
try:
    from modules.aipear_auto_prompt import (
        generate_aipear_prompt_post_basket as r18_aipear_prompt,
    )
except Exception:  # noqa: BLE001
    r18_aipear_prompt = None
from templates.formatters import format_hf, format_quick_positions
from templates.timeline import format_timeline
from utils.security import authorized
from utils.telegram import send_bot_message, send_long_message


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s \u2014 %(message)s",
)
# R19: bump httpx/httpcore/urllib3 to WARNING so Railway logs no longer leak
# the bot token via INFO HTTP-request lines. Env-var overridable.
import logging_config  # noqa: E402,F401
# R20: validate UTC at boot — alerts on system clock drift > 60s
import timezone_validator  # noqa: E402,F401
# R21: boot-time anchors and proactive day-clarity layers
from calendar_drift_guard import mark_past_events_at_boot  # noqa: E402
from auto.boot_announcement_v2 import announce_boot  # noqa: E402  (R-FINAL bug-3 dedup)
from morning_brief_scheduler import (  # noqa: E402
    send_morning_brief_job,
    get_scheduled_hour_utc as _morning_brief_hour,
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
            f"{close['leverage']} CLOSED.\n"
            f"Last recorded value: ${close['last_value_usd']:,.2f}"
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
    """Full report: X timeline + positions + LLM analysis.

    Round 16: throttled at 60s/user to avoid stacking concurrent runs.
    """
    await update.message.reply_text(
        "\u23f3 Generating full report: timeline + positions + analysis (30-90s)...",
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

    # ─── Section 1: X Timeline (48h) ─────────────────────────────────────────
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
                f"\u26a0\ufe0f Live API failed: {live_err}\n"
                f"Showing scheduler cache (last success UTC: {last_ok}).\n"
            )
            x_intel = cached
            x_intel_ok = True

    if x_intel_ok:
        timeline_text = format_timeline(x_intel, top_n=40)
        banner = cache_banner_for_report()
        header = (
            "\U0001f4e1 X TIMELINE \u2014 48H\n"
            + ("\u2500" * 30) + "\n"
            + banner + "\n\n"
        )
        if x_intel_fallback_note:
            header = (
                "\U0001f4e1 X TIMELINE \u2014 48H (cache fallback)\n"
                + ("\u2500" * 30) + "\n"
                + banner + "\n"
                + x_intel_fallback_note + "\n"
            )
        await send_long_message(
            update,
            header + timeline_text,
            reply_markup=MAIN_KEYBOARD,
        )

    # ─── Section 2: Positions ──────────────────────────────────────────────────
    positions_text = format_quick_positions(
        portfolio, hl,
        bounce_tech=bt,
        recent_fills=recent_fills,
        market=market,
    )
    await send_long_message(
        update,
        "\U0001f4bc POSITIONS\n" + ("\u2500" * 30) + "\n\n" + positions_text,
        reply_markup=MAIN_KEYBOARD,
    )

    # ─── Section 3: LLM Analysis (Sonnet primary) ───────────────────────────────
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

    # Round 18: Cryexc snapshot (cache 5min, falls back gracefully if disabled)
    if cryexc_is_enabled():
        try:
            cryexc_snap = await fetch_cryexc(force_live=False)
            merged_intel["cryexc_intel"] = cryexc_snap.to_dict()
        except Exception:  # noqa: BLE001
            log.exception("cryexc fetch in /reporte failed (non-fatal)")

    report, thesis_update = await generate_report(portfolio, hl, market, unlocks, merged_intel)

    await send_long_message(
        update,
        "\U0001f9e0 FULL ANALYSIS\n" + ("\u2500" * 30) + "\n\n" + report,
        reply_markup=MAIN_KEYBOARD,
    )
    if thesis_update:
        await send_long_message(update, thesis_update, reply_markup=MAIN_KEYBOARD)

    if not x_intel_ok:
        live_err = ""
        if isinstance(x_intel, dict):
            live_err = str(x_intel.get("error") or "")[:300]
        await update.message.reply_text(
            "\u2139\ufe0f X Timeline unavailable (live + cache both failed).\n"
            f"   Live error: {live_err or '—'}\n"
            "   Diagnostic: run /debug_x for live probe (bypasses cooldown).",
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
                "\U0001f4ca THESIS (plain-text snapshot \u2014 fallback)\n"
                f"Last updated: {last_mod}\n"
                f"{sep}\n\n{content}"
            )
        else:
            await update.message.reply_text(
                "\U0001f4ca No thesis saved yet. Run /reporte first.",
                reply_markup=MAIN_KEYBOARD,
            )
            return

    unprocessed = get_unprocessed_count()
    if unprocessed > 0:
        text += f"\n\n\u23f3 {unprocessed} pending intel items to process"

    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_timeline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "\u23f3 Reading last 48h of your X list...",
        reply_markup=MAIN_KEYBOARD,
    )
    x_intel = await fetch_x_intel(hours=48, caller="timeline", app=context.application)
    banner = cache_banner_for_report()
    if isinstance(x_intel, dict) and x_intel.get("status") != "ok":
        cached = get_cached_timeline()
        if cached and cached.get("status") == "ok":
            prefix = (
                f"\u26a0\ufe0f Live failed: {x_intel.get('error','')[:200]}\n"
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
    await update.message.reply_text(f"Automatic alerts: {estado}", reply_markup=MAIN_KEYBOARD)


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
        "\u23f3 Reading X list \u2014 top 20 accounts last 24h...",
        reply_markup=MAIN_KEYBOARD,
    )
    text = await format_intel_sources(hours=24)
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_cryexc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Round 18: Cryexc snapshot (intel)."""
    if not cryexc_is_enabled():
        await update.message.reply_text(
            "\u26a0\ufe0f /cryexc disabled (CRYEXC_ENABLED=false in Railway).",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    await update.message.reply_text(
        "\u23f3 Fetching cryexc snapshot (funding + movers + HL OI)...",
        reply_markup=MAIN_KEYBOARD,
    )
    try:
        snap = await fetch_cryexc(force_live=False)
        text = format_cryexc_for_telegram(snap)
        await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(
            f"\u274c Error fetching cryexc: {str(exc)[:200]}",
            reply_markup=MAIN_KEYBOARD,
        )


@authorized
@with_error_logging
async def cmd_providers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = format_provider_status()
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_flywheel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("\u23f3 Calculating flywheel pair trade...", reply_markup=MAIN_KEYBOARD)
    text = await compute_flywheel()
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_debug_flywheel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if os.getenv("DEBUG_MODE", "").strip().lower() != "true":
        await update.message.reply_text(
            "\u26a0\ufe0f /debug_flywheel is disabled. Set "
            "DEBUG_MODE=true in Railway vars to enable.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    await update.message.reply_text(
        "\u23f3 Dumping HyperLend reserves...", reply_markup=MAIN_KEYBOARD
    )
    payload = await fetch_reserve_rates(force=True)
    if payload.get("status") != "ok":
        err = payload.get("error") or "unknown"
        await send_long_message(
            update, f"\u274c RPC read failed: {err}",
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
    await update.message.reply_text("\u23f3 Calculating liquidation matrix...", reply_markup=MAIN_KEYBOARD)
    text = await compute_liq_matrix()
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_kill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("\u23f3 Evaluating kill scenarios...", reply_markup=MAIN_KEYBOARD)
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
        f"\u23f3 Applying /ciclo_update STATUS={status}"
        + (f" entry=${entry:,.2f}" if entry is not None else "")
        + "...",
        reply_markup=MAIN_KEYBOARD,
    )
    result = apply_cycle_update(status, entry)
    icon = "\u2705" if result.get("ok") else "\u274c"
    pushed = "pushed" if result.get("pushed") else "NOT pushed"
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
        px_str = f"${px:,.2f}" if px else "(no price)"
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
    lines.append(f"Cycle bottom expected: {BCD_DCA_PLAN.get('cycle_bottom_expected', '?')}")
    sources = ", ".join(BCD_DCA_PLAN.get("sources") or [])
    if sources:
        lines.append(f"Sources: {sources}")
    lines.append("")
    lines.append(
        "Edge-triggered automatic alerts every "
        f"{POLL_INTERVAL_MIN}min when price enters a range. Rearm 24h."
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
                f"\u2705 Log entry #{row_id} added ({params['kind']}).",
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
        "\U0001f9ea TEST ALERT \u2014 alert system operational.\n"
        f"Timestamp UTC: {datetime.now(timezone.utc).isoformat()}\n"
        "If you receive this message, the channel is working OK."
    )
    if TELEGRAM_CHAT_ID:
        await send_bot_message(context.application.bot, TELEGRAM_CHAT_ID, msg)
    await update.message.reply_text("\u2705 Test alert sent.", reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_reload_commands(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Round 16: re-sync command list with Telegram (BotFather)."""
    n = await sync_commands_with_telegram(context.application)
    await update.message.reply_text(
        f"\U0001f504 Commands re-synced with Telegram.\n"
        f"Total registered: {n} (visible in autocomplete).",
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
    """R17 — Reconcile fund_state vs on-chain."""
    await update.message.reply_text("⏳ Reconciling fund_state vs on-chain...", reply_markup=MAIN_KEYBOARD)
    discrepancies = await reconcile_fund_state()
    text = format_reconcile_report(discrepancies)
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R17 — Upcoming catalysts from the macro calendar."""
    text = cal_format()
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_add_event(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R17 — Add event to macro calendar.

    Usage: /add_event <event_id> <YYYY-MM-DDTHH:MMZ> <category> <impact> | <name>
    """
    raw = " ".join(context.args or [])
    if not raw.strip():
        await update.message.reply_text(
            "Usage: /add_event <event_id> <YYYY-MM-DDTHH:MMZ> <category> <impact> | <name>\n"
            "Ex: /add_event fomc_may7 2026-05-07T18:00Z fomc high | FOMC May rate decision",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    try:
        ev = cal_parse_args(raw)
        cal_add_event(ev)
        await update.message.reply_text(
            f"✅ Event added: {ev.event_id} → {ev.timestamp_utc.isoformat()}",
            reply_markup=MAIN_KEYBOARD,
        )
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(f"❌ {exc}", reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_remove_event(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R17 — Remove event from macro calendar."""
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /remove_event <event_id>",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    event_id = args[0].strip()
    ok = cal_remove_event(event_id)
    if ok:
        await update.message.reply_text(f"🗑 {event_id} removed.", reply_markup=MAIN_KEYBOARD)
    else:
        await update.message.reply_text(f"⚠️ {event_id} not found.", reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_kill_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R17 — Status of the 5 kill triggers."""
    await update.message.reply_text("⏳ Evaluating kill triggers...", reply_markup=MAIN_KEYBOARD)
    results = await kill_evaluate_all()
    text = format_kill_status(results)
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_intel_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R17 — Full-text search in intel_memory."""
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /intel_search <keyword>\n"
            "Ex: /intel_search hormuz | /intel_search BTC ATH",
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

    Usage: /export <type> <period>
        types: fills, pnl, positions, intel, errors
        periods: 7d, 30d, 90d, ytd, all
    """
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /export <type> <period>\n"
            "  types: fills, pnl, positions, intel, errors\n"
            "  periods: 7d, 30d, 90d, ytd, all\n"
            "Ex: /export fills 30d",
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
        await update.message.reply_text("❌ Export failed — see /errors.", reply_markup=MAIN_KEYBOARD)
        return

    caption = f"📊 {tipo} ({periodo}) — {count} rows"
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
    """R17 — 5-point pre-trade checklist."""
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /pretrade <SYMBOL>\nEx: /pretrade DYDX",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    symbol = args[0]
    await update.message.reply_text(f"⏳ Pre-trade {symbol.upper()}...", reply_markup=MAIN_KEYBOARD)
    text = await build_pretrade_checklist(symbol)
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


# ─── Round 18 commands ──────────────────────────────────────────────────────


@authorized
@with_error_logging
async def cmd_brief(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R18 — Morning brief on demand."""
    if r18_send_morning_brief is None:
        await update.message.reply_text(
            "Morning brief unavailable (module not loaded).",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    await update.message.reply_text("⏳ Morning brief...", reply_markup=MAIN_KEYBOARD)
    try:
        await r18_send_morning_brief(context.application.bot, force_chat_id=update.effective_chat.id)
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(
            f"⚠️ Brief failed: {str(e)[:200]}",
            reply_markup=MAIN_KEYBOARD,
        )


@authorized
@with_error_logging
async def cmd_pnlx(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R18 — Extended PnL by period."""
    if r18_pnl_period is None:
        await update.message.reply_text(
            "Extended PnL unavailable (module not loaded).",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    args = context.args or []
    period = (args[0] if args else "week").lower()
    valid = ("today", "week", "month", "ytd", "all")
    if period not in valid:
        await update.message.reply_text(
            f"Usage: /pnlx <period>\nValid: {' / '.join(valid)}",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    try:
        text = await r18_pnl_period(period)
    except Exception as e:  # noqa: BLE001
        text = f"⚠️ /pnlx failed: {str(e)[:200]}"
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_convergence(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R18 — Macro convergence triggers."""
    if r18_convergence_status is None:
        await update.message.reply_text(
            "Convergence module unavailable.",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    try:
        text = await r18_convergence_status()
    except Exception as e:  # noqa: BLE001
        text = f"⚠️ /convergence failed: {str(e)[:200]}"
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_risk_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R18 — Risk config invariant validator."""
    if r18_risk_check_report is None:
        await update.message.reply_text(
            "Risk check unavailable (module not loaded).",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    try:
        text = r18_risk_check_report()
    except Exception as e:  # noqa: BLE001
        text = f"⚠️ /risk_check failed: {str(e)[:200]}"
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_compounding_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R18 — Compounding events last 30 days."""
    if r18_compounding_history is None:
        await update.message.reply_text(
            "Compounding history unavailable (module not loaded).",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    try:
        text = r18_compounding_history(days=30)
    except Exception as e:  # noqa: BLE001
        text = f"⚠️ /compounding_history failed: {str(e)[:200]}"
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_scheduler_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R18 add-on — Scheduler health table (last_ok, fails_in_a_row, last_error)."""
    if r18_sched_health_format is None:
        await update.message.reply_text(
            "Scheduler health unavailable (scheduler_self_healing module not loaded).",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    try:
        text = r18_sched_health_format()
    except Exception as e:  # noqa: BLE001
        text = f"⚠️ /scheduler_health failed: {str(e)[:200]}"
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


# ─── R-SILENT: /silent command ───────────────────────────────────────────────


@authorized
@with_error_logging
async def cmd_silent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-SILENT — toggle bot to emergency-only mode.

    Usage:
        /silent on      → suppresses everything except HF<1.05 + post-event critical
        /silent off     → normal mode (with R-SILENT gates applied)
        /silent status  → show current status and gate configuration
    """
    from auto import silent_mode as _silent
    from auto import hf_alert_gate as _hfg
    from auto import catalyst_alert_gate as _cgate

    args = (context.args or [])
    cmd = (args[0] if args else "status").strip().lower()

    if cmd == "on":
        s = _silent.set_silent(True)
        msg = (
            "🔇 SILENT MODE: ON\n\n"
            f"Activated: {s.get('since_iso', '?')}\n\n"
            "Suppressed:\n"
            "  • HF warn (1.05–1.10) — only critical/preliq escape\n"
            "  • Catalyst T-30min — including critical\n"
            "  • Boot announcements\n"
            "  • Heartbeats / morning brief / weekly summary (via gate)\n\n"
            "Still active:\n"
            "  • HF critical (<1.05) and preliq (<1.02)\n"
            "  • Post-event critical T+15min (CATALYST_POST_ALLOWED_IN_SILENT=true)\n"
            "  • Basket close detector (always edge-triggered)\n\n"
            "Type /silent off to return to normal mode."
        )
    elif cmd == "off":
        s = _silent.set_silent(False)
        msg = (
            "🔊 SILENT MODE: OFF\n\n"
            f"Deactivated: {s.get('since_iso', '?')}\n\n"
            "Bot returning to normal mode with R-SILENT gates applied:\n"
            f"  • HF gate: warn <{_hfg.THRESHOLD:.2f} | critical <{_hfg.CRITICAL:.2f} | preliq <{_hfg.PRELIQ:.2f}\n"
            f"  • Catalyst gate: impacts={sorted(_cgate.ALLOW_IMPACTS)} timings={sorted(_cgate.ALLOW_TIMINGS)}\n"
            f"  • Boot dedup window: {os.getenv('BOOT_DEDUP_WINDOW_MIN', '30')} min"
        )
    else:  # status
        s = _silent.status()
        hfs = _hfg.status_summary()
        cs = _cgate.status_summary()
        bot_status = "ON" if s.get("silent") else "OFF"
        emoji = "🔇" if s.get("silent") else "🔊"
        lines = [
            f"{emoji} SILENT MODE: {bot_status}",
            f"Since: {s.get('since_iso', '?')} ({s.get('age_s', 0)//3600}h{(s.get('age_s', 0)%3600)//60}min)",
            f"Source: {s.get('source', '?')}",
            "",
            "── HF gate ──",
            f"  enabled={hfs['enabled']} threshold={hfs['threshold']:.2f} critical={hfs['critical']:.2f} preliq={hfs['preliq']:.2f}",
            f"  dedup={hfs['dedup_min']}min Δ={hfs['dedup_delta']} preliq_repeat={hfs['preliq_repeat_min']}min",
            f"  wallets tracked: {len(hfs.get('tracked_wallets') or [])}",
            "",
            "── Catalyst gate ──",
            f"  enabled={cs['enabled']} impacts={cs['allow_impacts']} timings={cs['allow_timings']}",
            f"  post: T+{cs['postevent_delay_min']}min (window {cs['postevent_window_min']}min)",
            f"  recent post-alerts: {len(cs.get('recent_post_alerts') or [])}",
            "",
            "── Boot dedup ──",
            f"  window: {os.getenv('BOOT_DEDUP_WINDOW_MIN', '30')} min",
            f"  data_dir: {os.getenv('DATA_DIR', '(default)')}",
        ]
        msg = "\n".join(lines)

    await send_long_message(update, msg, reply_markup=MAIN_KEYBOARD)


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
    """R17/R20: every 1 min — fire T-24h/T-2h/T-30m alerts for upcoming events.

    R20: when TIME_AWARENESS_ENABLED=true (default), routes to v2 scheduler
    that recomputes "in X hours" at SEND time and filters past events
    defensively. Set TIME_AWARENESS_ENABLED=false to roll back to v1.
    """
    if os.getenv("MACRO_CALENDAR_ENABLED", "true").strip().lower() == "false":
        return
    try:
        if os.getenv("TIME_AWARENESS_ENABLED", "true").strip().lower() != "false":
            from scheduler_calendar_v2 import run_calendar_alert_check
            await run_calendar_alert_check(application)
        else:
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


async def _portfolio_snapshot_refresh_job() -> None:
    """HOTFIX 2 (2026-04-27): proactive cache warmer.

    Runs on a fixed interval (default 30s) and pre-fills the
    ``portfolio_snapshot`` cache so dashboard hits always find a warm,
    fresh snapshot. Failure is swallowed — the SWR layer already serves
    last-good data from the cache when the fetch fails."""
    try:
        from modules.portfolio_snapshot import proactive_refresh
        await proactive_refresh()
    except Exception:  # noqa: BLE001
        log.exception("portfolio_snapshot proactive refresh job failed")


async def _heartbeat_job(application: Application) -> None:
    """R18 add-on: every HEARTBEAT_INTERVAL_HOURS — minimal alive snapshot."""
    if r18_send_heartbeat is None:
        return
    if os.getenv("HEARTBEAT_ENABLED", "true").strip().lower() == "false":
        return
    try:
        await r18_send_heartbeat(application.bot)
    except Exception:  # noqa: BLE001
        log.exception("heartbeat job failed")


async def _risk_validator_job(application: Application) -> None:
    """R18 audit: every 2h — auto-run risk_check; alert only on FAIL.

    Read-only. Surfaces drift in env-var policy gates so BCD doesn't have
    to remember to /risk_check manually. Silent on PASS.
    """
    if r18_risk_check_report is None or not r18_risk_check_enabled():
        return
    try:
        from modules.risk_config_validator import run_checks as _rcv_run
        results = _rcv_run()
        failures = [c for c in results if not c.ok]
        if not failures:
            return
        chat_id = TELEGRAM_CHAT_ID
        if not chat_id:
            return
        lines = ["\u26a0\ufe0f RISK CONFIG DRIFT \u2014 auto-detect"]
        for c in failures:
            lines.append(f"  \u2022 {c.name}: {c.detail} (expected: {c.expected})")
        lines.append("")
        lines.append("Run /risk_check for details, adjust env vars in Railway.")
        await send_bot_message(application.bot, chat_id, "\n".join(lines))
    except Exception:  # noqa: BLE001
        log.exception("risk_validator job failed")


async def _cryexc_monitor_job(application: Application) -> None:
    """R18: every 30min — cryexc snapshot + fire alert on new notable events."""
    if not cryexc_is_enabled() or not cryexc_monitor_is_enabled():
        return
    try:
        snap = await fetch_cryexc(force_live=True)
        new_events = filter_new_events(snap.notable_events)
        if not new_events:
            return
        chat_id = TELEGRAM_CHAT_ID
        if not chat_id:
            return
        body_lines = ["\U0001f514 CRYEXC ALERT — eventos nuevos:"]
        for ev in new_events[:8]:
            body_lines.append(f"  \u2022 {ev}")
        body_lines.append("")
        body_lines.append(f"Source: cryexc.josedonato.com  Ts: {snap.timestamp_utc[:16]} UTC")
        msg = "\n".join(body_lines)
        try:
            await send_bot_message(application.bot, chat_id, msg)
            for ev in new_events:
                mark_event_seen(ev)
        except Exception:  # noqa: BLE001
            log.exception("cryexc alert send failed")
    except Exception:  # noqa: BLE001
        log.exception("cryexc monitor job failed")


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
        log.info("\u2705 Synced %d commands with Telegram (BotFather autocomplete)", len(tg_commands))
        return len(tg_commands)
    except Exception as exc:  # noqa: BLE001
        log.exception("\u274c set_my_commands failed: %s", exc)
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

    # Round 21: drift guard — mark already-past events as alerted so the
    # scheduler can never re-fire stale alerts after a redeploy.
    try:
        mark_past_events_at_boot()
    except Exception:  # noqa: BLE001
        log.exception("drift_guard run failed (non-fatal)")

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

        # Round 18: Cryexc monitor — every 30 min, alerts on new notable events
        if cryexc_monitor_is_enabled():
            scheduler.add_job(
                _cryexc_monitor_job,
                "interval",
                minutes=30,
                args=[application],
                id="cryexc_monitor",
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2),
            )
            log.info("Cryexc monitor scheduler ENABLED (every 30 min)")
        else:
            log.info("Cryexc monitor scheduler DISABLED (CRYEXC_MONITOR_ENABLED=false)")

        # HOTFIX 2 (2026-04-27): proactive portfolio_snapshot refresh.
        # Keeps the dashboard cache warm so users never see an empty
        # screen waiting on a cold-start fetch. Disable via
        # DASHBOARD_PROACTIVE_REFRESH_ENABLED=false.
        if os.getenv("DASHBOARD_PROACTIVE_REFRESH_ENABLED", "true").strip().lower() != "false":
            try:
                _refresh_interval = int(os.getenv("DASHBOARD_PROACTIVE_REFRESH_INTERVAL", "30"))
            except ValueError:
                _refresh_interval = 30
            scheduler.add_job(
                _portfolio_snapshot_refresh_job,
                "interval",
                seconds=_refresh_interval,
                id="dashboard_snapshot_refresh",
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now(timezone.utc) + timedelta(seconds=10),
            )
            log.info(
                "Dashboard proactive refresh ENABLED — every %ds (HOTFIX 2)",
                _refresh_interval,
            )
        else:
            log.info(
                "Dashboard proactive refresh DISABLED "
                "(DASHBOARD_PROACTIVE_REFRESH_ENABLED=false)"
            )

        # ─── Round 18 jobs ───────────────────────────────────────────────
        # Morning brief — daily 08:00 UTC
        if r18_morning_scheduled is not None and r18_morning_enabled():
            scheduler.add_job(
                lambda: asyncio.create_task(r18_morning_scheduled(application)),
                "cron",
                hour=int(os.getenv("MORNING_BRIEF_HOUR_UTC", "8")),
                minute=0,
                id="morning_brief",
                max_instances=1,
                coalesce=True,
            )
            log.info("R18 morning_brief scheduler ENABLED (daily %sh UTC)",
                     os.getenv("MORNING_BRIEF_HOUR_UTC", "8"))
        # Basket close detector — every 30s (cheap, edge-triggered)
        if r18_basket_close_emit is not None and r18_basket_close_enabled():
            scheduler.add_job(
                lambda: asyncio.create_task(r18_basket_close_emit(application.bot)),
                "interval",
                seconds=30,
                id="basket_close_detector",
                max_instances=1,
                coalesce=True,
            )
            log.info("R18 basket_close_detector ENABLED (every 30s)")
        # Compounding detector — every 5 min
        if r18_compounding_scheduled is not None and r18_compounding_enabled():
            scheduler.add_job(
                lambda: asyncio.create_task(r18_compounding_scheduled(application.bot)),
                "interval",
                minutes=5,
                id="compounding_detector",
                max_instances=1,
                coalesce=True,
            )
            log.info("R18 compounding_detector ENABLED (every 5min)")
        # Macro convergence — every 60 min
        if r18_convergence_scheduled is not None and r18_convergence_enabled():
            scheduler.add_job(
                lambda: asyncio.create_task(r18_convergence_scheduled(application.bot)),
                "interval",
                minutes=60,
                id="macro_convergence",
                max_instances=1,
                coalesce=True,
            )
            log.info("R18 macro_convergence ENABLED (every 60min)")
        # Predictive alerts — every 30 min
        if r18_predictive_scheduled is not None and r18_predictive_enabled():
            scheduler.add_job(
                lambda: asyncio.create_task(r18_predictive_scheduled(application.bot)),
                "interval",
                minutes=30,
                id="predictive_alerts",
                max_instances=1,
                coalesce=True,
            )
            log.info("R18 predictive_alerts ENABLED (every 30min)")
        # Pre-event brief — every 5 min, fires T-90→T-30 window
        if r18_preevent_scheduled is not None and r18_preevent_enabled():
            scheduler.add_job(
                lambda: asyncio.create_task(r18_preevent_scheduled(application)),
                "interval",
                minutes=5,
                id="pre_event_brief",
                max_instances=1,
                coalesce=True,
            )
            log.info("R18 pre_event_brief ENABLED (every 5min)")

        # R18 add-on: heartbeat every N hours
        if r18_send_heartbeat is not None and os.getenv(
            "HEARTBEAT_ENABLED", "true"
        ).strip().lower() != "false":
            try:
                hb_hours = float(os.getenv("HEARTBEAT_INTERVAL_HOURS", "6"))
            except ValueError:
                hb_hours = 6.0
            scheduler.add_job(
                lambda: asyncio.create_task(_heartbeat_job(application)),
                "interval",
                hours=hb_hours,
                id="heartbeat",
                max_instances=1,
                coalesce=True,
            )
            log.info("R18 add-on heartbeat ENABLED (every %.1fh)", hb_hours)

        # R18 audit: risk_config_validator proactive scheduler (every 2h)
        if r18_risk_check_report is not None and r18_risk_check_enabled():
            try:
                rcv_hours = float(os.getenv("RISK_VALIDATOR_INTERVAL_HOURS", "2"))
            except ValueError:
                rcv_hours = 2.0
            scheduler.add_job(
                lambda: asyncio.create_task(_risk_validator_job(application)),
                "interval",
                hours=rcv_hours,
                id="risk_config_validator",
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            log.info("R18 risk_config_validator ENABLED (every %.1fh)", rcv_hours)

        # R21: morning brief — anchor message at MORNING_BRIEF_HOUR_UTC every day
        if os.getenv("MORNING_BRIEF_ENABLED", "true").strip().lower() != "false":
            mb_hour = _morning_brief_hour()
            scheduler.add_job(
                lambda: asyncio.create_task(send_morning_brief_job(application.bot)),
                "cron",
                hour=mb_hour,
                minute=0,
                id="morning_brief",
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )
            log.info("R21 morning_brief ENABLED (cron %02d:00 UTC daily)", mb_hour)

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

    # R-FINAL bug-2: monkey-patch downstream modules so flywheel.py /
    # alerts.py also get the cache-aware HyperLend reader. Done here (not
    # at import time) to avoid import-ordering issues.
    try:
        _apply_hl_runtime_patch()
        log.info("R-FINAL: hyperlend cache-aware reader patched into downstream modules")
    except Exception:  # noqa: BLE001
        log.exception("R-FINAL hyperlend runtime patch failed (non-fatal)")

    # R21 + R-FINAL bug-3: boot announcement — confirm to BCD that the bot
    # is online, clock is validated, calendar is fresh, and list pending
    # events of the rest of the current day. The v2 wrapper now consults
    # auto.boot_dedup so cold-restart spam is suppressed.
    try:
        asyncio.create_task(announce_boot(application.bot))
    except Exception:  # noqa: BLE001
        log.exception("boot announcement task creation failed (non-fatal)")


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
    # Round 18
    "cryexc": cmd_cryexc,
    "brief": cmd_brief,
    "pnlx": cmd_pnlx,
    "convergence": cmd_convergence,
    "risk_check": cmd_risk_check,
    "compounding_history": cmd_compounding_history,
    # Round 18 add-on
    "scheduler_health": cmd_scheduler_health,
    # R-SILENT
    "silent": cmd_silent,
}


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not configured", file=sys.stderr)
        sys.exit(1)
    if not TELEGRAM_CHAT_ID:
        print("ERROR: TELEGRAM_CHAT_ID not configured", file=sys.stderr)
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

    # Round 18: register inline-callback handler for fund_state_auto_reconcile
    try:
        if r18_auto_reconcile_handler is not None:
            cb = r18_auto_reconcile_handler()
            if cb is not None:
                app.add_handler(cb)
                log.info("R18 fund_state_auto_reconcile callback handler registered")
    except Exception:  # noqa: BLE001
        log.exception("R18 auto_reconcile callback registration failed (non-fatal)")

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
