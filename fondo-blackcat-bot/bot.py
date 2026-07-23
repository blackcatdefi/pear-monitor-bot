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
import time
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import BotCommand as TGBotCommand
from telegram import KeyboardButton, LinkPreviewOptions, ReplyKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes, Defaults

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
    # R-COST3 (2026-06-30): generate_report / generate_thesis_check (the Sonnet
    # FULL ANALYSIS narrative) removed from the live path — /reporte is now a
    # deterministic assembly. Only the deterministic /tesis helpers remain.
    _load_thesis,
    load_tesis_latest,
)
from modules.errors_log import (
    cleanup_old as errors_cleanup,
    format_recent as format_recent_errors,
    with_error_logging,
)
from modules.health_server import start_health_server, stop_health_server
# R-BOT-DEFINITIVE-KILLCLEAN (2026-06-15): HyperLend reader ELIMINADO. El fondo
# no usa HyperLend (protocolo Aave-fork muerto). El estado de riesgo vivo es el
# Portfolio Margin nativo (compute_pm_state) — ningún code path llama ya a un
# endpoint de HyperLend / UETH borrow APY.
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
    budget_banner_for_report,
    cache_banner_for_report,
    debug_x_status,
    fetch_x_intel,
    format_intel_sources,
    format_x_costos,
    format_x_costs,
    format_x_status,
    get_cache_state,
    get_cached_timeline,
    get_store_timeline_payload,
)
from modules.cryexc_intel import (
    fetch_cryexc,
    filter_new_events,
    format_for_telegram as format_cryexc_for_telegram,
    is_enabled as cryexc_is_enabled,
    is_monitor_enabled as cryexc_monitor_is_enabled,
    mark_event_seen,
)
from fund_state import BCD_DCA_PLAN
# R-NOPRELIQ + REMOVE BLOFIN (2026-05-15): cycle_trade module ELIMINADO.
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
# R-SIGNAL-DIET (2026-07-20): heartbeat push cada 6h ELIMINADO del scheduler.
# La misma info (uptime, capital, BTC) ahora es on-demand vía /health.
# modules.heartbeat.build_heartbeat queda como builder del texto para /health.
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
from templates.formatters import format_quick_positions, format_report_header
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
    wallets, bt, market, recent_fills = await asyncio.gather(
        fetch_all_wallets(),
        fetch_bounce_tech(),
        fetch_market_data(),
        fetch_all_recent_fills(hours=24),
    )
    hl: list = []  # HyperLend deprecado — el riesgo vivo es Portfolio Margin

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
    # R-BOT-DEFINITIVE-KILLCLEAN: /hf = Portfolio Margin aave-HF (riesgo real de
    # liquidación sobre el colateral HYPE). HyperLend muerto → ya no se lee.
    from modules.portfolio_margin import format_pm_state_telegram
    from modules.pm_context import select_primary_pm_state
    wallets, market = await asyncio.gather(fetch_all_wallets(), fetch_market_data())
    pm = select_primary_pm_state(wallets, market)
    block = format_pm_state_telegram(pm) if pm is not None else ""
    if not block:
        block = (
            "⚖️ PORTFOLIO MARGIN — sin datos\n"
            "No hay colateral/deuda PM legible en la wallet primaria en este momento."
        )
    await update.message.reply_text(block, reply_markup=MAIN_KEYBOARD)


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

    # R-BOT-FEEDS-EXPAND (2026-05-07) — TraderMap.io BTC fetched in parallel
    # alongside the other intel sources so it adds zero serial latency.
    from modules.tradermap import fetch_tradermap_btc
    portfolio, market, unlocks, x_intel, gmail_intel, bt, recent_fills, tradermap = await asyncio.gather(
        fetch_all_wallets(),
        fetch_market_data(),
        fetch_unlocks(),
        fetch_x_intel(hours=48, caller="reporte", app=context.application),
        scan_gmail_unread(),
        fetch_bounce_tech(),
        fetch_all_recent_fills(hours=24),
        fetch_tradermap_btc(),
    )
    hl: list = []  # HyperLend deprecado — riesgo vivo = Portfolio Margin (panel PM)

    if _telethon_ok:
        intel_legacy, intel_unread = await asyncio.gather(
            fetch_telegram_intel(hours=24),
            scan_telegram_unread(max_per_dialog=100),
        )
    else:
        intel_legacy = {"status": "error", "error": "telethon_disabled"}
        intel_unread = {"status": "error", "error": "telethon_disabled"}

    # ─── Section 0: Destacado Header (R-BOT-TERMINOLOGY-UNIFY Bug #4) ────────
    # 4 KPIs surfaced BEFORE timeline: TOTAL EQUITY + BASKET UPnL + HF
    # FLYWHEEL + NEXT CATALYST <72h. Single-source-of-truth via
    # auto.capital_calc / auto.hyperlend_reader / modules.macro_calendar.
    header_text = ""
    try:
        header_text = format_report_header(portfolio, hl, market, unlocks)
        await update.message.reply_text(header_text, reply_markup=MAIN_KEYBOARD)
    except Exception:  # noqa: BLE001
        log.exception("format_report_header failed (non-fatal — continuing)")

    # ─── Section 0b: DELTA vs previous report (R-BOT-DEFINITIVE-2 T6) ────────
    # Deterministic KPI diff (equity/aave-HF/HYPE/BTC/debt/UPnL) vs the last
    # persisted /reporte snapshot. No previous snapshot → omitted silently.
    _delta_kpis = None
    try:
        from modules.report_delta import (
            collect_report_kpis,
            format_report_delta_block,
            load_last_kpis,
        )
        _delta_kpis = collect_report_kpis(portfolio, market, header_text)
        _delta_block = format_report_delta_block(_delta_kpis, load_last_kpis())
        if _delta_block:
            await update.message.reply_text(_delta_block, reply_markup=MAIN_KEYBOARD)
    except Exception:  # noqa: BLE001
        log.exception("report delta block failed (non-fatal)")

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
                f"Showing local store cache (last success UTC: {last_ok}).\n"
            )
            x_intel = cached
            x_intel_ok = True

    if x_intel_ok:
        timeline_text = format_timeline(x_intel, top_n=40)
        # R-COST-V2 CHANGE 4: budget-exhausted banner (cache-only render)
        if isinstance(x_intel, dict) and x_intel.get("budget_exhausted"):
            banner = budget_banner_for_report()
        else:
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

    # ─── Section 1b: Telegram + Gmail intel (R-BOT-DEFINITIVE-2 T3) ──────────
    # Deterministic $0 render of the ALREADY-FETCHED Telegram/Gmail feeds
    # (fetched above for the integrity scan; R-COST3 removed the LLM narrative
    # that used to show them). Render-only — read/mark-read/archive untouched.
    try:
        from modules.intel_render import (
            format_gmail_intel_block,
            format_telegram_intel_block,
        )
        _tg_block = format_telegram_intel_block(intel_legacy, intel_unread)
        if _tg_block:
            await send_long_message(update, _tg_block, reply_markup=MAIN_KEYBOARD)
        _gm_block = format_gmail_intel_block(gmail_intel)
        if _gm_block:
            await send_long_message(update, _gm_block, reply_markup=MAIN_KEYBOARD)
    except Exception:  # noqa: BLE001
        log.exception("telegram/gmail intel render failed (non-fatal)")

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

    # ─── Section 2a: Position classification (R-REPORTE-LIVE FIX 2) ───────────
    # Deterministic per-run tagging of each open position by its REAL on-chain
    # structure (margin mode / SL/TP / laddered limits) so CYCLE-ACCUMULATION
    # DCA legs are never recommended for a bearish close. Non-fatal.
    _clf_tags = []
    try:
        from modules.position_classifier import (
            classify_portfolio,
            build_classification_block,
            cycle_coins,
        )
        _clf_tags = classify_portfolio(portfolio, market)
        _clf_block = build_classification_block(_clf_tags)
        if _clf_block:
            await send_long_message(update, _clf_block, reply_markup=MAIN_KEYBOARD)
    except Exception:  # noqa: BLE001
        log.exception("position classification block failed (non-fatal)")

    # ─── Section 2a-bis: Per-position funding (P1.6) ─────────────────────────
    # Live 8h funding rate + cumulative carry since entry for each open
    # position; LONG cycle-accumulation legs past the expensive floor raise a
    # MANUAL-REVIEW carry flag (never an auto-action). Non-fatal.
    try:
        from modules.funding_tracker import fetch_funding_rates, build_funding_block
        from modules.position_classifier import cycle_coins as _cyc
        _rates = await fetch_funding_rates()
        _all_positions = []
        for _w in (portfolio or []):
            if isinstance(_w, dict) and _w.get("status") == "ok":
                _all_positions.extend((_w.get("data") or {}).get("positions") or [])
        _fund_block = build_funding_block(_all_positions, _rates, _cyc(_clf_tags))
        if _fund_block:
            await send_long_message(update, _fund_block, reply_markup=MAIN_KEYBOARD)
    except Exception:  # noqa: BLE001
        log.exception("per-position funding block failed (non-fatal)")

    # ─── Section 2a-ter: INTEGRITY-HALT scan (R-AUDIT2-P1.3) ─────────────────
    # Born from the ZEC liquidation: scan the intel feeds for integrity /
    # credibility rumors tied to a HELD position with adverse PnL and raise a
    # 🛑 STOP-accumulation MANUAL-REVIEW flag. Never an auto-action. Non-fatal.
    try:
        from modules.integrity_halt import run_integrity_halt
        from config import CYCLE_DCA_BLOCKLIST as _ZB  # noqa: F401  (loaded for plan ctx)
        _ih_positions = []
        for _w in (portfolio or []):
            if isinstance(_w, dict) and _w.get("status") == "ok":
                _ih_positions.extend((_w.get("data") or {}).get("positions") or [])
        _ih_intel = {}
        if isinstance(intel_legacy, dict):
            _ih_intel["legacy"] = intel_legacy
        if isinstance(intel_unread, dict):
            _ih_intel["unread"] = intel_unread
        if isinstance(x_intel, dict):
            _ih_intel["x"] = x_intel
        if isinstance(gmail_intel, dict):
            _ih_intel["gmail"] = gmail_intel
        try:
            from config import FUND_PLAN_ASSETS as _PLAN
            _plan = set(_PLAN)
        except Exception:  # noqa: BLE001
            _plan = {"HYPE", "BTC", "SOL"}
        _ih_block, _ih_new = run_integrity_halt(_ih_positions, _ih_intel, plan_assets=_plan)
        if _ih_block:
            await send_long_message(update, _ih_block, reply_markup=MAIN_KEYBOARD)
    except Exception:  # noqa: BLE001
        log.exception("integrity-halt block failed (non-fatal)")

    # ─── Section 2a-quater: Embedded screener (R-REPORTE-SCREENER-EMBED) ─────
    # Compact TOP-15 SHORT + TOP-15 LONG over the FULL HL+VAR universe — the
    # SAME R-SCREEN 5-gate engine /unlockcheck uses (modules.screener_core
    # calls universal_screener.compute_screen, pure read), surfacing ONLY the
    # 30 names (no RESTO, no DATA-INSUF — full detail stays in /unlockcheck).
    # LONG block carries the tactical/AiPear/not-mandate disclaimer. Non-fatal.
    try:
        from modules.screener_core import build_embedded_screener_block
        _scr_block = await build_embedded_screener_block()
        if _scr_block:
            await send_long_message(update, _scr_block, reply_markup=MAIN_KEYBOARD)
    except Exception:  # noqa: BLE001
        log.exception("embedded screener block failed (non-fatal)")

    # ─── Section 2b: TraderMap BTC (R-BOT-FEEDS-EXPAND Task 1) ────────────────
    # Surface the BTC chart snapshot (price + indicators when env vars set)
    # next to positions so BCD sees price-action context in /reporte without
    # hopping to a separate command. Block is non-fatal — never breaks /reporte.
    try:
        from modules.tradermap import format_tradermap_block
        tm_text = format_tradermap_block(tradermap)
        await update.message.reply_text(tm_text, reply_markup=MAIN_KEYBOARD)
    except Exception:  # noqa: BLE001
        log.exception("format_tradermap_block failed (non-fatal)")

    # ─── Section 3: LMEC bear-invalidation triggers (DETERMINISTIC) ──────────
    # R-COST3 (2026-06-30): the Sonnet FULL ANALYSIS narrative was REMOVED.
    # The bot is a DATA AGGREGATOR + ALERTING + SCREENER — the co-manager (Claude
    # in chat) does all analysis; the owner never read the bot's prose. The ONLY
    # piece of the old LLM section worth keeping is the 4 LMEC bear-invalidation
    # triggers, which are already auto-computed from real closed weekly BTC
    # candles (modules.lmec_triggers) — zero LLM. Render it deterministically.
    lmec_ok = "n/d"
    try:
        from modules.lmec_triggers import evaluate_lmec_triggers, format_lmec_block
        lmec_block = format_lmec_block(evaluate_lmec_triggers(market))
        if lmec_block and lmec_block.strip():
            await send_long_message(update, lmec_block, reply_markup=MAIN_KEYBOARD)
            lmec_ok = "ok"
    except Exception:  # noqa: BLE001
        log.exception("LMEC deterministic block failed (non-fatal)")

    # ─── R-INTEL30 Phase 1 — fan out 11 sources in parallel for /reporte ──────
    # ETF flows, FRED macro, AR FX, ISW geopol, EIA WPSR, ASXN HYPE, HypurrScan
    # auctions, Arkham whales, HL info extras, Apollo Spark. R-COST3: these now
    # feed ONLY the deterministic Section 4 render (no LLM payload). Non-fatal.
    intel30_blocks: list[str] = []
    try:
        from modules.intel30 import (
            hl_info_api as _hli, asxn_data as _asxn, hypurrscan as _hp,
            fred_api as _fred, farside_etfs as _far, arkham_intel as _ark,
            eia_oil as _eia, isw_ctp as _isw, criptoya_ar as _cy, bcra_macro as _bcra,
            apollo_spark as _spark,
        )
        intel30_results = await asyncio.gather(
            _hli.fetch_all(), _asxn.fetch_all(), _hp.fetch_all(), _fred.fetch_all(),
            _far.fetch_all(), _ark.fetch_all(), _eia.fetch_all(), _isw.fetch_all(),
            _cy.fetch_all(), _bcra.fetch_all(), _spark.fetch_all(),
            return_exceptions=True,
        )
        intel30_modules = [
            ("hl_info", _hli), ("asxn", _asxn), ("hypurrscan", _hp), ("fred", _fred),
            ("farside_etfs", _far), ("arkham", _ark), ("eia", _eia), ("isw_ctp", _isw),
            ("criptoya_ar", _cy), ("bcra", _bcra), ("apollo_spark", _spark),
        ]
        for (key, mod), res in zip(intel30_modules, intel30_results):
            if not isinstance(res, Exception):
                try:
                    _blk = mod.format_for_telegram(res)
                    # WI-9e: silent-skip sources (e.g. Arkham sin key) return ""
                    if _blk and _blk.strip():
                        intel30_blocks.append(_blk)
                except Exception:  # noqa: BLE001
                    log.exception("intel30 %s format failed", key)
    except Exception:  # noqa: BLE001
        log.exception("R-INTEL30 fetch in /reporte failed (non-fatal)")

    # R-COST3 audit marker: /reporte is now a deterministic assembly — zero LLM
    # calls in the report path. Sources reaching the final message are logged so
    # a future orphan-source (fetched-but-never-rendered) is easy to spot.
    log.info(
        "[COST_AUDIT] /reporte deterministic assembly — LLM calls=0; "
        "sections=header,delta,x_timeline,telegram_intel,gmail_intel,positions,"
        "classification,funding,integrity_halt,"
        "screener,tradermap,lmec(%s),intel30(%d blocks)",
        lmec_ok, len(intel30_blocks),
    )

    # ─── Section 4: R-INTEL30 Phase 1 enrichment ─────────────────────────────
    # Surface the 11 sources in /reporte as one combined message so BCD can scan
    # ETF flows + macro + geopol at a glance without running dedicated commands.
    if intel30_blocks:
        intel30_text = (
            "🌐 R-INTEL30 PHASE 1 — INTEL EXPANDIDO\n"
            + ("─" * 30) + "\n\n"
            + "\n\n".join(intel30_blocks)
        )
        try:
            await send_long_message(update, intel30_text, reply_markup=MAIN_KEYBOARD)
        except Exception:  # noqa: BLE001
            log.exception("R-INTEL30 send_long_message failed (non-fatal)")

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

    # R-BOT-DEFINITIVE-2 T6: persist this run's KPIs at the END of the run so
    # the NEXT /reporte can render the delta block. Non-fatal.
    try:
        if _delta_kpis:
            from modules.report_delta import save_report_kpis
            save_report_kpis(_delta_kpis)
    except Exception:  # noqa: BLE001
        log.exception("save_report_kpis failed (non-fatal)")


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
    """R-COST-V2: /timeline reads EXCLUSIVELY from the local store — $0.
    To refresh with new tweets, run /reporte or /xrefresh."""
    x_intel = get_store_timeline_payload(hours=48)
    banner = cache_banner_for_report()
    if not x_intel or not x_intel.get("total"):
        await update.message.reply_text(
            "\U0001f4ed Local X store empty — run /reporte or /xrefresh first "
            "(that's the only place the X API is called).",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    text = banner + "\n" + ("\u2500" * 30) + "\n\n" + format_timeline(x_intel, top_n=40)
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_xrefresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-COST-V2: explicit manual X refresh — the ONLY live call site
    besides /reporte. Incremental via since_id (pays only the delta)."""
    await update.message.reply_text(
        "\u23f3 Refreshing X list (incremental since_id)...",
        reply_markup=MAIN_KEYBOARD,
    )
    x_intel = await fetch_x_intel(hours=48, caller="xrefresh", app=context.application)
    if isinstance(x_intel, dict) and x_intel.get("budget_exhausted"):
        from modules.x_intel import budget_banner_for_report as _bb
        await update.message.reply_text(_bb(), reply_markup=MAIN_KEYBOARD)
        return
    if not isinstance(x_intel, dict) or x_intel.get("status") != "ok":
        err = str((x_intel or {}).get("error") or "")[:200] if isinstance(x_intel, dict) else ""
        await update.message.reply_text(
            f"\u274c Refresh failed: {err or 'unknown'}", reply_markup=MAIN_KEYBOARD
        )
        return
    fetched = x_intel.get("fetched_new", 0)
    total = x_intel.get("total", 0)
    await update.message.reply_text(
        f"\u2705 X store refreshed: +{fetched} new posts fetched "
        f"(\u2248${fetched * 0.005:.2f}) \u2014 {total} tweets in 48h window.\n"
        "Use /timeline to view it.",
        reply_markup=MAIN_KEYBOARD,
    )


@authorized
@with_error_logging
async def cmd_costs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-COST-V2 CHANGE 5: X cost visibility dashboard."""
    text = await format_x_costs()
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
    # R-COST-V2: store-only read — zero API calls.
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


# R-BOT-DEFINITIVE-KILLCLEAN (2026-06-15): cmd_flywheel, cmd_debug_flywheel y
# cmd_liqcalc ELIMINADOS. Eran el flywheel HyperLend pair-trade (LONG HYPE
# colateral / SHORT UETH deuda) que YA NO EXISTE — el fondo migró 100% a
# Portfolio Margin nativo. El riesgo de liquidación vivo (aave-HF, liq price,
# utilización del colateral HYPE) se ve en /reporte (panel PM) y /hf.


@authorized
@with_error_logging
async def cmd_kill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("\u23f3 Evaluating kill scenarios...", reply_markup=MAIN_KEYBOARD)
    text = await compute_kill_scenarios()
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


# R-NOPRELIQ + REMOVE BLOFIN (2026-05-15): cmd_ciclo y cmd_ciclo_update ELIMINADOS.
# Trade del Ciclo (Blofin) ya no es un vehículo del fondo.


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
    # R-NOPRELIQ + REMOVE BLOFIN (2026-05-15): subcomando `/pnl ciclo` ELIMINADO.

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

    await update.message.reply_text("⏳ Fetching fills from Hyperliquid...", reply_markup=MAIN_KEYBOARD)
    text = await pnl_tracker.build_auto_summary()
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
async def cmd_pat_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-PAT-RENEW: GitHub PAT expiry readout (days left + verdict)."""
    try:
        from modules.pat_status import get_pat_status, format_pat_status_block
        status = get_pat_status(force_refresh=True)
        text = format_pat_status_block(status)
    except Exception as exc:  # noqa: BLE001
        text = f"\U0001f511 GitHub PAT status\n\u26a0\ufe0f No disponible: {str(exc)[:200]}"
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
async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-SIGNAL-DIET: on-demand alive snapshot (reemplaza el heartbeat push 6h).

    Misma info que el viejo heartbeat (uptime, capital, HF, BTC) pero SOLO
    cuando BCD la pide. Cero pushes programados."""
    from modules.heartbeat import build_heartbeat
    text = await build_heartbeat()
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
async def cmd_setcatalyst(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-BOT-DEFINITIVE WI-1 — manage the catalysts engine table.

    Usage:
      /setcatalyst add YYYY-MM-DD [HH:MM] <nombre> [impact]
      /setcatalyst del <id>
      /setcatalyst list
    """
    from modules.catalysts import handle_setcatalyst
    text = handle_setcatalyst(list(context.args or []))
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


# ─── R-DASHBOARD-COMMAND: /dashboard ─────────────────────────────────────────


# ─── R-BOT-LMEC-AUTOFEED: /lmec_status command ──────────────────────────────


@authorized
@with_error_logging
async def cmd_lmec_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-BOT-LMEC-AUTOFEED — bear-invalidation telemetry.

    Shows source actual (tradermap/env), last successful pull, valores de
    cada leg, persisted state (weeks counter + last flip + scraper health).
    """
    await update.message.reply_text(
        "🔬 Calculando LMEC...", reply_markup=MAIN_KEYBOARD
    )
    try:
        from modules.lmec_triggers import (
            evaluate_lmec_triggers,
            format_lmec_status,
        )
        from modules.market import fetch_market_data

        market = None
        try:
            market = await fetch_market_data()
        except Exception:  # noqa: BLE001
            log.exception("/lmec_status market fetch failed (non-fatal)")
        result = evaluate_lmec_triggers(market)
        text = format_lmec_status(result)
    except Exception as exc:  # noqa: BLE001
        log.exception("/lmec_status render failed")
        text = f"❌ /lmec_status error: {str(exc)[:200]}\nSee /errors for details."
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_setlmec(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """P1.9 — set BCD's manual LMEC TradingView inputs (persisted on Volume).

    Usage:
      /setlmec                 → show current inputs + usage
      /setlmec macd pos|neg    → weekly MACD above/below 0
      /setlmec rsi <valor>     → weekly RSI 14
      /setlmec ma50w <valor>   → current 50-week MA (USD)
      /setlmec clear <campo>   → clear one input (back to awaiting)
    """
    from modules.lmec_state import (
        get_computed_inputs,
        get_computed_meta,
        get_manual_inputs,
        set_manual_input,
    )

    args = context.args or []
    if not args:
        cur = get_manual_inputs()
        comp = get_computed_inputs()
        meta = get_computed_meta()

        def _show(k, label):
            # OVERRIDE wins and is flagged; otherwise show the COMPUTED value.
            ov = cur.get(k)
            if ov is not None:
                return f"  • {label}: {ov}  [OVERRIDE /setlmec]"
            cv = comp.get(k)
            if cv is not None:
                shown = f"{cv:.1f}" if isinstance(cv, float) else cv
                return f"  • {label}: {shown}  [COMPUTED]"
            return f"  • {label}: n/d (auto-cómputo sin datos)"

        src = meta.get("source") or "—"
        wc = meta.get("weekly_close_ts_utc") or "—"
        fresh = "fresh" if meta.get("fresh") else ("STALE" if meta.get("present") else "—")
        text = (
            "🧭 /setlmec — inputs LMEC (auto-computados; /setlmec = override manual)\n\n"
            + _show("macd_weekly_positive", "MACD semanal positivo") + "\n"
            + _show("rsi_weekly", "RSI semanal") + "\n"
            + _show("ma50w_usd", "MA50w (USD)") + "\n\n"
            + f"📊 Fuente computada: {src} ({fresh})\n"
            + f"   weekly close: {wc}\n\n"
            "Uso (override manual):\n"
            "  /setlmec macd pos|neg\n"
            "  /setlmec rsi 72.5\n"
            "  /setlmec ma50w 88000\n"
            "  /setlmec clear macd|rsi|ma50w   (vuelve a COMPUTED)"
        )
        await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)
        return

    field = args[0].strip().lower()
    val = args[1].strip().lower() if len(args) > 1 else ""
    alias = {"macd": "macd_weekly_positive", "rsi": "rsi_weekly", "ma50w": "ma50w_usd"}
    try:
        if field == "clear":
            key = alias.get(val)
            if not key:
                raise ValueError("campo a limpiar: macd|rsi|ma50w")
            set_manual_input(key, None)
            await update.message.reply_text(
                f"🧹 LMEC {val} override limpiado (vuelve a COMPUTED).", reply_markup=MAIN_KEYBOARD
            )
            return
        if field == "macd":
            if val in ("pos", "positive", "true", "1", "yes"):
                set_manual_input("macd_weekly_positive", True)
            elif val in ("neg", "negative", "false", "0", "no"):
                set_manual_input("macd_weekly_positive", False)
            else:
                raise ValueError("usá: /setlmec macd pos|neg")
            await update.message.reply_text("✅ MACD semanal actualizado.", reply_markup=MAIN_KEYBOARD)
            return
        if field == "rsi":
            set_manual_input("rsi_weekly", float(val))
            await update.message.reply_text("✅ RSI semanal actualizado.", reply_markup=MAIN_KEYBOARD)
            return
        if field == "ma50w":
            set_manual_input("ma50w_usd", float(val))
            await update.message.reply_text("✅ MA50w actualizado.", reply_markup=MAIN_KEYBOARD)
            return
        raise ValueError("campo desconocido — usá macd|rsi|ma50w|clear")
    except ValueError as exc:
        await update.message.reply_text(f"❌ {exc}", reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_setppc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-BOT-DEFINITIVE-2 T5 — manual HYPE PPC + net-acquisition override.

    Usage:
      /setppc                   → show current override + usage
      /setppc <ppc> <net_acq>   → set both (USD), timestamped in SQLite
      /setppc clear             → remove override (report reverts to n/d/auto)
    """
    from modules.hype_acquisition import (
        clear_ppc_override,
        get_ppc_override,
        set_ppc_override,
    )

    args = context.args or []
    if not args:
        ov = get_ppc_override()
        cur = (
            f"  • PPC contable: ${ov['ppc_usd']:,.2f} (manual, set {ov['set_date']})\n"
            f"  • adq. neta: ${ov['net_acq_usd']:,.2f} (manual, set {ov['set_date']})"
            if ov else "  • sin override — el reporte usa fills/n-d automático"
        )
        await update.message.reply_text(
            "💠 /setppc — override manual de PPC HYPE\n\n" + cur + "\n\n"
            "Uso:\n  /setppc 53.5 41.5   (PPC contable + adq. neta, USD)\n"
            "  /setppc clear       (vuelve a n/d/auto)",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    if args[0].strip().lower() == "clear":
        cleared = clear_ppc_override()
        await update.message.reply_text(
            "🧹 Override PPC limpiado (vuelve a n/d/auto)." if cleared
            else "ℹ️ No había override PPC activo.",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    if len(args) < 2:
        await update.message.reply_text(
            "❌ Faltan valores — usá: /setppc <ppc> <adq_neta>  (ej: /setppc 53.5 41.5)",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    try:
        ppc = float(args[0].replace("$", "").replace(",", "."))
        net = float(args[1].replace("$", "").replace(",", "."))
        if not (ppc > 0 and net > 0):
            raise ValueError
    except (TypeError, ValueError):
        await update.message.reply_text(
            "❌ Valores inválidos — ambos deben ser números > 0 "
            "(ej: /setppc 53.5 41.5)",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    if set_ppc_override(ppc, net):
        await update.message.reply_text(
            f"✅ Override PPC guardado: PPC ${ppc:,.2f} · adq. neta ${net:,.2f} "
            "(aparece en /reporte como línea manual).",
            reply_markup=MAIN_KEYBOARD,
        )
    else:
        await update.message.reply_text(
            "❌ No se pudo guardar el override (ver /errors).",
            reply_markup=MAIN_KEYBOARD,
        )


@authorized
@with_error_logging
async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-DASHBOARD-COMMAND — full dashboard as a Telegram message.

    Renders the same data as the web dashboard (R-DASHBOARD-FIX SSoT):
    Capital, Main flywheel, Secondary flywheel, Active basket, Market,
    Wallets summary, Upcoming catalysts, Footer.
    """
    await update.message.reply_text("⏳ Fetching dashboard...", reply_markup=MAIN_KEYBOARD)
    try:
        from modules.dashboard_telegram import build_dashboard_telegram
        text = await build_dashboard_telegram()
    except Exception as exc:  # noqa: BLE001
        log.exception("/dashboard render failed")
        text = f"❌ Dashboard error: {str(exc)[:200]}\nSee /errors for details."
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


# ─── R-INTEL30 Phase 1 — 11 new free intel sources ────────────────────────────
#
# Modules under modules/intel30/. Each handler is a thin wrapper that fetches
# data via the module's `fetch_all()` and renders via `format_for_telegram()`.
# All modules degrade gracefully on network errors / missing API keys, so a
# command never crashes the bot — at worst it prints a one-liner with the error.

async def _intel30_render(update: Update, module_name: str, header: str) -> None:
    """Generic dispatch: import a Phase 1 module and render its output."""
    try:
        mod = __import__(f"modules.intel30.{module_name}", fromlist=["fetch_all", "format_for_telegram"])
        data = await mod.fetch_all()
        text = mod.format_for_telegram(data)
    except Exception as exc:  # noqa: BLE001
        log.exception("intel30 %s failed", module_name)
        text = f"⚠️ {header}\n  Error: {str(exc)[:200]}"
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_etfs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-INTEL30 #5 — Farside daily BTC/ETH/SOL ETF flows."""
    await _intel30_render(update, "farside_etfs", "Farside ETF Flows")


@authorized
@with_error_logging
async def cmd_macro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-INTEL30 #4+#11 — FRED US macro + Apollo Daily Spark."""
    from modules.intel30 import fred_api, apollo_spark
    try:
        fred_data, apollo_data = await asyncio.gather(
            fred_api.fetch_all(), apollo_spark.fetch_all(), return_exceptions=True
        )
        parts = []
        if not isinstance(fred_data, Exception):
            parts.append(fred_api.format_for_telegram(fred_data))
        if not isinstance(apollo_data, Exception):
            parts.append(apollo_spark.format_for_telegram(apollo_data))
        text = "\n\n".join(parts) if parts else "⚠️ macro fetch failed"
    except Exception as exc:  # noqa: BLE001
        log.exception("/macro failed")
        text = f"⚠️ Macro intel error: {str(exc)[:200]}"
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_argy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-INTEL30 #9+#10 — CriptoYa AR FX + BCRA macro."""
    from modules.intel30 import criptoya_ar, bcra_macro
    try:
        cy_data, bcra_data = await asyncio.gather(
            criptoya_ar.fetch_all(), bcra_macro.fetch_all(), return_exceptions=True
        )
        parts = []
        if not isinstance(cy_data, Exception):
            parts.append(criptoya_ar.format_for_telegram(cy_data))
        if not isinstance(bcra_data, Exception):
            parts.append(bcra_macro.format_for_telegram(bcra_data))
        text = "\n\n".join(parts) if parts else "⚠️ AR macro fetch failed"
    except Exception as exc:  # noqa: BLE001
        log.exception("/argy failed")
        text = f"⚠️ AR intel error: {str(exc)[:200]}"
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_isw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-INTEL30 #8 — ISW + Critical Threats Project geopol RSS."""
    await _intel30_render(update, "isw_ctp", "ISW+CTP")


@authorized
@with_error_logging
async def cmd_eia(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-INTEL30 #7 — EIA WPSR oil/gas weekly."""
    await _intel30_render(update, "eia_oil", "EIA WPSR")


@authorized
@with_error_logging
async def cmd_asxn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-INTEL30 #2 — ASXN HYPE buyback/burn/staking dashboards."""
    await _intel30_render(update, "asxn_data", "ASXN HYPE")


@authorized
@with_error_logging
async def cmd_hypurr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-INTEL30 #3 — HypurrScan HIP-1 auctions + TWAPs."""
    await _intel30_render(update, "hypurrscan", "HypurrScan")


@authorized
@with_error_logging
async def cmd_arkham(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-INTEL30 #6 — Arkham entity transfers."""
    await _intel30_render(update, "arkham_intel", "Arkham")


@authorized
@with_error_logging
async def cmd_hl_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-INTEL30 #1 — HL Info API extras (perpDexs + predictedFundings)."""
    await _intel30_render(update, "hl_info_api", "HL Info API")


@authorized
@with_error_logging
async def cmd_spark(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-INTEL30 #11 — Apollo Daily Spark (Torsten Slok)."""
    await _intel30_render(update, "apollo_spark", "Apollo Daily Spark")


@authorized
@with_error_logging
async def cmd_intel30(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-INTEL30 — Run all 11 Phase 1 sources in parallel and dump combined output."""
    from modules.intel30 import (
        hl_info_api, asxn_data, hypurrscan, fred_api, farside_etfs,
        arkham_intel, eia_oil, isw_ctp, criptoya_ar, bcra_macro, apollo_spark,
    )
    await update.message.reply_text(
        "\u23f3 Fetching 11 Phase-1 intel sources in parallel (~15s)...",
        reply_markup=MAIN_KEYBOARD,
    )
    modules = [
        ("HL Info", hl_info_api), ("ASXN", asxn_data), ("HypurrScan", hypurrscan),
        ("FRED", fred_api), ("Farside", farside_etfs), ("Arkham", arkham_intel),
        ("EIA", eia_oil), ("ISW+CTP", isw_ctp), ("CriptoYa", criptoya_ar),
        ("BCRA", bcra_macro), ("Apollo Spark", apollo_spark),
    ]
    results = await asyncio.gather(
        *[m.fetch_all() for _, m in modules], return_exceptions=True
    )
    parts = []
    for (name, mod), res in zip(modules, results):
        if isinstance(res, Exception):
            parts.append(f"⚠️ {name}: {str(res)[:80]}")
        else:
            parts.append(mod.format_for_telegram(res))
    combined = "\n\n".join(parts)
    await send_long_message(update, combined, reply_markup=MAIN_KEYBOARD)


# ─── R-PERFECT Phase 2 (16 modules) + Phase 3 (3 modules) ────────────────────

@authorized
@with_error_logging
async def cmd_hl_rpc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-PERFECT Sub-1 #1 — HyperEVM RPC edge probe."""
    await _intel30_render(update, "hl_rpc_edge", "HyperEVM RPC")


@authorized
@with_error_logging
async def cmd_hyperevmscan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-PERFECT Sub-1 #2 — HyperEVMScan via Etherscan v2."""
    await _intel30_render(update, "hyperevmscan", "HyperEVMScan")


@authorized
@with_error_logging
async def cmd_dune(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-PERFECT Sub-1 #3 — Dune top HL dashboards."""
    await _intel30_render(update, "dune_hl", "Dune HL")


@authorized
@with_error_logging
async def cmd_hypetrad(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-PERFECT Sub-1 #4 — HypeTrad pro trader leaderboard."""
    await _intel30_render(update, "hypetrad", "HypeTrad")


@authorized
@with_error_logging
async def cmd_treasury(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-PERFECT Sub-2 #1 — US Treasury Fiscal Data."""
    await _intel30_render(update, "treasury_fiscal", "Treasury Fiscal")


@authorized
@with_error_logging
async def cmd_nyfed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-PERFECT Sub-2 #2 — NY Fed Markets reference rates."""
    await _intel30_render(update, "nyfed_markets", "NY Fed")


@authorized
@with_error_logging
async def cmd_cot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-PERFECT Sub-2 #3 — CFTC Commitments of Traders."""
    await _intel30_render(update, "cftc_cot", "CFTC COT")


@authorized
@with_error_logging
async def cmd_l2beat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-PERFECT Sub-3 #1 — L2Beat scaling summary."""
    await _intel30_render(update, "l2beat", "L2Beat")


@authorized
@with_error_logging
async def cmd_artemis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-PERFECT Sub-3 #2 — Artemis Terminal asset metrics."""
    await _intel30_render(update, "artemis_lite", "Artemis")


@authorized
@with_error_logging
async def cmd_visa_oc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-PERFECT Sub-3 #3 — Visa Onchain Analytics."""
    await _intel30_render(update, "visa_onchain", "Visa Onchain")


@authorized
@with_error_logging
async def cmd_treasuries(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-PERFECT Sub-3 #4 — BTC + ETH treasuries bundle."""
    await _intel30_render(update, "treasuries_bundle", "Treasuries")


@authorized
@with_error_logging
async def cmd_openinsider(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-PERFECT Sub-4 #1 — OpenInsider Form 4 latest."""
    await _intel30_render(update, "openinsider", "OpenInsider")


@authorized
@with_error_logging
async def cmd_capitol(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-PERFECT Sub-4 #2 — Capitol Trades."""
    await _intel30_render(update, "capitol_trades", "CapitolTrades")


@authorized
@with_error_logging
async def cmd_epoch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-PERFECT Sub-4 #3 — Epoch AI notable models."""
    await _intel30_render(update, "epoch_ai", "Epoch AI")


@authorized
@with_error_logging
async def cmd_semianalysis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-PERFECT Sub-4 #4 — SemiAnalysis Substack RSS."""
    await _intel30_render(update, "semianalysis_rss", "SemiAnalysis")


@authorized
@with_error_logging
async def cmd_finrss(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-PERFECT Sub-4 #5 — Finance newsletter RSS bundle."""
    await _intel30_render(update, "finance_rss", "Finance RSS")


@authorized
@with_error_logging
async def cmd_cryptovol(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-PERFECT Phase 3 #1 — Deribit DVOL + Coinalyze + Velo."""
    await _intel30_render(update, "crypto_vol", "Crypto Vol")


@authorized
@with_error_logging
async def cmd_kalshi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-PERFECT Phase 3 #2 — Kalshi public markets + RSA-PSS auth probe."""
    await _intel30_render(update, "kalshi_api", "Kalshi")


@authorized
@with_error_logging
async def cmd_indec(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-PERFECT Phase 3 #3 — INDEC + LATAM macro extras."""
    await _intel30_render(update, "argy_extra", "INDEC + LATAM")


@authorized
@with_error_logging
async def cmd_intel30_full(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-PERFECT — Run all 30 intel sources (Phase1+2+3) in parallel."""
    from modules.intel30 import ALL_MODULES
    await update.message.reply_text(
        "\u23f3 Fetching 30 intel sources in parallel (~25s)...",
        reply_markup=MAIN_KEYBOARD,
    )
    mods = []
    for name in ALL_MODULES:
        try:
            mods.append((name, __import__(f"modules.intel30.{name}", fromlist=["fetch_all"])))
        except Exception:  # noqa: BLE001
            log.exception("intel30_full import %s", name)
    results = await asyncio.gather(*[m.fetch_all() for _, m in mods], return_exceptions=True)
    parts: list[str] = []
    for (name, mod), res in zip(mods, results):
        if isinstance(res, Exception):
            parts.append(f"⚠️ {name}: {str(res)[:80]}")
        else:
            try:
                parts.append(mod.format_for_telegram(res))
            except Exception as e:  # noqa: BLE001
                parts.append(f"⚠️ {name}: format failed — {str(e)[:60]}")
    combined = "\n\n".join(parts)
    await send_long_message(update, combined, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_selftest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-PERFECT Phase 3 #9 — /selftest: fan out all 30 sources, return matrix."""
    from modules.intel_selftest import run_selftest, format_matrix
    await update.message.reply_text("\u23f3 Running /selftest on 30 sources (timeout 10s each)...",
                                    reply_markup=MAIN_KEYBOARD)
    matrix = await run_selftest()
    text = format_matrix(matrix)
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-PERFECT Phase 3 #3 — /cost: LLM call cost breakdown last 7d."""
    from modules.cost_tracker import format_cost_report
    text = format_cost_report(days=7)
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_sources(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-PERFECT — /sources: latest source status table from intel.log."""
    from modules.intel_selftest import format_source_status
    text = format_source_status()
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
    # R-ONDEMAND gate: silence background intel pulls when bot is on-demand-only.
    try:
        from modules.cron_state import intel_autopull_enabled
        if not intel_autopull_enabled():
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        count = await process_pending_intel(limit=50)
        if count > 0:
            log.info("Intel processor job completed: %d items processed", count)
    except Exception:  # noqa: BLE001
        log.exception("Intel processor job failed")


# R-COST-V2: _x_timeline_cache_job REMOVED. No scheduled/cron/interval path
# may call the X API — reads happen ONLY inside /reporte and /xrefresh.


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


# ─── R-VARIATIONAL — Farm the DUMP funding scanner + reversion watches ───────


@authorized
@with_error_logging
async def cmd_variationalfunding(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scan Variational perps for funding ≤ VARIATIONAL_FUNDING_THRESHOLD (anual)."""
    from modules import variational as _var

    threshold = _var.funding_threshold()
    await update.message.reply_text(
        f"⏳ Escaneando Variational (funding ≤ {threshold:,.0f}% anual)...",
        reply_markup=MAIN_KEYBOARD,
    )
    try:
        markets = await _var.fetch_markets()
    except _var.VariationalError as exc:
        await update.message.reply_text(
            f"⚠️ Variational n/a — {exc}", reply_markup=MAIN_KEYBOARD
        )
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("/variationalfunding failed")
        await update.message.reply_text(
            f"❌ Error inesperado: {str(exc)[:200]}", reply_markup=MAIN_KEYBOARD
        )
        return

    qualifying = _var.scan_negative_funding(markets, threshold)
    text = _var.format_funding_scan(qualifying, threshold, len(markets))
    await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)


def _variational_usage() -> str:
    return (
        "Usage:\n"
        "  /variationalalerts <TICKER>   — registrar watch (baseline = funding actual)\n"
        "  /variationalalerts list       — ver watches activos\n"
        "  /variationalalerts remove <TICKER>\n"
        "  /variationalalerts clear      — borrar todos\n"
        "Ej: /variationalalerts PORTAL"
    )


@authorized
@with_error_logging
async def cmd_variationalalerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Register / manage mean-reversion watches on Variational funding."""
    from modules import variational as _var
    from modules import variational_alerts as _va

    args = [a.strip() for a in (context.args or []) if a.strip()]
    fraction = _va.reversion_fraction()

    # No arg → usage + current list.
    if not args:
        watches = await asyncio.to_thread(_va.list_watches, True)
        text = _variational_usage() + "\n\n" + _va.format_watch_list(watches, fraction)
        await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)
        return

    sub = args[0].lower()

    if sub == "list":
        watches = await asyncio.to_thread(_va.list_watches, True)
        await send_long_message(
            update, _va.format_watch_list(watches, fraction), reply_markup=MAIN_KEYBOARD
        )
        return

    if sub == "clear":
        n = await asyncio.to_thread(_va.clear)
        await update.message.reply_text(
            f"🗑 {n} watch(es) borrado(s).", reply_markup=MAIN_KEYBOARD
        )
        return

    if sub == "remove":
        if len(args) < 2:
            await update.message.reply_text(
                "Usage: /variationalalerts remove <TICKER>", reply_markup=MAIN_KEYBOARD
            )
            return
        ok = await asyncio.to_thread(_va.remove, args[1])
        msg = f"🗑 Watch {args[1].upper()} eliminado." if ok else f"No había watch para {args[1].upper()}."
        await update.message.reply_text(msg, reply_markup=MAIN_KEYBOARD)
        return

    # Otherwise: treat first arg as a ticker to register.
    ticker = sub.upper()
    try:
        market = await _var.get_market(ticker)
    except _var.VariationalError as exc:
        await update.message.reply_text(
            f"⚠️ No pude leer funding de Variational — {exc}", reply_markup=MAIN_KEYBOARD
        )
        return

    if market is None:
        await update.message.reply_text(
            f"❌ {ticker} no figura en Variational ahora mismo. Verificá el ticker "
            f"o probá /variationalfunding para ver los activos listados.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    baseline = market.annualized_pct
    watch = await asyncio.to_thread(_va.register, ticker, baseline)
    target = _va.reversion_target(baseline, fraction)
    await update.message.reply_text(
        "✅ Watch registrado — Farm the DUMP\n\n"
        f"{ticker}\n"
        f"Baseline funding: {baseline:,.1f}% anual\n"
        f"Disparo cuando funding ≥ {target:,.1f}% (baseline × {fraction:g})\n"
        f"Mark actual: {market.mark_price if market.mark_price is not None else 'n/a'}\n\n"
        f"Te aviso una vez cuando revierta. /variationalalerts list para ver todos.",
        reply_markup=MAIN_KEYBOARD,
    )


async def _variational_alerts_job(application: Application) -> None:
    """Every ~30 min — fire ONE alert per watch when funding reverts to half.

    These alerts are user-requested and material, so they fire regardless of
    silent mode (no other noise is added). Fully wrapped: a Variational
    outage or DB hiccup logs and returns, never crashing the scheduler.
    """
    from config import VARIATIONAL_ALERTS_ENABLED
    if not VARIATIONAL_ALERTS_ENABLED:
        return
    from modules import variational as _var
    from modules import variational_alerts as _va

    chat_id = TELEGRAM_CHAT_ID
    if not chat_id:
        return
    try:
        active = await asyncio.to_thread(_va.list_watches, False)  # untriggered only
    except Exception:  # noqa: BLE001
        log.exception("variational alerts: list_watches failed")
        return
    if not active:
        return

    try:
        markets = await _var.fetch_markets()
    except Exception as exc:  # noqa: BLE001
        log.warning("variational alerts: market fetch n/a (%s) — skipping cycle", exc)
        return

    by_ticker = {m.ticker: m for m in markets}
    fraction = _va.reversion_fraction()
    for w in active:
        m = by_ticker.get(w.ticker)
        if m is None:
            continue  # ticker temporarily delisted — keep watching
        current = m.annualized_pct
        try:
            if _va.has_reverted(w.baseline_funding, current, fraction):
                msg = _va.format_reversion_alert(w, current, fraction, m.mark_price)
                # R-FARMDUMP — auto-run the 5 pre-trade checks and append a
                # GO/CAUTION/NO-GO verdict. Fully wrapped: a failure here must
                # never block the (material) reversion alert from firing.
                enriched = await _farmdump_block(w, m, current, fraction)
                if enriched:
                    msg = msg + "\n\n" + enriched
                await send_bot_message(application.bot, chat_id, msg)
                await asyncio.to_thread(_va.mark_triggered, w.ticker, current)
                log.info("variational reversion fired: %s base=%.1f cur=%.1f",
                         w.ticker, w.baseline_funding, current)
            else:
                await asyncio.to_thread(_va.update_current, w.ticker, current)
        except Exception:  # noqa: BLE001
            log.exception("variational alerts: eval failed for %s", w.ticker)


async def _pm_monitor_job(application: Application) -> None:
    """R-PMCORE + R-PMALERT — Portfolio Margin watchdog (edge-triggered, SQLite,
    R-SILENT aware).

    Recompute the primary-account PM state each tick (~15 min) and fire ONE
    alert when the borrow-capacity utilisation ratio CROSSES UP into a higher
    band: 🟢 CALM <0.40 · 🟡 WARN >=0.40 · 🟠 STRESS >=0.70 · 🔴 LIQ-RISK >=0.85
    (0.95 = liquidation). The same band never re-alerts; a retreat resets the
    SQLite state silently so the next genuine cross re-fires. The naked-long
    guard (debt drawn, no shorts) fires on its own edge regardless of band.

    R-SILENT: only LIQ-RISK (CRITICAL) and the naked-long alert break silence
    unconditionally; WARN/STRESS are suppressed while silent mode is on (state
    still advances silently). This is the SINGLE PM monitor — R-PMALERT extends
    it, it does not add a competing job. Fully wrapped — never crashes.
    """
    if os.getenv("PM_MONITOR_ENABLED", "true").strip().lower() == "false":
        return
    chat_id = TELEGRAM_CHAT_ID
    if not chat_id:
        return
    try:
        from config import PM_PRIMARY_WALLET
        from modules.portfolio import fetch_all_wallets
        from modules.portfolio_margin import compute_pm_state
        from modules import pm_alert_monitor as _pma
        from modules.hl_prices import get_oracle_prices

        wallets = await fetch_all_wallets()
        prices = get_oracle_prices()
        pmw = (PM_PRIMARY_WALLET or "").lower()
        primary = None
        for w in wallets:
            if isinstance(w, dict) and w.get("status") == "ok":
                d = w.get("data") or {}
                if (d.get("wallet") or "").lower() == pmw:
                    primary = d
                    break
        if primary is None:
            return
        try:
            from modules.hl_borrow_lend import get_collateral_ltv_map
            _ltv = get_collateral_ltv_map()
        except Exception:  # noqa: BLE001
            _ltv = {}
        try:
            _cmm = float(primary.get("cross_maintenance_margin_used") or 0.0)
        except (TypeError, ValueError):
            _cmm = 0.0
        pm = compute_pm_state(
            primary.get("spot_balances") or [],
            primary.get("positions") or [],
            prices,
            ltv_map=_ltv,
            perp_cross_mm=_cmm,
        )
        # R-PMALERT: edge-trigger via SQLite. evaluate() persists the new state
        # (so a retreat resets silently) and returns the rendered alert.
        decision = _pma.evaluate(pm)
        if decision.should_alert:
            # R-SILENT gate: WARN/STRESS stay silent while silent mode is on;
            # CRITICAL (LIQ-RISK) and naked-long break silence unconditionally.
            silent = False
            try:
                from auto.silent_mode import is_silent
                silent = is_silent()
            except Exception:  # noqa: BLE001
                silent = False
            allowed = (not silent) or decision.breaks_silence
            if allowed:
                await send_bot_message(application.bot, chat_id, decision.message)
                log.info("PM alert fired: reason=%s level=%s naked=%s ratio=%.3f",
                         decision.reason, decision.level, decision.naked_long,
                         pm.ratio)
            else:
                log.info("PM alert %s suppressed by silent mode (level=%s)",
                         decision.reason, decision.level)
    except Exception:  # noqa: BLE001
        log.exception("PM monitor job failed")


async def _farmdump_block(w, m, current_funding: float, fraction: float) -> str:
    """Build the appended '5 CHECKS' block for a fired/queried reversion.

    Returns the rendered block, or '' if the checks engine fails entirely (the
    bare reversion alert still fires). Never raises.
    """
    from modules import farmdump_checks as _fd
    from modules import variational_alerts as _va

    try:
        pct_rev = _va.pct_reverted(w.baseline_funding, current_funding)
        result = await _fd.run_checks_safe(
            w.ticker,
            w.baseline_funding,
            current_funding,
            var_price=getattr(m, "mark_price", None),
            var_vol_24h=getattr(m, "volume_24h", None),
            var_oi_usd=getattr(m, "open_interest_usd", None),
            pct_reverted=pct_rev,
        )
        if result is None:
            return ""
        return _fd.format_checks_block(result)
    except Exception:  # noqa: BLE001
        log.exception("farmdump: _farmdump_block failed for %s", getattr(w, "ticker", "?"))
        return ""


@authorized
@with_error_logging
async def cmd_variationalcheck(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run the 5 Farm-the-DUMP pre-trade checks on demand for a ticker.

    Uses the ticker's CURRENT Variational funding as both the live reading and
    (absent a registered watch) the baseline, so BCD can vet a setup any time.
    If a watch exists, its registered baseline is used for the documentation
    line. Recommendation only — the bot never trades.
    """
    from modules import variational as _var
    from modules import variational_alerts as _va

    args = [a.strip() for a in (context.args or []) if a.strip()]
    if not args:
        await update.message.reply_text(
            "Usage: /variationalcheck <TICKER>\nEj: /variationalcheck PORTAL",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    ticker = args[0].upper()
    await update.message.reply_text(
        f"⏳ Corriendo los 5 checks Farm the DUMP para {ticker}...",
        reply_markup=MAIN_KEYBOARD,
    )
    try:
        market = await _var.get_market(ticker)
    except _var.VariationalError as exc:
        await update.message.reply_text(
            f"⚠️ Variational n/a — {exc}", reply_markup=MAIN_KEYBOARD
        )
        return
    except Exception as exc:  # noqa: BLE001
        log.exception("/variationalcheck failed")
        await update.message.reply_text(
            f"❌ Error inesperado: {str(exc)[:200]}", reply_markup=MAIN_KEYBOARD
        )
        return

    if market is None:
        await update.message.reply_text(
            f"❌ {ticker} no figura en Variational ahora mismo. Verificá el ticker "
            f"o probá /variationalfunding.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    current = market.annualized_pct
    watch = await asyncio.to_thread(_va.get_watch, ticker)
    baseline = watch.baseline_funding if watch is not None else current

    # Reuse the shared block builder via a lightweight shim object for the watch.
    class _W:  # noqa: N801 — tiny adapter, not a public type
        pass
    w = _W()
    w.ticker = ticker
    w.baseline_funding = baseline

    fraction = _va.reversion_fraction()
    block = await _farmdump_block(w, market, current, fraction)
    header = (
        f"🔎 VARIATIONAL CHECK — {ticker}\n"
        f"Baseline: {baseline:,.0f}%  →  Current: {current:,.0f}% anual\n"
    )
    if not block:
        await update.message.reply_text(
            header + "\n⚠️ No pude correr los checks (datos n/a).",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    await send_long_message(update, header + "\n" + block, reply_markup=MAIN_KEYBOARD)


@authorized
@with_error_logging
async def cmd_pm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-PMCORE — Portfolio Margin state of the primary account.

    Shows HYPE collateral value, debt drawn, borrow capacity (LTV 0.50),
    margin ratio with WARN/STRESS/LIQ thresholds, and the naked-long hedge
    guard. Read-only; the bot never trades.
    """
    from config import PM_PRIMARY_WALLET
    from modules.portfolio import fetch_all_wallets
    from modules.portfolio_margin import compute_pm_state, format_pm_state_telegram
    from modules.hl_prices import get_oracle_prices

    await update.message.reply_text("⏳ Leyendo Portfolio Margin...", reply_markup=MAIN_KEYBOARD)
    try:
        wallets = await fetch_all_wallets()
        prices = get_oracle_prices()
        pmw = (PM_PRIMARY_WALLET or "").lower()
        primary = None
        for w in wallets:
            if isinstance(w, dict) and w.get("status") == "ok":
                d = w.get("data") or {}
                if (d.get("wallet") or "").lower() == pmw:
                    primary = d
                    break
        if primary is None:
            await update.message.reply_text(
                "⚠️ No encontré la wallet primaria de Portfolio Margin.",
                reply_markup=MAIN_KEYBOARD,
            )
            return
        try:
            from modules.hl_borrow_lend import get_collateral_ltv_map
            _ltv = get_collateral_ltv_map()
        except Exception:  # noqa: BLE001
            _ltv = {}
        try:
            _cmm = float(primary.get("cross_maintenance_margin_used") or 0.0)
        except (TypeError, ValueError):
            _cmm = 0.0
        pm = compute_pm_state(
            primary.get("spot_balances") or [],
            primary.get("positions") or [],
            prices,
            ltv_map=_ltv,
            perp_cross_mm=_cmm,
        )
        # R-NOISE-CUT: ex-MARGIN-STRESS perp cross utilization → INFORMATIONAL
        # panel line only (never a push).
        try:
            from modules.alerts_margin import perp_cross_utilization
            _putil, _pn = perp_cross_utilization(primary)
        except Exception:  # noqa: BLE001
            _putil, _pn = None, 0
        block = format_pm_state_telegram(
            pm, perp_cross_util_pct=_putil, perp_cross_count=_pn
        ) or "⚠️ Sin datos de Portfolio Margin."
        await send_long_message(update, block, reply_markup=MAIN_KEYBOARD)
    except Exception as exc:  # noqa: BLE001
        log.exception("/pm failed")
        await update.message.reply_text(
            f"❌ Error leyendo Portfolio Margin: {str(exc)[:200]}",
            reply_markup=MAIN_KEYBOARD,
        )


@authorized
@with_error_logging
async def cmd_vaults(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-PMCORE — per-vault breakdown + evolution, each vault tracked alone."""
    from modules.vault_deposits import fetch_vault_deposits, format_vault_deposits_telegram
    from modules.vault_history import format_vault_evolution_block

    await update.message.reply_text("⏳ Leyendo vault deposits...", reply_markup=MAIN_KEYBOARD)
    try:
        result = await asyncio.to_thread(fetch_vault_deposits, True)
        parts = [format_vault_deposits_telegram(result)]
        try:
            evo = format_vault_evolution_block(result)
            if evo:
                parts.append("")
                parts.append(evo)
        except Exception:  # noqa: BLE001
            pass
        text = "\n".join(p for p in parts if p) or "Sin vault deposits configurados/encontrados."
        await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)
    except Exception as exc:  # noqa: BLE001
        log.exception("/vaults failed")
        await update.message.reply_text(
            f"❌ Error leyendo vaults: {str(exc)[:200]}", reply_markup=MAIN_KEYBOARD
        )


@authorized
@with_error_logging
async def cmd_unlockcheck(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-SCREEN — universal SHORT/LONG screener over the FULL tradeable universe.

    Broadens the EXACT same squeeze-first 5-gate pre-filter (data-quality,
    z>=floor+persistence, Hurst<0.5, squeeze/momentum guard, funding>=0) from the
    11-name watchlist to EVERY perp tradeable on Hyperliquid + Variational
    (deduped by ticker, venue + liquidity annotated). Ranks them most→least
    shortable (pass-count → squeeze forced to the bottom → z+/Hurst tiebreak),
    surfaces a clearly-separated LONG-context read (mirror setup; tactical/your
    call + AiPear, never a mandate), and lists data-insufficient names
    separately. Read-only and recommendation-only — the bot never selects tokens,
    sizes, or trades; only a 5/5 + AiPear is the human's call. Cointegration is a
    labelled CONTEXT-ONLY proxy that does not gate.

    The basket-unlock ladder (>=4 names) and R-SIGNAL still run in the scheduler;
    this command is the on-demand universal screener.
    """
    from modules import universal_screener as _scr

    await update.message.reply_text(
        "⏳ Screeneando el universo completo (HL + Variational, 5 gates)…",
        reply_markup=MAIN_KEYBOARD,
    )
    try:
        # Pure read: do NOT advance the persistence counters on a manual check.
        res = await _scr.compute_screen(advance_state=False)
        # R-SCREEN-TELEMETRY: attach the compact telemetry block under each 5/5
        # GO candidate (best-effort — never breaks the screener render).
        tel_blocks: dict = {}
        tel_note = None
        try:
            from modules import telemetry as _tel
            tel_blocks, tel_note, _ = await _tel.render_go_telemetry(res)
        except Exception:  # noqa: BLE001
            log.exception("/unlockcheck telemetry failed (non-fatal)")
            tel_blocks, tel_note = {}, None
        text = _scr.format_screen(res, telemetry_blocks=tel_blocks,
                                  telemetry_note=tel_note)
        await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)
    except Exception as exc:  # noqa: BLE001
        log.exception("/unlockcheck failed")
        await update.message.reply_text(
            f"❌ Error calculando R-SCREEN: {str(exc)[:200]}",
            reply_markup=MAIN_KEYBOARD,
        )


@authorized
@with_error_logging
async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-SCREEN per-token — run the SAME five-gate engine on ONE requested ticker.

    Usage: /check <TICKER> (e.g. /check WLD). Returns the per-gate pass/fail with
    real values, the shortability verdict (e.g. "SHORT: NO-GO — squeeze activo,
    RSI 73 + HH" or "SHORT: 5/5 GO candidate — confirmá con AiPear"), and the
    long-viability read ("LONG: no viable" / "LONG context: sobrevendido +
    funding<0"). If the token isn't tradeable on HL/Variational or has
    insufficient candle data, says so plainly. Pure read; never selects or trades.
    """
    from modules import universal_screener as _scr

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Uso: /check <TICKER>  (ej. /check WLD)", reply_markup=MAIN_KEYBOARD
        )
        return
    ticker = args[0].strip().upper().lstrip("$")
    await update.message.reply_text(
        f"⏳ Screeneando {ticker} (5 gates: short + long)…", reply_markup=MAIN_KEYBOARD
    )
    try:
        # Pure read: single-token query never advances the persistence counters.
        row, status = await _scr.check_single(ticker)
        text = _scr.format_check(row, status, ticker)
        await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)
    except Exception as exc:  # noqa: BLE001
        log.exception("/check failed")
        await update.message.reply_text(
            f"❌ Error en /check {ticker}: {str(exc)[:200]}",
            reply_markup=MAIN_KEYBOARD,
        )


@authorized
@with_error_logging
async def cmd_telemetry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-TELEMETRY — per-token telemetry block for AiPear basket sizing.

    Usage: /telemetry TICKER1 TICKER2 …  (space- or comma-separated, 1-8). For
    each token pulls, straight from the Hyperliquid info API (same shared
    rate-limited + cached client R-SCREEN uses): (1) funding live + 7d avg
    (hourly % and APR, PAYS/RECEIVES for a short), (2) OI notional vs 24h volume
    + ratio, (3) distance above the 7-day low, (4) top-of-book resting notional
    within ±0.5%/±1.0% of mid (bid+ask), and (5) squeeze state + fails-first
    gate + z + Hurst from the SAME R-SCREEN 5-gate engine. Accuracy over
    completeness — any single feed that fails prints n/d for THAT metric only,
    never fabricated nor 0-filled. Pure read; the bot never selects or sizes.
    """
    from modules import telemetry as _tel

    tickers, parse_notes = _tel.parse_tickers(context.args)
    if not tickers:
        await update.message.reply_text(
            "Uso: /telemetry TICKER1 TICKER2 …  (1-8, espacio o coma; ej. "
            "/telemetry BTC HYPE WLD)"
            + (f"\n{'; '.join(parse_notes)}" if parse_notes else ""),
            reply_markup=MAIN_KEYBOARD,
        )
        return
    await update.message.reply_text(
        f"⏳ Telemetría HL para {', '.join(tickers)} (funding/OI/depth/squeeze)…",
        reply_markup=MAIN_KEYBOARD,
    )
    try:
        tokens = await _tel.build_telemetry(tickers)
        text = _tel.format_telemetry(tokens, parse_notes)
        await send_long_message(update, text, reply_markup=MAIN_KEYBOARD)
    except Exception as exc:  # noqa: BLE001
        log.exception("/telemetry failed")
        await update.message.reply_text(
            f"❌ Error en /telemetry: {str(exc)[:200]}",
            reply_markup=MAIN_KEYBOARD,
        )


@authorized
@with_error_logging
async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-SIGNAL — per-name short signals (orthogonal to the >=4 R-UNLOCK ladder).

    Runs the SAME R-UNLOCK-PRECISION 5-sub-gate engine and returns the CURRENT
    set of watchlist names that pass ALL five gates right now (z>=+1.00
    persistent, Hurst<=0.47, squeeze CLEAR, funding>=0, data>=90%). Fires on ANY
    individual qualifier — the fund confirms each 5/5 with AiPear before adding
    it to the short book one at a time. If zero qualify, says so with the count.
    Read-only and recommendation-only — the bot never selects tokens or sizes.
    Pure read: does NOT advance the debounce counters nor burn the announce edge.
    """
    from modules import unlock_monitor as _ul
    from modules import signal_monitor as _sig

    await update.message.reply_text(
        "⏳ Calculando señales por nombre (filtro 5-gates)…", reply_markup=MAIN_KEYBOARD
    )
    try:
        snap = await _ul.compute_snapshot(advance_state=False)
        res = _sig.evaluate_signals(snap, advance_state=False)
        await send_long_message(update, _sig.format_signals(res), reply_markup=MAIN_KEYBOARD)
    except Exception as exc:  # noqa: BLE001
        log.exception("/signals failed")
        await update.message.reply_text(
            f"❌ Error calculando R-SIGNAL: {str(exc)[:200]}",
            reply_markup=MAIN_KEYBOARD,
        )


async def cmd_halts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-AUDIT2-P1.3 — show ACTIVE 🛑 INTEGRITY-HALT flags. Read-only.

    These are MANUAL-REVIEW alerts (integrity rumor + adverse PnL on a held
    asset). They never auto-clear; clear one with /haltclear <ASSET>.
    """
    try:
        from modules.integrity_halt import get_active_flags, build_integrity_block
        flags = get_active_flags()
        if not flags:
            await update.message.reply_text(
                "✅ Sin INTEGRITY-HALT activos.", reply_markup=MAIN_KEYBOARD
            )
            return
        await send_long_message(
            update, build_integrity_block(flags), reply_markup=MAIN_KEYBOARD
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("/halts failed")
        await update.message.reply_text(
            f"❌ Error leyendo INTEGRITY-HALT: {str(exc)[:200]}",
            reply_markup=MAIN_KEYBOARD,
        )


async def cmd_haltclear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """R-AUDIT2-P1.3 — explicit BCD dismissal of an INTEGRITY-HALT flag.

    Usage: /haltclear <ASSET>. The ONLY way a flag clears (a shielded-asset
    flag NEVER auto-clears on absence of confirmation). BCD's call.
    """
    try:
        from modules.integrity_halt import dismiss
        args = (context.args or [])
        if not args:
            await update.message.reply_text(
                "Uso: /haltclear <ASSET> (p.ej. /haltclear ZEC). Cierre manual de BCD.",
                reply_markup=MAIN_KEYBOARD,
            )
            return
        asset = str(args[0]).upper()
        cleared = dismiss(asset, resolution="BCD dismissal via /haltclear")
        if cleared:
            await update.message.reply_text(
                f"🛑→✅ INTEGRITY-HALT de {asset} cerrado por BCD.",
                reply_markup=MAIN_KEYBOARD,
            )
        else:
            await update.message.reply_text(
                f"No había INTEGRITY-HALT activo para {asset}.",
                reply_markup=MAIN_KEYBOARD,
            )
    except Exception as exc:  # noqa: BLE001
        log.exception("/haltclear failed")
        await update.message.reply_text(
            f"❌ Error cerrando INTEGRITY-HALT: {str(exc)[:200]}",
            reply_markup=MAIN_KEYBOARD,
        )


async def _unlock_monitor_job(application: Application) -> None:
    """R-UNLOCK — basket-unlock watchdog (actionable-only, R-SILENT aware).

    Every ~30 min: recompute the A/B/C state. R-NOISE-CUT (2026-06-16): PUSH a
    Telegram alert ONLY on a genuine cross into the terminal actionable state
    (UNLOCK = the re-screen GO trigger). Every intermediate level — WATCH
    ("ablandándose"), APPROACHING ("acercándose"), NONE — is SILENT: the level
    is still recomputed and persisted (so transitions keep tracking and
    /unlockcheck reflects them on demand) but nothing is pushed. No more
    in-between softening chatter. A retreat resets the stored level silently so
    the next genuine UNLOCK cross can fire again.

    The pushed message is CONCISE and leads with the side (READY TO SHORT) +
    the condition that crossed + one caveat (format_actionable_alert). Fully
    wrapped — never crashes the scheduler.
    """
    if os.getenv("UNLOCK_MONITOR_ENABLED", "true").strip().lower() == "false":
        return
    chat_id = TELEGRAM_CHAT_ID
    if not chat_id:
        return
    try:
        from modules import unlock_monitor as _ul

        prev = _ul.load_state().get("level", _ul.NONE)
        snap = await _ul.compute_snapshot()
        new_level = snap.level
        # R-NOISE-CUT: push ONLY on the terminal actionable cross (UNLOCK).
        # Intermediate softening states advance state below but never push.
        fire = _ul.should_push_actionable(new_level, prev)

        if fire:
            # R-SILENT gate kept for parity; UNLOCK is at/above the default
            # break-silence threshold so it pages even under silent mode.
            silent = False
            try:
                from auto.silent_mode import is_silent
                silent = is_silent()
            except Exception:  # noqa: BLE001
                silent = False
            rank = _ul._LEVEL_RANK
            min_break = _ul.alert_breaks_silence_level()
            allowed = (not silent) or (rank.get(new_level, 0) >= rank.get(min_break, 3))
            if allowed:
                msg = _ul.format_actionable_alert(snap, prev)
                await send_bot_message(application.bot, chat_id, msg)
                log.info("R-UNLOCK actionable alert fired: %s → %s (triggered=%d)",
                         prev, new_level, snap.n_counts)
            else:
                log.info("R-UNLOCK %s suppressed by silent mode (min_break=%s)",
                         new_level, min_break)
        else:
            # Log-only: make the silent intermediate state observable in Railway
            # logs without paging the operator.
            log.info("R-UNLOCK level=%s (prev=%s) — no push (non-actionable)",
                     new_level, prev)

        # Persist the level transition (escalation OR silent retreat) without
        # disturbing the rolling series already saved by compute_snapshot().
        try:
            cur = _ul.load_state()
            _ul.save_state(new_level, cur.get("btc_z_deep", False),
                           cur.get("vol_series", []), cur.get("btcd_series", []))
        except Exception:  # noqa: BLE001
            log.exception("R-UNLOCK: level persist failed")

        # ── R-SIGNAL (orthogonal per-name trigger) ─────────────────────────
        # Reuse the SAME snapshot — no second data pull. Advances each name's
        # debounce streak + announce flag and fires ONE alert (breaking
        # R-SILENT) only when >=1 name NEWLY passes all 5 gates. Independent of
        # whether the >=4 UNLOCK ladder fired above; fully wrapped.
        try:
            if os.getenv("SIGNAL_MONITOR_ENABLED", "true").strip().lower() != "false":
                from modules import signal_monitor as _sig

                res = _sig.evaluate_signals(snap, advance_state=True)
                if res.fire:
                    await send_bot_message(
                        application.bot, chat_id, _sig.format_alert(res)
                    )
                    log.info("R-SIGNAL alert fired: new=%s total_qual=%d",
                             ",".join(res.new_names), len(res.qualifying))
        except Exception:  # noqa: BLE001
            log.exception("R-SIGNAL emission failed (non-fatal)")

        # ── R-SCREEN (universal screener) — advance z-persistence over the FULL
        # universe so /unlockcheck and /check can reach 5/5 over time exactly the
        # way the watchlist does. SILENT: emits NOTHING (R-SILENT safe), separate
        # SQLite table, never touches the watchlist trigger state. Fully wrapped.
        try:
            if os.getenv("SCREENER_MONITOR_ENABLED", "true").strip().lower() != "false":
                from modules import universal_screener as _scr

                n_ranked = await _scr.advance_universe_state()
                log.info("R-SCREEN universe state advanced (%d ranked)", n_ranked)
        except Exception:  # noqa: BLE001
            log.exception("R-SCREEN universe advance failed (non-fatal)")
    except Exception:  # noqa: BLE001
        log.exception("R-UNLOCK monitor job failed")


# ─── Round 17 scheduler jobs ────────────────────────────────────────────────


async def _macro_calendar_job(application: Application) -> None:
    """R17/R20: every 1 min — fire T-24h/T-2h/T-30m alerts for upcoming events.

    R20: when TIME_AWARENESS_ENABLED=true (default), routes to v2 scheduler
    that recomputes "in X hours" at SEND time and filters past events
    defensively. Set TIME_AWARENESS_ENABLED=false to roll back to v1.
    """
    if os.getenv("MACRO_CALENDAR_ENABLED", "true").strip().lower() == "false":
        return
    # R-ONDEMAND: catalyst nudges (T-24/T-2/T-30) silenced unless explicit BCD opt-in.
    try:
        from modules.cron_state import catalyst_nudge_enabled
        if not catalyst_nudge_enabled():
            return
    except Exception:  # noqa: BLE001
        pass
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
    """R17: every 5 min — evaluate the 3 kill triggers, edge-trigger Telegram alerts."""
    if os.getenv("KILL_TRIGGERS_ENABLED", "true").strip().lower() == "false":
        return
    try:
        await kill_scheduled_check(application.bot)
    except Exception:  # noqa: BLE001
        log.exception("kill triggers job failed")


async def _go_alerts_job(application: Application) -> None:
    """R-SIGNAL-DIET: hourly 5/5 GO entry alerts — the ONE proactive trading
    signal the bot pushes. Diff-based (new entrants only) + 6h cooldown +
    grouping >5 + 3-strike failure gate; all logic in modules.go_alerts."""
    try:
        from modules.go_alerts import run_go_alert_cycle
        await run_go_alert_cycle(application.bot)
    except Exception:  # noqa: BLE001
        log.exception("go_alerts job failed (non-fatal)")


# R-BOT-DEFINITIVE-KILLCLEAN (2026-06-15): _rates_monitor_job ELIMINADO.
# Monitoreaba UETH borrow APY + HF de HyperLend (fuente muerta). El riesgo de
# HF vivo (aave-HF del PM) lo cubre el canal real-risk de modules.alerts_margin.


async def _weekly_summary_job(application: Application) -> None:
    """R17: Sunday 18:00 UTC — weekly performance summary."""
    # R-ONDEMAND: weekly auto-broadcast falls under REPORT_CRON_ENABLED.
    try:
        from modules.cron_state import report_cron_enabled
        if not report_cron_enabled():
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        await weekly_scheduled_summary(application.bot)
    except Exception:  # noqa: BLE001
        log.exception("weekly summary job failed")


async def _pat_expiry_job(application: Application) -> None:
    """R-PAT-RENEW: daily — alert when the GitHub PAT is <=14d from expiry.

    SAFETY gate (PAT_ALERT_ENABLED, default true): the fund cannot operate
    with the bot blind to its own deploy credentials, so this stays on by
    default like HF_PRELIQ. Deduped to once per UTC day.
    """
    try:
        from modules.pat_status import (
            get_pat_status,
            should_send_alert,
            record_alert_sent,
            format_pat_status_block,
        )
    except Exception:  # noqa: BLE001
        log.exception("pat_expiry job: module import failed")
        return
    try:
        status = get_pat_status(force_refresh=True)
        if should_send_alert(status):
            msg = (
                "\U0001f6a8 ALERTA — GitHub PAT por expirar\n\n"
                f"{format_pat_status_block(status)}\n\n"
                "Acción: renovar el PAT y actualizar GITHUB_TOKEN en Railway. "
                "Sin esto el push autónomo y el backup a GitHub se caen."
            )
            if TELEGRAM_CHAT_ID:
                await send_bot_message(application.bot, TELEGRAM_CHAT_ID, msg)
            record_alert_sent()
            log.warning("PAT expiry alert sent (days_left=%s)", status.get("days_left"))
    except Exception:  # noqa: BLE001
        log.exception("pat_expiry job failed")


# ─── R-PERFECT Phase 4 scheduler jobs ───────────────────────────────────────


async def _selftest_cron_job(application: Application) -> None:
    """R-PERFECT Fase 4: 4x/day /selftest + flap evaluation.

    R-SIGNAL-DIET (2026-07-20): source flap/recovery reports son ruido de
    proceso interno — van SOLO a logs (Railway), NUNCA a Telegram. El estado
    por fuente se sigue persistiendo (source_state.db) y es visible on-demand
    vía /intel_sources y el health server."""
    if os.getenv("SELFTEST_CRON_ENABLED", "true").strip().lower() == "false":
        return
    try:
        from modules.intel_selftest import run_selftest
        from modules.source_alerts import evaluate_matrix
        matrix = await run_selftest()
        alerts = evaluate_matrix(matrix)
        for a in alerts:
            log.warning("source flap (log-only): %s", a)
    except Exception:  # noqa: BLE001
        log.exception("selftest cron job failed")


async def _backup_volume_job(application: Application) -> None:
    """R-PERFECT Fase 4: daily 04:00 UTC — gzip /app/data, prune 30d, optional GH push."""
    if os.getenv("BACKUP_VOLUME_ENABLED", "true").strip().lower() == "false":
        return
    try:
        from modules.backup_volume import run_backup
        loop = asyncio.get_event_loop()
        # tarfile is sync; offload to a thread so we don't block the event loop
        result = await loop.run_in_executor(None, run_backup)
        if not result.get("ok"):
            chat_id = os.getenv("ALERT_CHAT_ID") or os.getenv("AUTHORIZED_USER_ID")
            if chat_id:
                try:
                    await application.bot.send_message(
                        chat_id=int(chat_id),
                        text=f"📦 backup FAILED: {result.get('reason','?')[:120]}",
                    )
                except Exception:  # noqa: BLE001
                    log.exception("backup failure alert send failed")
    except Exception:  # noqa: BLE001
        log.exception("backup_volume cron job failed")


async def _cost_alert_job(application: Application) -> None:
    """R-PERFECT Fase 3 #3: hourly LLM cost threshold check, alert if breached."""
    if os.getenv("COST_ALERTS_ENABLED", "true").strip().lower() == "false":
        return
    try:
        from modules.cost_tracker import check_alert_thresholds
        msg = check_alert_thresholds()
        if msg:
            chat_id = os.getenv("ALERT_CHAT_ID") or os.getenv("AUTHORIZED_USER_ID")
            if chat_id:
                try:
                    await application.bot.send_message(
                        chat_id=int(chat_id), text=msg, parse_mode="Markdown"
                    )
                except Exception:  # noqa: BLE001
                    log.exception("cost alert send failed")
    except Exception:  # noqa: BLE001
        log.exception("cost_alert cron job failed")


async def _lmec_weekly_recheck_job(application: Application) -> None:
    """R-BOT-LMEC-AUTOFEED: domingo 00:00 UTC — recheck + alert on flip.

    1. Pull tradermap (best-effort) so the validator updates the
       failure streak.
    2. Re-evaluate the 4 LMEC legs.
    3. If any leg flipped from non-VALIDA → VALIDA on this evaluation,
       send a critical Telegram alert. Idempotent: lmec_state already
       guards against duplicate flips.
    """
    try:
        # Warm tradermap state + record schema validation outcome.
        try:
            from modules.tradermap import fetch_tradermap_btc
            from modules.tradermap_validator import record_outcome

            payload = await fetch_tradermap_btc()
            record_outcome(payload)
        except Exception:  # noqa: BLE001
            log.exception("LMEC weekly: tradermap warm failed (non-fatal)")
        # R-LMEC-AUTOCOMPUTE: refresh the computed weekly TA snapshot on the
        # weekly close BEFORE flip detection, so legs 2/3/4 reflect the new
        # closed candle without any manual /setlmec step.
        try:
            from modules.btc_weekly_indicators import refresh_and_persist

            await refresh_and_persist()
        except Exception:  # noqa: BLE001
            log.exception("LMEC weekly: indicator refresh failed (non-fatal)")
        from modules.lmec_triggers import detect_and_alert_flips
        from modules.market import fetch_market_data

        market = None
        try:
            market = await fetch_market_data()
        except Exception:  # noqa: BLE001
            log.exception("LMEC weekly: market fetch failed (non-fatal)")
        out = detect_and_alert_flips(market)
        flips = out.get("flips") or []
        text = out.get("alert_text") or ""
        chat_id = TELEGRAM_CHAT_ID
        if not chat_id:
            log.warning("LMEC weekly: TELEGRAM_CHAT_ID unset, alert suppressed")
            return
        if flips and text:
            await send_bot_message(application.bot, chat_id, text)
            log.info("LMEC weekly: alert sent for flips=%s", flips)
        else:
            log.info("LMEC weekly: no flips detected (state stable)")
    except Exception:  # noqa: BLE001
        log.exception("LMEC weekly job failed")


async def _lmec_counter_refresh_job() -> None:
    """R-BOT-LMEC-AUTOFEED: every 6h — keep weeks-broken counter warm.

    Calls evaluate_lmec_triggers() so the counter is kept in sync even
    when /reporte hasn't been issued recently. Lightweight — no
    Telegram messaging. Failure is swallowed.
    """
    # R-ONDEMAND gate: LMEC counter refresh is part of the background
    # intel-autopull surface (silent helper that keeps state warm). The
    # weekly Sunday recheck is *not* gated — flip alerts must keep firing
    # since they are catalyst-critical.
    try:
        from modules.cron_state import intel_autopull_enabled
        if not intel_autopull_enabled():
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        # R-LMEC-AUTOCOMPUTE: recompute the weekly MACD/RSI/MA50w snapshot from
        # real closed weekly candles BEFORE evaluating, so legs 2/3/4 read the
        # bot's own computed values (no manual /setlmec needed).
        try:
            from modules.btc_weekly_indicators import refresh_and_persist

            await refresh_and_persist()
        except Exception:  # noqa: BLE001
            log.exception("LMEC weekly indicator refresh failed (non-fatal)")

        from modules.lmec_triggers import evaluate_lmec_triggers
        from modules.market import fetch_market_data

        market = None
        try:
            market = await fetch_market_data()
        except Exception:  # noqa: BLE001
            return
        evaluate_lmec_triggers(market)
    except Exception:  # noqa: BLE001
        log.exception("LMEC counter refresh job failed")


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


# R-SIGNAL-DIET (2026-07-20): _heartbeat_job ELIMINADO — cero pushes de
# heartbeat. Ver cmd_health (on-demand).


def _risk_validator_state_path() -> str:
    """Path of the persisted drift-alert dedup state (survives restarts)."""
    try:
        from config import DATA_DIR as _dd
    except Exception:  # noqa: BLE001
        _dd = os.getenv("DATA_DIR", "/tmp")
    return os.path.join(_dd, "risk_validator_state.json")


def _risk_validator_should_alert(fingerprint: str, *, now: float | None = None) -> bool:
    """Edge-trigger + cooldown for the drift alert (R-RISK-VALIDATOR-HOTFIX).

    At a 5-minute cadence a standing drift would otherwise re-fire 288x/day.
    Fire only when the failure fingerprint CHANGES or when
    RISK_VALIDATOR_REALERT_HOURS (default 6h) elapsed since the last send.
    State is persisted to DATA_DIR so a restart never re-fires an unchanged
    drift inside the cooldown window. NEVER raises.
    """
    import json as _json
    ts = float(now if now is not None else time.time())
    try:
        realert_sec = float(os.getenv("RISK_VALIDATOR_REALERT_HOURS", "6") or 6) * 3600.0
    except ValueError:
        realert_sec = 6 * 3600.0
    path = _risk_validator_state_path()
    prev_fp, prev_ts = None, 0.0
    try:
        with open(path, encoding="utf-8") as fh:
            prev = _json.load(fh)
        prev_fp = prev.get("fingerprint")
        prev_ts = float(prev.get("sent_at") or 0.0)
    except Exception:  # noqa: BLE001 — first run / corrupt state → treat as new
        pass
    if fingerprint == prev_fp and (ts - prev_ts) < realert_sec:
        return False
    try:
        with open(path, "w", encoding="utf-8") as fh:
            _json.dump({"fingerprint": fingerprint, "sent_at": ts}, fh)
    except Exception:  # noqa: BLE001
        log.exception("risk_validator state persist failed (non-fatal)")
    return True


async def _risk_validator_job(application: Application) -> None:
    """R-RISK-VALIDATOR-HOTFIX: every 5min — auto-run risk_check; alert on FAIL.

    Read-only. Surfaces drift in env-var policy gates so BCD doesn't have
    to remember to /risk_check manually. Silent on PASS. Drift alerts are
    edge-triggered (fingerprint change) with a 6h re-alert cooldown,
    persisted across restarts. Logs ONE INFO line per successful execution
    (job name, duration, findings count) so liveness is verifiable in
    Railway logs.
    """
    if r18_risk_check_report is None or not r18_risk_check_enabled():
        return
    _t0 = time.monotonic()
    try:
        from modules.risk_config_validator import run_checks as _rcv_run
        results = _rcv_run()
        failures = [c for c in results if not c.ok]
        if failures:
            chat_id = TELEGRAM_CHAT_ID
            fingerprint = "|".join(sorted(f"{c.name}:{c.detail}" for c in failures))
            if chat_id and _risk_validator_should_alert(fingerprint):
                lines = ["\u26a0\ufe0f RISK CONFIG DRIFT \u2014 auto-detect"]
                for c in failures:
                    lines.append(f"  \u2022 {c.name}: {c.detail} (expected: {c.expected})")
                lines.append("")
                lines.append("Run /risk_check for details, adjust env vars in Railway.")
                await send_bot_message(application.bot, chat_id, "\n".join(lines))
        log.info(
            "risk_validator_job OK — duration=%.2fs findings=%d",
            time.monotonic() - _t0,
            len(failures),
        )
    except Exception:  # noqa: BLE001
        log.exception("risk_validator job failed")


async def _cryexc_monitor_job(application: Application) -> None:
    """R18: every 30min — cryexc snapshot + fire alert on new notable events."""
    if not cryexc_is_enabled() or not cryexc_monitor_is_enabled():
        return
    # R-ONDEMAND: cryexc proactive event push is a catalyst nudge surface.
    try:
        from modules.cron_state import catalyst_nudge_enabled
        if not catalyst_nudge_enabled():
            return
    except Exception:  # noqa: BLE001
        pass
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


# ─── R-ONDEMAND gate-aware wrappers for R18/R21 broadcast schedulers ────────


def _gated_broadcast(coro_factory, gate_fn, label: str):
    """Return an ASYNC callable suitable for APScheduler ``add_job(...)``.

    ``coro_factory`` is a 0-arg callable that, when invoked, returns the
    coroutine to dispatch. ``gate_fn`` is a 0-arg sync callable returning
    True if the broadcast should run. The wrapper short-circuits with a
    debug log when the gate is closed — no Telegram traffic, no exception.

    R-RISK-VALIDATOR-HOTFIX (2026-06-10): the previous SYNC wrapper called
    ``asyncio.create_task(...)``. AsyncIOScheduler dispatches sync callables
    to a thread-pool executor where NO event loop is running, so create_task
    raised RuntimeError("no running event loop") on every cycle and the
    wrapped broadcast NEVER executed. An ``async def`` runner is detected by
    AsyncIOScheduler's iscoroutinefunction check and scheduled natively on
    the bot's running loop — same mechanism as the healthy ``_alert_job``.
    """
    async def _runner() -> None:
        try:
            if not gate_fn():
                log.debug("R-ONDEMAND gate closed: %s skipped", label)
                return
        except Exception:  # noqa: BLE001
            log.exception("R-ONDEMAND gate eval failed for %s (failing open)", label)
        try:
            await coro_factory()
        except Exception:  # noqa: BLE001
            log.exception("R-ONDEMAND broadcast %s failed", label)
    return _runner


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

    # R-BOT-DEFINITIVE WI-1: seed + refresh the catalysts engine (FRED CPI/PPI/
    # NFP + official FOMC calendar + ticket seed events). Non-fatal.
    try:
        from modules.catalysts import refresh_catalysts
        _cat_res = await refresh_catalysts()
        log.info("catalysts engine refreshed at boot: %s", _cat_res)
    except Exception:  # noqa: BLE001
        log.exception("catalysts engine boot refresh failed (non-fatal)")

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
        # R-COST-V2: X timeline scheduler PERMANENTLY REMOVED. X API reads
        # happen ONLY inside /reporte and /xrefresh (incremental since_id).
        log.info("X timeline scheduler: REMOVED (R-COST-V2) — X reads only via /reporte + /xrefresh")

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
        # Kill triggers (BTC>82k 4h, DCA zone, PM aave-HF<1.10, basket DD<-2k) — 5 min
        scheduler.add_job(
            _kill_triggers_job,
            "interval",
            minutes=5,
            args=[application],
            id="kill_triggers",
            max_instances=1,
            coalesce=True,
        )
        # R-BOT-DEFINITIVE-KILLCLEAN (2026-06-15): job "rates_monitor" (UETH
        # borrow APY + HyperLend HF) ELIMINADO — fuente muerta. El riesgo de HF
        # vivo (aave-HF del PM) lo cubre el canal real-risk en alerts_margin.
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

        # R-PAT-RENEW: daily GitHub PAT expiry check at 09:00 UTC.
        # SAFETY job — fires a Telegram alert when the deploy PAT is
        # <=PAT_ALERT_THRESHOLD_DAYS (14) from expiry. Deduped once/UTC-day.
        scheduler.add_job(
            _pat_expiry_job,
            "cron",
            hour=9,
            minute=0,
            args=[application],
            id="pat_expiry_check",
            max_instances=1,
            coalesce=True,
            next_run_time=datetime.now(timezone.utc) + timedelta(minutes=3),
        )

        # R-BOT-LMEC-AUTOFEED: weekly LMEC recheck — Sunday 00:00 UTC.
        # Aligns with weekly chart close. Emits flip-to-VALIDA alerts.
        if os.getenv("LMEC_AUTOFEED_ENABLED", "true").strip().lower() != "false":
            scheduler.add_job(
                _lmec_weekly_recheck_job,
                "cron",
                day_of_week="sun",
                hour=0,
                minute=0,
                args=[application],
                id="lmec_weekly_recheck",
                max_instances=1,
                coalesce=True,
            )
            # Refresh counter every 6h so the weeks-broken streak survives
            # idle weeks where BCD doesn't issue /reporte.
            try:
                _lmec_refresh_hours = float(os.getenv("LMEC_REFRESH_INTERVAL_HOURS", "6"))
            except ValueError:
                _lmec_refresh_hours = 6.0
            scheduler.add_job(
                _lmec_counter_refresh_job,
                "interval",
                hours=_lmec_refresh_hours,
                id="lmec_counter_refresh",
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2),
            )
            log.info(
                "R-BOT-LMEC-AUTOFEED: weekly recheck (Sun 00:00 UTC) + "
                "counter refresh every %.1fh ENABLED",
                _lmec_refresh_hours,
            )
        else:
            log.info("LMEC_AUTOFEED_ENABLED=false → LMEC scheduler DISABLED")

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

        # R-VARIATIONAL: mean-reversion watch checker — every 30 min.
        # Fires ONE material alert per registered watch when funding reverts to
        # baseline × VARIATIONAL_REVERSION_FRACTION. Gated by VARIATIONAL_ALERTS_ENABLED.
        if os.getenv("VARIATIONAL_ALERTS_ENABLED", "true").strip().lower() != "false":
            scheduler.add_job(
                _variational_alerts_job,
                "interval",
                minutes=30,
                args=[application],
                id="variational_alerts",
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=3),
            )
            log.info("Variational reversion-alert scheduler ENABLED (every 30 min)")
        else:
            log.info("Variational reversion-alert scheduler DISABLED (VARIATIONAL_ALERTS_ENABLED=false)")

        # R-PMCORE: Portfolio Margin watchdog — every 15 min. Edge-triggered,
        # breaks R-SILENT only at WARN (ratio 0.40) / naked-long. Gated by
        # PM_MONITOR_ENABLED.
        if os.getenv("PM_MONITOR_ENABLED", "true").strip().lower() != "false":
            scheduler.add_job(
                _pm_monitor_job,
                "interval",
                minutes=int(os.getenv("PM_MONITOR_INTERVAL_MIN", "15")),
                args=[application],
                id="pm_monitor",
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=4),
            )
            log.info("PM monitor scheduler ENABLED (every %s min)",
                     os.getenv("PM_MONITOR_INTERVAL_MIN", "15"))
        else:
            log.info("PM monitor scheduler DISABLED (PM_MONITOR_ENABLED=false)")

        # R-BOT-DEFINITIVE WI-1: catalysts engine daily refresh (FRED release
        # dates + FOMC sync). Cheap, keyless beyond the existing FRED key.
        try:
            from modules.catalysts import refresh_catalysts as _cat_refresh
            scheduler.add_job(
                _cat_refresh,
                "cron",
                hour=int(os.getenv("CATALYSTS_REFRESH_HOUR_UTC", "6")),
                minute=15,
                id="catalysts_refresh",
                max_instances=1,
                coalesce=True,
            )
            log.info("Catalysts engine daily refresh ENABLED (06:15 UTC)")
        except Exception:  # noqa: BLE001
            log.exception("catalysts refresh job registration failed (non-fatal)")

        # R-UNLOCK: basket-entry-unlock watchdog — every 30 min. Edge-triggered
        # (only on escalation NONE→WATCH→APPROACHING→UNLOCK), R-SILENT aware
        # (soft levels stay silent while silent mode is on; UNLOCK breaks it).
        # Gated by UNLOCK_MONITOR_ENABLED.
        if os.getenv("UNLOCK_MONITOR_ENABLED", "true").strip().lower() != "false":
            scheduler.add_job(
                _unlock_monitor_job,
                "interval",
                minutes=int(os.getenv("UNLOCK_MONITOR_INTERVAL_MIN", "30")),
                args=[application],
                id="unlock_monitor",
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            log.info("R-UNLOCK monitor scheduler ENABLED (every %s min)",
                     os.getenv("UNLOCK_MONITOR_INTERVAL_MIN", "30"))
        else:
            log.info("R-UNLOCK monitor scheduler DISABLED (UNLOCK_MONITOR_ENABLED=false)")

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
        # Morning brief — daily 08:00 UTC (R-ONDEMAND: REPORT_CRON_ENABLED gate)
        if r18_morning_scheduled is not None and r18_morning_enabled():
            from modules.cron_state import report_cron_enabled
            scheduler.add_job(
                _gated_broadcast(
                    lambda: r18_morning_scheduled(application),
                    report_cron_enabled,
                    "r18_morning_brief",
                ),
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
            # R-RISK-VALIDATOR-HOTFIX: async coroutine registered natively
            # (the old sync lambda + create_task raised RuntimeError in the
            # thread-pool executor — job never ran).
            scheduler.add_job(
                r18_basket_close_emit,
                "interval",
                seconds=30,
                args=[application.bot],
                id="basket_close_detector",
                max_instances=1,
                coalesce=True,
            )
            log.info("R18 basket_close_detector ENABLED (every 30s)")
        # Compounding detector — every 5 min (R-ONDEMAND: TESIS_CRON_ENABLED)
        if r18_compounding_scheduled is not None and r18_compounding_enabled():
            from modules.cron_state import tesis_cron_enabled
            scheduler.add_job(
                _gated_broadcast(
                    lambda: r18_compounding_scheduled(application.bot),
                    tesis_cron_enabled,
                    "r18_compounding_detector",
                ),
                "interval",
                minutes=5,
                id="compounding_detector",
                max_instances=1,
                coalesce=True,
            )
            log.info("R18 compounding_detector ENABLED (every 5min)")
        # Macro convergence — every 60 min (R-ONDEMAND: TESIS_CRON_ENABLED)
        if r18_convergence_scheduled is not None and r18_convergence_enabled():
            from modules.cron_state import tesis_cron_enabled
            scheduler.add_job(
                _gated_broadcast(
                    lambda: r18_convergence_scheduled(application.bot),
                    tesis_cron_enabled,
                    "r18_macro_convergence",
                ),
                "interval",
                minutes=60,
                id="macro_convergence",
                max_instances=1,
                coalesce=True,
            )
            log.info("R18 macro_convergence ENABLED (every 60min)")
        # Predictive alerts — every 30 min (R-ONDEMAND: TESIS_CRON_ENABLED)
        if r18_predictive_scheduled is not None and r18_predictive_enabled():
            from modules.cron_state import tesis_cron_enabled
            scheduler.add_job(
                _gated_broadcast(
                    lambda: r18_predictive_scheduled(application.bot),
                    tesis_cron_enabled,
                    "r18_predictive_alerts",
                ),
                "interval",
                minutes=30,
                id="predictive_alerts",
                max_instances=1,
                coalesce=True,
            )
            log.info("R18 predictive_alerts ENABLED (every 30min)")
        # Pre-event brief — every 5 min, fires T-90→T-30 window (R-ONDEMAND: CATALYST_NUDGE)
        if r18_preevent_scheduled is not None and r18_preevent_enabled():
            from modules.cron_state import catalyst_nudge_enabled
            scheduler.add_job(
                _gated_broadcast(
                    lambda: r18_preevent_scheduled(application),
                    catalyst_nudge_enabled,
                    "r18_pre_event_brief",
                ),
                "interval",
                minutes=5,
                id="pre_event_brief",
                max_instances=1,
                coalesce=True,
            )
            log.info("R18 pre_event_brief ENABLED (every 5min)")

        # R-SIGNAL-DIET (2026-07-20): heartbeat scheduled push ELIMINADO.
        # /health entrega la misma info on-demand. Cero pushes periódicos.

        # R-SIGNAL-DIET: 5/5 GO entry alerts — cada GO_ALERTS_INTERVAL_MIN
        # (default 60) corre el engine R-SCREEN (mismo code path que
        # /unlockcheck, pure read) y pushea SOLO nuevos entrantes 5/5
        # (cooldown 6h, agrupado, silencio en fallas <3 consecutivas).
        if os.getenv("GO_ALERTS_ENABLED", "true").strip().lower() != "false":
            try:
                go_min = int(os.getenv("GO_ALERTS_INTERVAL_MIN", "60"))
            except ValueError:
                go_min = 60
            scheduler.add_job(
                _go_alerts_job,
                "interval",
                minutes=max(10, go_min),
                args=[application],
                id="go_alerts",
                max_instances=1,
                coalesce=True,
            )
            log.info("R-SIGNAL-DIET go_alerts ENABLED (every %dmin)", max(10, go_min))

        # R18 audit: risk_config_validator proactive scheduler.
        # R-RISK-VALIDATOR-HOTFIX (2026-06-10): the old registration was
        # ``lambda: asyncio.create_task(_risk_validator_job(application))`` —
        # a SYNC callable that AsyncIOScheduler dispatched to a thread-pool
        # executor with no running event loop → RuntimeError every cycle and
        # the job NEVER executed in production. Now registered as a native
        # async job (same mechanism as the healthy _alert_job) and promoted
        # to a 5-minute cadence: risk checks must not wait 2 hours.
        if r18_risk_check_report is not None and r18_risk_check_enabled():
            try:
                rcv_minutes = int(os.getenv("RISK_VALIDATOR_INTERVAL_MIN", "5"))
            except ValueError:
                rcv_minutes = 5
            scheduler.add_job(
                _risk_validator_job,
                "interval",
                minutes=rcv_minutes,
                args=[application],
                id="risk_config_validator",
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2),
            )
            log.info("R18 risk_config_validator ENABLED (every %dmin, native async)", rcv_minutes)

        # R21: morning brief — anchor message at MORNING_BRIEF_HOUR_UTC every day
        # R-ONDEMAND: gated by REPORT_CRON_ENABLED (broadcast surface).
        if os.getenv("MORNING_BRIEF_ENABLED", "true").strip().lower() != "false":
            from modules.cron_state import report_cron_enabled
            mb_hour = _morning_brief_hour()
            scheduler.add_job(
                _gated_broadcast(
                    lambda: send_morning_brief_job(application.bot),
                    report_cron_enabled,
                    "r21_morning_brief",
                ),
                "cron",
                hour=mb_hour,
                minute=0,
                id="morning_brief",
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )
            log.info("R21 morning_brief ENABLED (cron %02d:00 UTC daily)", mb_hour)

        # ─── R-PERFECT Fase 4: stress test cron + hardening ────────────
        # /selftest 4x/day at 00:00, 06:00, 12:00, 18:00 UTC
        if os.getenv("SELFTEST_CRON_ENABLED", "true").strip().lower() != "false":
            scheduler.add_job(
                _selftest_cron_job,
                "cron",
                hour="0,6,12,18",
                minute=0,
                args=[application],
                id="selftest_cron",
                max_instances=1,
                coalesce=True,
            )
            log.info("R-PERFECT: /selftest cron ENABLED (4x/day at 00/06/12/18 UTC)")
        # Daily backup at 04:00 UTC
        if os.getenv("BACKUP_VOLUME_ENABLED", "true").strip().lower() != "false":
            scheduler.add_job(
                _backup_volume_job,
                "cron",
                hour=int(os.getenv("BACKUP_HOUR_UTC", "4")),
                minute=0,
                args=[application],
                id="backup_volume",
                max_instances=1,
                coalesce=True,
            )
            log.info("R-PERFECT: backup_volume cron ENABLED (daily %sh UTC)",
                     os.getenv("BACKUP_HOUR_UTC", "4"))
        # Cost alert check — hourly
        if os.getenv("COST_ALERTS_ENABLED", "true").strip().lower() != "false":
            scheduler.add_job(
                _cost_alert_job,
                "interval",
                hours=1,
                args=[application],
                id="cost_alert",
                max_instances=1,
                coalesce=True,
                next_run_time=datetime.now(timezone.utc) + timedelta(minutes=10),
            )
            log.info("R-PERFECT: cost_alert hourly check ENABLED")

        scheduler.start()
        application.bot_data["scheduler"] = scheduler
        log.info(
            "Scheduler started: alerts %dmin, intel 30min, X REMOVED (R-COST-V2), backup 03:00 UTC, cleanup Sun 04:00 UTC. "
            "R17: macro_cal 1min, reconcile 15min, kill 5min, rates 30min, weekly_summary Sun 18:00 UTC. "
            "R-PERFECT: selftest 4x/day, backup_volume 04:00 UTC, cost_alert hourly.",
            POLL_INTERVAL_MIN,
        )

    # Cleanup old intel memory entries (7+ days old)
    try:
        deleted = intel_cleanup(days=7)
        log.info("Intel memory cleanup: deleted %d old entries", deleted)
    except Exception:
        log.exception("Intel memory cleanup failed")

    # R-BOT-DEFINITIVE-KILLCLEAN (2026-06-15): _apply_hl_runtime_patch ELIMINADO
    # — ya no hay reader de HyperLend que parchear (módulo borrado).

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
    # R-COST-V2: manual incremental refresh + cost dashboard
    "xrefresh": cmd_xrefresh,
    "costs": cmd_costs,
    "alertas": cmd_alertas,
    "intel": cmd_intel,
    "debug_x": cmd_debug_x,
    "x_status": cmd_x_status,
    "costos_x": cmd_costos_x,
    "intel_sources": cmd_intel_sources,
    "providers": cmd_providers,
    # R-BOT-DEFINITIVE-KILLCLEAN (2026-06-15): "flywheel", "debug_flywheel" y
    # "liqcalc" ELIMINADOS (flywheel HyperLend pair-trade muerto → /reporte + /hf
    # cubren el riesgo PM vivo).
    "kill": cmd_kill,
    # R-NOPRELIQ + REMOVE BLOFIN (2026-05-15): "ciclo" / "ciclo_update" ELIMINADOS.
    "dca": cmd_dca,
    "pnl": cmd_pnl,
    "log": cmd_log,
    # Round 16
    "version": cmd_version,
    # R-PAT-RENEW
    "pat_status": cmd_pat_status,
    "errors": cmd_errors,
    "metrics": cmd_metrics,
    # R-SIGNAL-DIET — on-demand alive snapshot (ex-heartbeat push)
    "health": cmd_health,
    "test_alerts": cmd_test_alerts,
    "reload_commands": cmd_reload_commands,
    # Round 17
    "status": cmd_status,
    "reconcile": cmd_reconcile,
    "calendar": cmd_calendar,
    "setcatalyst": cmd_setcatalyst,  # R-BOT-DEFINITIVE WI-1
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
    # R-DASHBOARD-COMMAND
    "dashboard": cmd_dashboard,
    # R-BOT-LMEC-AUTOFEED
    "lmec_status": cmd_lmec_status,
    "setlmec": cmd_setlmec,
    # R-BOT-DEFINITIVE-2 T5 — manual HYPE PPC override
    "setppc": cmd_setppc,
    # R-VARIATIONAL — Farm the DUMP
    "variationalfunding": cmd_variationalfunding,
    "variationalalerts": cmd_variationalalerts,
    # R-FARMDUMP — on-demand 5-check pre-trade filter
    "variationalcheck": cmd_variationalcheck,
    # R-PMCORE — Portfolio Margin state + per-vault breakdown
    "pm": cmd_pm,
    "vaults": cmd_vaults,
    # R-UNLOCK / R-SCREEN — universal SHORT/LONG screener (on-demand state)
    "unlockcheck": cmd_unlockcheck,
    # R-SCREEN — per-token query (same 5-gate engine on one ticker)
    "check": cmd_check,
    # R-TELEMETRY — per-token HL telemetry block (AiPear basket sizing)
    "telemetry": cmd_telemetry,
    # R-SIGNAL — per-name short signals (orthogonal to the >=4 unlock ladder)
    "signals": cmd_signals,
    # R-AUDIT2-P1.3 — INTEGRITY-HALT view + BCD dismissal
    "halts": cmd_halts,
    "haltclear": cmd_haltclear,
    # R-INTEL30 Phase 1 — 11 new free intel sources
    "etfs": cmd_etfs,
    "macro": cmd_macro,
    "argy": cmd_argy,
    "isw": cmd_isw,
    "eia": cmd_eia,
    "asxn": cmd_asxn,
    "hypurr": cmd_hypurr,
    "arkham": cmd_arkham,
    "hl_info": cmd_hl_info,
    "spark": cmd_spark,
    "intel30": cmd_intel30,
    # R-PERFECT Phase 2 — 16 new intel sources
    "hl_rpc": cmd_hl_rpc,
    "hyperevmscan": cmd_hyperevmscan,
    "dune": cmd_dune,
    "hypetrad": cmd_hypetrad,
    "treasury": cmd_treasury,
    "nyfed": cmd_nyfed,
    "cot": cmd_cot,
    "l2beat": cmd_l2beat,
    "artemis": cmd_artemis,
    "visa_oc": cmd_visa_oc,
    "treasuries": cmd_treasuries,
    "openinsider": cmd_openinsider,
    "capitol": cmd_capitol,
    "epoch": cmd_epoch,
    "semianalysis": cmd_semianalysis,
    "finrss": cmd_finrss,
    # R-PERFECT Phase 3 — 3 new sources
    "cryptovol": cmd_cryptovol,
    "kalshi": cmd_kalshi,
    "indec": cmd_indec,
    # R-PERFECT — meta/observability
    "intel30_full": cmd_intel30_full,
    "selftest": cmd_selftest,
    "cost": cmd_cost,
    "sources": cmd_sources,
}


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not configured", file=sys.stderr)
        sys.exit(1)
    if not TELEGRAM_CHAT_ID:
        print("ERROR: TELEGRAM_CHAT_ID not configured", file=sys.stderr)
        sys.exit(1)

    # R-NOIMG (2026-06-30): disable Telegram link-preview cards bot-wide. The
    # reports embed source URLs (DefiLlama, CoinGlass, etc.); Telegram renders
    # an "image" preview card per URL that BCD had to delete by hand before
    # pasting the text. There is NO real image/photo generation in the bot —
    # the cards ARE the link previews — so killing them at the Defaults level
    # makes every outbound message (/reporte, /unlockcheck, /telemetry, etc.)
    # text-only with zero attachments. Report content is byte-identical.
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .defaults(Defaults(link_preview_options=LinkPreviewOptions(is_disabled=True)))
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
