"""Anthropic Claude integration for report generation."""
from __future__ import annotations

import logging
from typing import Any

from anthropic import AsyncAnthropic

from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL
from templates.formatters import compile_raw_data
from templates.system_prompt import SYSTEM_PROMPT, THESIS_PROMPT

log = logging.getLogger(__name__)

_client: AsyncAnthropic | None = None


def get_client() -> AsyncAnthropic | None:
    global _client
    if not ANTHROPIC_API_KEY:
        return None
    if _client is None:
        _client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    return _client


async def generate_report(
    portfolio: list[dict[str, Any]] | None,
    hyperlend: dict[str, Any] | None,
    market: dict[str, Any] | None,
    unlocks: dict[str, Any] | None,
    telegram_intel: dict[str, Any] | None,
) -> str:
    client = get_client()
    if client is None:
        return "❌ ANTHROPIC_API_KEY no configurada — no se puede generar el reporte."

    user_content = compile_raw_data(portfolio, hyperlend, market, unlocks, telegram_intel)
    try:
        # Prompt caching on the system prompt — it's stable across requests
        resp = await client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=8000,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_content}],
        )
        parts = []
        for block in resp.content:
            if hasattr(block, "text"):
                parts.append(block.text)
        return "\n".join(parts).strip() or "(reporte vacío)"
    except Exception as exc:  # noqa: BLE001
        log.exception("Anthropic call failed")
        return f"❌ Error generando reporte con Claude: {exc}"


async def generate_thesis_check(
    portfolio: list[dict[str, Any]] | None,
    hyperlend: dict[str, Any] | None,
    market: dict[str, Any] | None,
) -> str:
    client = get_client()
    if client is None:
        return "❌ ANTHROPIC_API_KEY no configurada."

    user_content = compile_raw_data(portfolio, hyperlend, market, None, None)
    try:
        resp = await client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=2000,
            system=[
                {
                    "type": "text",
                    "text": THESIS_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_content}],
        )
        parts = []
        for block in resp.content:
            if hasattr(block, "text"):
                parts.append(block.text)
        return "\n".join(parts).strip() or "(análisis vacío)"
    except Exception as exc:  # noqa: BLE001
        log.exception("Thesis check failed")
        return f"❌ Error: {exc}"
