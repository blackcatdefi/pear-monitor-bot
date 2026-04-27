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
    BotCommand("reporte", "TODO-EN-UNO: timeline + posiciones + análisis", "core", "cmd_reporte"),
    BotCommand("posiciones", "Snapshot rápido (wallets + HF + Bounce Tech)", "core", "cmd_posiciones"),
    BotCommand("flywheel", "Pair trade HL (LONG HYPE / SHORT UETH)", "core", "cmd_flywheel"),
    BotCommand("tesis", "Estado de la tesis macro", "core", "cmd_tesis"),
    BotCommand("start", "Mostrar este menú completo", "core", "cmd_start"),
    BotCommand("help", "Alias de /start", "core", "cmd_start"),

    # ─── TRADING ───
    BotCommand("liqcalc", "Matriz liq HYPE × deuda", "trading", "cmd_liqcalc"),
    BotCommand("hf", "Health Factor de HyperLend", "trading", "cmd_hf"),
    BotCommand("kill", "Kill scenarios de cada posición", "trading", "cmd_kill"),
    BotCommand("ciclo", "Estado del Trade del Ciclo (Blofin manual)", "trading", "cmd_ciclo"),
    BotCommand("ciclo_update", "Abrir/cerrar Trade del Ciclo", "trading", "cmd_ciclo_update"),
    BotCommand("dca", "Plan DCA tramificado BTC/ETH/HYPE + zona actual", "trading", "cmd_dca"),
    BotCommand("pnl", "Realized PnL 7D / 30D / YTD", "trading", "cmd_pnl"),

    # ─── INTEL ───
    BotCommand("timeline", "Timeline X 48h (tu X list)", "intel", "cmd_timeline"),
    BotCommand("intel", "Resumen de intel memory (últimas 24h)", "intel", "cmd_intel"),
    BotCommand("intel_sources", "Top 20 cuentas activas en X (24h)", "intel", "cmd_intel_sources"),

    # ─── ADMIN ───
    BotCommand("log", "Últimas 20 entradas del position log", "admin", "cmd_log"),
    BotCommand("alertas", "Toggle alertas automáticas (on/off)", "admin", "cmd_alertas"),
    BotCommand("providers", "Status de los LLM providers", "admin", "cmd_providers"),
    BotCommand("reload_commands", "Re-sincronizar lista de comandos con Telegram", "admin", "cmd_reload_commands"),
    BotCommand("test_alerts", "Disparar alerta de test al chat", "admin", "cmd_test_alerts"),

    # ─── DEBUG / OBSERVABILIDAD ───
    BotCommand("debug_x", "Diagnóstico de conectividad X/Twitter", "debug", "cmd_debug_x"),
    BotCommand("x_status", "Estado X API (R15+ live + counters + cache)", "debug", "cmd_x_status"),
    BotCommand("costos_x", "Auditoría de costos X API", "debug", "cmd_costos_x"),
    BotCommand("debug_flywheel", "Dump raw HyperLend reserves (DEBUG_MODE=true)", "debug", "cmd_debug_flywheel"),
    BotCommand("version", "Commit SHA + uptime + provider status", "debug", "cmd_version"),
    BotCommand("errors", "Últimos 20 errores capturados", "debug", "cmd_errors"),
    BotCommand("metrics", "Dashboard de salud del bot (24h)", "debug", "cmd_metrics"),
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
        "🐱‍⬛ Fondo Black Cat — analista personal",
        "",
        "Keyboard — todos los comandos:",
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
