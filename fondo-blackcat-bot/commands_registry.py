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
    BotCommand("posiciones", "Quick snapshot (wallets + HF + Bounce Tech)", "core", "cmd_posiciones"),
    BotCommand("dashboard", "Live dashboard: capital + flywheels + basket + market (R-DASHBOARD-COMMAND)", "core", "cmd_dashboard"),
    BotCommand("status", "R17 quick status (no LLM, <3s)", "core", "cmd_status"),
    BotCommand("flywheel", "HL pair trade (LONG HYPE / SHORT UETH)", "core", "cmd_flywheel"),
    BotCommand("tesis", "Current macro thesis state", "core", "cmd_tesis"),
    BotCommand("calendar", "Upcoming catalysts (macro + unlocks + TGEs)", "core", "cmd_calendar"),
    BotCommand("brief", "Full morning brief (R18)", "core", "cmd_brief"),
    BotCommand("convergence", "Macro convergence triggers status (R18)", "core", "cmd_convergence"),
    BotCommand("start", "Show this full menu", "core", "cmd_start"),
    BotCommand("help", "Alias for /start", "core", "cmd_start"),

    # ─── TRADING ───
    BotCommand("liqcalc", "Liq matrix HYPE × debt", "trading", "cmd_liqcalc"),
    BotCommand("hf", "HyperLend Health Factor", "trading", "cmd_hf"),
    BotCommand("kill", "Kill scenarios per position", "trading", "cmd_kill"),
    BotCommand("kill_status", "Status of the 5 kill triggers (R17)", "trading", "cmd_kill_status"),
    BotCommand("ciclo", "Cycle Trade status (Blofin manual)", "trading", "cmd_ciclo"),
    BotCommand("ciclo_update", "Open/close Cycle Trade", "trading", "cmd_ciclo_update"),
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

    # ─── DEBUG / OBSERVABILITY ───
    BotCommand("debug_x", "X/Twitter connectivity diagnostic", "debug", "cmd_debug_x"),
    BotCommand("x_status", "X API status (R15+ live + counters + cache)", "debug", "cmd_x_status"),
    BotCommand("costos_x", "X API cost audit", "debug", "cmd_costos_x"),
    BotCommand("debug_flywheel", "Dump raw HyperLend reserves (DEBUG_MODE=true)", "debug", "cmd_debug_flywheel"),
    BotCommand("version", "Commit SHA + uptime + provider status", "debug", "cmd_version"),
    BotCommand("errors", "Last 20 captured errors", "debug", "cmd_errors"),
    BotCommand("metrics", "Bot health dashboard (24h)", "debug", "cmd_metrics"),
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
