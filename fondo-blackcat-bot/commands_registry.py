"""Round 16: Single source of truth for bot commands.

Every command the bot exposes lives here. On startup, bot.py syncs this list
to BotFather via setMyCommands so they appear in Telegram's autocomplete bar.
The /start handler renders them grouped by category from this same list.

Adding a new command = add an entry here + register the handler in bot.py.
The validate_commands_match_handlers() check enforces no drift.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class BotCommand:
    command: str          # without leading /
    description: str      # short description (max 256 chars Telegram limit)
    category: str         # "core" | "intel" | "trading" | "admin" | "debug"
    handler_name: str     # name of the handler function in bot.py (validation)


# Telegram limits descriptions to 256 chars; we keep them <100.
COMMANDS: List[BotCommand] = [
    # ─── CORE ───
    BotCommand("reporte", "All-in-one: timeline + positions + analysis", "core", "cmd_reporte"),
    BotCommand("posiciones", "Quick snapshot (wallets + PM + Bounce Tech)", "core", "cmd_posiciones"),
    BotCommand("dashboard", "Live dashboard: capital + basket + market (R-DASHBOARD-COMMAND)", "core", "cmd_dashboard"),
    BotCommand("status", "R17 quick status (no LLM, <3s)", "core", "cmd_status"),
    BotCommand("tesis", "Current macro thesis state", "core", "cmd_tesis"),
    BotCommand("calendar", "Upcoming catalysts (macro + unlocks + TGEs)", "core", "cmd_calendar"),
    BotCommand("brief", "Full morning brief (R18)", "core", "cmd_brief"),
    BotCommand("convergence", "Macro convergence triggers status (R18)", "core", "cmd_convergence"),
    BotCommand("start", "Show this full menu", "core", "cmd_start"),
    BotCommand("help", "Alias for /start", "core", "cmd_start"),

    # ─── TRADING ───
    BotCommand("hf", "Portfolio Margin aave-HF + liq price (colateral HYPE)", "trading", "cmd_hf"),
    BotCommand("kill", "Kill scenarios per position", "trading", "cmd_kill"),
    BotCommand("kill_status", "Status of the 3 kill triggers (BTC >82k / PM aave-HF / basket DD)", "trading", "cmd_kill_status"),
    # R-NOPRELIQ + REMOVE BLOFIN (2026-05-15): /ciclo y /ciclo_update ELIMINADOS.
    BotCommand("dca", "Tiered DCA plan BTC/ETH/HYPE + current zone", "trading", "cmd_dca"),
    BotCommand("pnl", "Realized PnL 7D / 30D / YTD", "trading", "cmd_pnl"),
    BotCommand("pnlx", "Extended PnL by period + best/worst (R18)", "trading", "cmd_pnlx"),
    BotCommand("pretrade", "5-point pre-trade checklist <SYMBOL>", "trading", "cmd_pretrade"),
    BotCommand("compounding_history", "Compounding events last 30d (R18)", "trading", "cmd_compounding_history"),

    # ─── INTEL ───
    BotCommand("timeline", "X Timeline 48h (your X list)", "intel", "cmd_timeline"),
    BotCommand("intel", "Intel memory summary (last 24h)", "intel", "cmd_intel"),
    BotCommand("intel_sources", "Top 20 active accounts on X (24h)", "intel", "cmd_intel_sources"),
    BotCommand("intel_search", "Search keyword in intel_memory <kw>", "intel", "cmd_intel_search"),
    BotCommand("cryexc", "Snapshot cryexc.josedonato.com (funding+movers+HL OI)", "intel", "cmd_cryexc"),

    # ── R-INTEL30 Phase 1 — 11 free intel sources (2026-05-08) ──────────────
    BotCommand("etfs", "Farside daily BTC/ETH/SOL spot ETF flows", "intel", "cmd_etfs"),
    BotCommand("macro", "FRED US macro + Apollo Daily Spark", "intel", "cmd_macro"),
    BotCommand("argy", "AR macro: CriptoYa FX brecha + BCRA reservas/BM", "intel", "cmd_argy"),
    BotCommand("isw", "ISW + Critical Threats Project geopol RSS", "intel", "cmd_isw"),
    BotCommand("eia", "EIA WPSR oil/gas weekly stocks", "intel", "cmd_eia"),
    BotCommand("asxn", "ASXN HYPE buyback/burn/staking/genesis", "intel", "cmd_asxn"),
    BotCommand("hypurr", "HypurrScan HIP-1 Dutch auctions", "intel", "cmd_hypurr"),
    BotCommand("arkham", "Arkham whale/entity transfers", "intel", "cmd_arkham"),
    BotCommand("hl_info", "HL Info API: HIP-3 + predicted fundings", "intel", "cmd_hl_info"),
    BotCommand("spark", "Apollo Daily Spark (Torsten Slok)", "intel", "cmd_spark"),
    BotCommand("intel30", "Run all 11 R-INTEL30 Phase 1 sources at once", "intel", "cmd_intel30"),

    # ── R-PERFECT Phase 2 — 16 new free intel sources (2026-05-08) ───────────
    BotCommand("hl_rpc", "HyperEVM RPC edge probe (block/gas/chain_id)", "intel", "cmd_hl_rpc"),
    BotCommand("hyperevmscan", "HyperEVMScan via Etherscan v2 (block/gas)", "intel", "cmd_hyperevmscan"),
    BotCommand("dune", "Dune top-5 HL dashboards", "intel", "cmd_dune"),
    BotCommand("hypetrad", "HypeTrad pro-trader leaderboard", "intel", "cmd_hypetrad"),
    BotCommand("treasury", "US Treasury Fiscal Data — public debt", "intel", "cmd_treasury"),
    BotCommand("nyfed", "NY Fed Markets — SOFR/EFFR/OBFR rates", "intel", "cmd_nyfed"),
    BotCommand("cot", "CFTC COT — TFF positioning weekly", "intel", "cmd_cot"),
    BotCommand("l2beat", "L2Beat — top 10 L2 by TVS", "intel", "cmd_l2beat"),
    BotCommand("artemis", "Artemis — chain metrics (fees+DAU+rev)", "intel", "cmd_artemis"),
    BotCommand("visa_oc", "Visa Onchain Analytics — stablecoin volume", "intel", "cmd_visa_oc"),
    BotCommand("treasuries", "BTC + ETH treasuries bundle", "intel", "cmd_treasuries"),
    BotCommand("openinsider", "OpenInsider Form 4 latest", "intel", "cmd_openinsider"),
    BotCommand("capitol", "CapitolTrades — Congress disclosures", "intel", "cmd_capitol"),
    BotCommand("epoch", "Epoch AI — notable models recent", "intel", "cmd_epoch"),
    BotCommand("semianalysis", "SemiAnalysis Substack RSS", "intel", "cmd_semianalysis"),
    BotCommand("finrss", "Finance newsletter bundle (Money Stuff/NetInt/Diff)", "intel", "cmd_finrss"),

    # ── R-PERFECT Phase 3 — 3 new sources ────────────────────────────────────
    BotCommand("cryptovol", "Crypto vol — Deribit DVOL + Coinalyze + Velo", "intel", "cmd_cryptovol"),
    BotCommand("kalshi", "Kalshi public markets + RSA-PSS auth probe", "intel", "cmd_kalshi"),
    BotCommand("indec", "INDEC + LATAM macro extras", "intel", "cmd_indec"),

    # ── R-PERFECT — meta/observability ───────────────────────────────────────
    BotCommand("intel30_full", "Run ALL 30 intel sources in parallel", "intel", "cmd_intel30_full"),
    BotCommand("selftest", "Selftest 30 sources — LIVE/PARTIAL/UNAVAILABLE matrix", "debug", "cmd_selftest"),
    BotCommand("cost", "LLM cost breakdown last 7d", "debug", "cmd_cost"),
    BotCommand("sources", "Last source-status snapshot from intel.log", "debug", "cmd_sources"),

    # ─── ADMIN ───
    BotCommand("log", "Last 20 position log entries", "admin", "cmd_log"),
    BotCommand("alertas", "Toggle automatic alerts (on/off)", "admin", "cmd_alertas"),
    BotCommand("providers", "LLM providers status", "admin", "cmd_providers"),
    BotCommand("reload_commands", "Re-sync command list with Telegram", "admin", "cmd_reload_commands"),
    BotCommand("test_alerts", "Fire a test alert to chat", "admin", "cmd_test_alerts"),
    BotCommand("reconcile", "Reconcile fund_state vs on-chain (R17)", "admin", "cmd_reconcile"),
    BotCommand("risk_check", "Risk config invariant validator (R18)", "admin", "cmd_risk_check"),
    BotCommand("add_event", "Add event to calendar <id> <ISO> <cat> <imp> | <name>", "admin", "cmd_add_event"),
    BotCommand("remove_event", "Remove event from calendar <event_id>", "admin", "cmd_remove_event"),
    BotCommand("export", "Export CSV <type> <period> (fills|pnl|positions|intel|errors × 7d|30d|90d|ytd|all)", "admin", "cmd_export"),
    BotCommand("scheduler_health", "Scheduler health table (R18 add-on)", "admin", "cmd_scheduler_health"),
    BotCommand("silent", "Toggle silent mode (on/off/status) — R-SILENT denoise", "admin", "cmd_silent"),
    BotCommand("pat_status", "GitHub PAT expiry — días restantes + verdict (R-PAT-RENEW)", "admin", "cmd_pat_status"),

    BotCommand("setcatalyst", "Catalysts engine: add/del/list (FRED+FOMC+manual) — WI-1", "trading", "cmd_setcatalyst"),
    BotCommand("lmec_status", "LMEC bear-invalidation telemetry (R-BOT-LMEC-AUTOFEED)", "trading", "cmd_lmec_status"),
    BotCommand("setlmec", "Set manual LMEC inputs: MACD/RSI/MA50w semanal (P1.9)", "trading", "cmd_setlmec"),
    BotCommand("setppc", "Override manual PPC HYPE + adq. neta (/setppc 53.5 41.5 | clear) — T5", "trading", "cmd_setppc"),

    # ─── R-VARIATIONAL — Farm the DUMP ───
    BotCommand("variationalfunding", "Scan Variational perps: funding anualizado ≤ umbral (-500% def)", "trading", "cmd_variationalfunding"),
    BotCommand("variationalalerts", "Watch ticker → alerta cuando funding revierte a mitad del baseline", "trading", "cmd_variationalalerts"),
    BotCommand("variationalcheck", "Corre los 5 checks Farm the DUMP on-demand → veredicto GO/CAUTION/NO-GO", "trading", "cmd_variationalcheck"),

    # ─── R-PMCORE — Portfolio Margin (post-migración HyperLend→PM) ───
    BotCommand("pm", "Portfolio Margin: colateral HYPE / deuda / capacidad / margin ratio + naked-long guard", "trading", "cmd_pm"),
    BotCommand("vaults", "Breakdown por vault (cada uno separado): equity, PnL, all-time, MDD, evolución", "trading", "cmd_vaults"),

    # ─── R-UNLOCK / R-SCREEN — universal SHORT/LONG screener ───
    BotCommand("unlockcheck", "R-SCREEN: screener universal SHORT/LONG sobre TODO el universo (HL+Variational), rankeado más→menos shorteable + flag long; 5/5+AiPear es tu decisión", "trading", "cmd_unlockcheck"),
    BotCommand("check", "R-SCREEN por token: corre los 5 gates en 1 ticker <TICKER> (ej. /check WLD) → veredicto short + lectura long", "trading", "cmd_check"),
    BotCommand("telemetry", "R-TELEMETRY: bloque por token <T1 T2…> (1-8) — funding live+7d, OI/vol, dist 7d-low, depth ±0.5/1%, squeeze+fails-first+z/Hurst (HL info API; n/d si falla un feed)", "trading", "cmd_telemetry"),

    # ─── R-SIGNAL — per-name short signals (orthogonal to the >=4 ladder) ───
    BotCommand("signals", "R-SIGNAL: nombres que pasan el filtro short de 5 gates AHORA (confirmá c/u 5/5 AiPear) — ortogonal al ladder >=4", "trading", "cmd_signals"),

    # ─── R-AUDIT2-P1.3 — INTEGRITY-HALT (rumor de integridad + PnL adverso) ───
    BotCommand("halts", "INTEGRITY-HALT activos: rumor de integridad sobre un activo en cartera con PnL adverso → STOP acumular (MANUAL REVIEW, nunca auto-acción)", "trading", "cmd_halts"),
    BotCommand("haltclear", "Cierre manual de BCD de un INTEGRITY-HALT: /haltclear <ASSET>. Único modo de limpiarlo (shielded NUNCA auto-limpia)", "trading", "cmd_haltclear"),

    # ─── DEBUG / OBSERVABILITY ───
    BotCommand("debug_x", "X/Twitter connectivity diagnostic", "debug", "cmd_debug_x"),
    BotCommand("x_status", "X API status (R15+ live + counters + cache)", "debug", "cmd_x_status"),
    BotCommand("costos_x", "X API cost audit", "debug", "cmd_costos_x"),
    BotCommand("version", "Commit SHA + uptime + provider status", "debug", "cmd_version"),
    BotCommand("errors", "Last 20 captured errors", "debug", "cmd_errors"),
    BotCommand("metrics", "Bot health dashboard (24h)", "debug", "cmd_metrics"),
    # R-SIGNAL-DIET — reemplaza el heartbeat push 6h (mismo snapshot, on-demand)
    BotCommand("health", "Bot alive on-demand: uptime + capital + BTC (ex-heartbeat)", "debug", "cmd_health"),
]


CATEGORY_LABELS = {
    "core": "🐱‍⬛ Core",
    "trading": "📊 Trading",
    "intel": "📡 Intel",
    "admin": "⚙️ Admin",
    "debug": "🛠 Debug",
}


def render_start_menu() -> str:
    """Render the /start text dynamically from COMMANDS, grouped by category."""
    lines = [
        "🐱‍⬛ Fondo Black Cat — personal analyst",
        "",
        "Keyboard — all commands:",
    ]
    for cat_key, cat_label in CATEGORY_LABELS.items():
        cmds_in_cat = [c for c in COMMANDS if c.category == cat_key and c.command != "help"]
        if not cmds_in_cat:
            continue
        lines.append("")
        lines.append(cat_label)
        for cmd in cmds_in_cat:
            lines.append(f"/{cmd.command} — {cmd.description}")
    return "\n".join(lines)


def telegram_command_payload() -> list[tuple[str, str]]:
    """Return (command, description) tuples for setMyCommands.

    Excludes /start (Telegram clients show it automatically) and /help (alias).
    Description is truncated to 256 chars per Telegram API limit.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for cmd in COMMANDS:
        if cmd.command in ("start", "help"):
            continue
        if cmd.command in seen:
            continue
        seen.add(cmd.command)
        out.append((cmd.command, cmd.description[:256]))
    return out


def validate_commands_match_handlers(registered_handler_names: set[str]) -> list[str]:
    """Return list of error strings (empty if all good).

    `registered_handler_names` should be the set of handler function names that
    were actually wired into the Application in bot.py.
    """
    errors: list[str] = []
    for cmd in COMMANDS:
        if cmd.handler_name not in registered_handler_names:
            errors.append(f"/{cmd.command} → handler '{cmd.handler_name}' NOT registered")
    return errors
