"""
Anthropic Claude API — generador del reporte final.

Toma la data cruda de los módulos 1-5 y la pasa al modelo con el system prompt
del Co-Gestor. Retorna el reporte final en texto plano (formato definido en
templates/daily_report.py).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import anthropic

from config import ANTHROPIC_API_KEY
from templates.daily_report import SYSTEM_PROMPT

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-5"
MAX_TOKENS = 8000


def _compile_raw_data(portfolio: list[dict], hyperlend: dict | None,
                       market: dict, unlocks: list[dict],
                       telegram_intel_text: str) -> str:
    """Concatena la data cruda en un bloque estructurado para el LLM."""
    now = datetime.now(timezone.utc).isoformat()
    parts = [
        f"Fecha del reporte: {now}",
        "",
        "=== PORTFOLIO (HyperLiquid perps por wallet) ===",
        json.dumps(portfolio, indent=2, default=str, ensure_ascii=False),
        "",
        "=== HYPERLEND (on-chain HyperEVM) ===",
        json.dumps(hyperlend, indent=2, default=str, ensure_ascii=False) if hyperlend else "(no disponible)",
        "",
        "=== MARKET DATA ===",
        json.dumps(market, indent=2, default=str, ensure_ascii=False),
        "",
        "=== UNLOCKS 7d (>$2M) ===",
        json.dumps(unlocks, indent=2, default=str, ensure_ascii=False),
        "",
        "=== TELEGRAM INTEL (últimas 24h) ===",
        telegram_intel_text,
        "",
        "Generá el reporte siguiendo EXACTAMENTE el formato definido en tu system prompt.",
    ]
    return "\n".join(parts)


async def generate_report(portfolio: list[dict], hyperlend: dict | None,
                          market: dict, unlocks: list[dict],
                          telegram_intel_text: str) -> str:
    if not ANTHROPIC_API_KEY:
        return "❌ ANTHROPIC_API_KEY no configurada — reporte no generado."

    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    user_content = _compile_raw_data(
        portfolio, hyperlend, market, unlocks, telegram_intel_text
    )

    try:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        return response.content[0].text
    except Exception as e:  # noqa: BLE001
        log.exception("Claude API failed")
        return f"❌ Error generando reporte: {e}"


async def generate_thesis_check(portfolio: list[dict], hyperlend: dict | None,
                                market: dict) -> str:
    """Variante corta: solo status de la tesis (para /tesis)."""
    if not ANTHROPIC_API_KEY:
        return "❌ ANTHROPIC_API_KEY no configurada."

    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    prompt = (
        "Con esta data, dame SOLO el status de la tesis del fondo (no reporte completo):\n\n"
        "1. Qué VALIDA la tesis ahora (3 bullets con data específica)\n"
        "2. Qué podría INVALIDARLA (3 bullets con triggers concretos)\n"
        "3. Acción sugerida (MANTENER/AGREGAR/REDUCIR/SALIR) + razón en 1 línea.\n\n"
        "DATA:\n"
        + json.dumps({
            "portfolio": portfolio,
            "hyperlend": hyperlend,
            "market": market,
        }, indent=2, default=str, ensure_ascii=False)
    )
    try:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
    except Exception as e:  # noqa: BLE001
        log.exception("Thesis check failed")
        return f"❌ Error: {e}"
