"""Fallback report template used when Anthropic is unavailable.

Purely deterministic formatting of the raw module outputs. Follows the same
section layout as the Claude-generated report so the user gets something
coherent even without the LLM step.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from modules.hyperlend import format_hyperlend
from modules.market import format_market_quick
from modules.portfolio import format_quick_positions
from modules.unlocks import format_unlocks


def build_fallback_report(
    portfolio: dict,
    hyperlend_data: dict,
    market: dict,
    unlocks: dict,
    telegram_intel_text: str | None = None,
) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parts = [
        "═══ REPORTE FONDO BLACK CAT (fallback, sin Claude) ═══",
        f"Fecha: {ts}",
        "",
        "1. PORTFOLIO",
        format_quick_positions(portfolio, hyperlend_data.get("hf")),
        "",
        "2. HYPERLEND",
        format_hyperlend(hyperlend_data),
        "",
        "3. MERCADO",
        format_market_quick(market),
        "",
        "4. UNLOCKS",
        format_unlocks(unlocks),
    ]
    if telegram_intel_text:
        parts.extend(["", "5. TELEGRAM INTEL (raw, sin síntesis)", telegram_intel_text[:3000]])
    parts.append("")
    parts.append("═══ FIN REPORTE ═══")
    return "\n".join(parts)
