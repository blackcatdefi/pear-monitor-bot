"""Hybrid LLM router — picks the right model for each task type.

Architecture:
  Tier 1 (CRITICAL): Claude Sonnet 4.6 — complex analysis, thesis, kill scenarios
  Tier 2 (ROUTINE):  Gemini 2.5 Flash — intel parsing, sentiment, entity extraction
  Tier 3 (FALLBACK): Claude Haiku 4.5 — cheap fallback when primary tier fails

Fallback chains:
  Critical: Sonnet → Haiku → Gemini → LLMError
  Routine:  Gemini → Haiku → LLMError
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

# ─── Env vars ────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

REQUEST_TIMEOUT = 90.0


class TaskType(Enum):
    CRITICAL = "critical"
    ROUTINE = "routine"


# Task → tier mapping
TASK_TIER: dict[str, TaskType] = {
    # Critical — Sonnet 4.6 (quality matters)
    "reporte": TaskType.CRITICAL,
    "tesis": TaskType.CRITICAL,
    "tesis_update": TaskType.CRITICAL,
    "kill": TaskType.CRITICAL,
    "decision_query": TaskType.CRITICAL,
    # Routine — Gemini free (volume matters)
    "intel_parse": TaskType.ROUTINE,
    "telegram_summary": TaskType.ROUTINE,
    "x_sentiment": TaskType.ROUTINE,
    "entity_extraction": TaskType.ROUTINE,
    "deduplication": TaskType.ROUTINE,
}


class LLMError(Exception):
    """Raised when all providers in a tier's fallback chain fail."""
    pass


# ─── Usage / cost tracking (in-memory, resets on redeploy) ───────────────────
_usage: dict[str, dict[str, Any]] = {
    "claude-sonnet-4-6": {
        "calls": 0, "errors": 0,
        "input_tokens": 0, "output_tokens": 0,
        "last_success": None, "last_error": None,
    },
    "claude-haiku-4-5": {
        "calls": 0, "errors": 0,
        "input_tokens": 0, "output_tokens": 0,
        "last_success": None, "last_error": None,
    },
    "gemini-2.5-flash": {
        "calls": 0, "errors": 0,
        "input_tokens": 0, "output_tokens": 0,
        "last_success": None, "last_error": None,
    },
}

# Pricing per 1M tokens (USD)
_PRICING = {
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
    "gemini-2.5-flash": {"input": 0.0, "output": 0.0},
}

_last_provider: str | None = None
_last_provider_ts: str | None = None
_session_start = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _track_success(provider: str, in_tok: int = 0, out_tok: int = 0) -> None:
    global _last_provider, _last_provider_ts
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    _usage[provider]["calls"] += 1
    _usage[provider]["input_tokens"] += in_tok
    _usage[provider]["output_tokens"] += out_tok
    _usage[provider]["last_success"] = now
    _last_provider = provider
    _last_provider_ts = now


def _track_error(provider: str) -> None:
    _usage[provider]["errors"] += 1
    _usage[provider]["last_error"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def get_cost_estimate() -> dict[str, float]:
    """Calculate estimated cost by provider."""
    costs: dict[str, float] = {}
    total = 0.0
    for provider, stats in _usage.items():
        pricing = _PRICING.get(provider, {"input": 0, "output": 0})
        in_cost = (stats["input_tokens"] / 1_000_000) * pricing["input"]
        out_cost = (stats["output_tokens"] / 1_000_000) * pricing["output"]
        costs[provider] = round(in_cost + out_cost, 4)
        total += in_cost + out_cost
    costs["total"] = round(total, 4)
    return costs


# ─── Provider implementations ────────────────────────────────────────────────

async def _call_sonnet(
    system_prompt: str, user_message: str, max_tokens: int,
) -> Optional[str]:
    """Claude Sonnet 4.6 — critical analysis tier."""
    if not ANTHROPIC_API_KEY:
        log.warning("Sonnet: no ANTHROPIC_API_KEY")
        return None
    try:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        resp = await client.messages.create(
            model="claude-sonnet-4-6",
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            max_tokens=max_tokens,
            temperature=0.3,
        )
        text = resp.content[0].text
        _track_success(
            "claude-sonnet-4-6",
            resp.usage.input_tokens,
            resp.usage.output_tokens,
        )
        log.info(
            "Sonnet OK (%d chars, %d+%d tok)",
            len(text), resp.usage.input_tokens, resp.usage.output_tokens,
        )
        return text
    except Exception as e:
        _track_error("claude-sonnet-4-6")
        log.warning("Sonnet failed: %s", e)
        return None


async def _call_haiku(
    system_prompt: str, user_message: str, max_tokens: int,
) -> Optional[str]:
    """Claude Haiku 4.5 — cheap fallback."""
    if not ANTHROPIC_API_KEY:
        log.warning("Haiku: no ANTHROPIC_API_KEY")
        return None
    try:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            max_tokens=max_tokens,
            temperature=0.3,
        )
        text = resp.content[0].text
        _track_success(
            "claude-haiku-4-5",
            resp.usage.input_tokens,
            resp.usage.output_tokens,
        )
        log.info(
            "Haiku OK (%d chars, %d+%d tok)",
            len(text), resp.usage.input_tokens, resp.usage.output_tokens,
        )
        return text
    except Exception as e:
        _track_error("claude-haiku-4-5")
        log.warning("Haiku failed: %s", e)
        return None


async def _call_gemini(
    system_prompt: str, user_message: str, max_tokens: int,
) -> Optional[str]:
    """Gemini 2.5 Flash — free tier for routine tasks."""
    if not GEMINI_API_KEY:
        log.warning("Gemini: no GEMINI_API_KEY")
        return None

    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        "models/gemini-2.5-flash:generateContent"
    )
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_message}]}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": 0.3,
        },
    }
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(url, params={"key": GEMINI_API_KEY}, json=payload)
            if resp.status_code == 429:
                log.warning("Gemini rate limit (429)")
                _track_error("gemini-2.5-flash")
                return None
            if resp.status_code >= 500:
                log.warning("Gemini server error %d", resp.status_code)
                _track_error("gemini-2.5-flash")
                return None
            resp.raise_for_status()
            data = resp.json()
            candidates = data.get("candidates", [])
            if not candidates:
                log.warning("Gemini: no candidates")
                _track_error("gemini-2.5-flash")
                return None
            parts = candidates[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts)
            if not text:
                _track_error("gemini-2.5-flash")
                return None
            usage_meta = data.get("usageMetadata", {})
            in_tok = usage_meta.get("promptTokenCount", 0)
            out_tok = usage_meta.get("candidatesTokenCount", 0)
            _track_success("gemini-2.5-flash", in_tok, out_tok)
            log.info("Gemini OK (%d chars, %d+%d tok)", len(text), in_tok, out_tok)
            return text
    except Exception as e:
        _track_error("gemini-2.5-flash")
        log.warning("Gemini failed: %s", e)
        return None


# ─── Main routing function ───────────────────────────────────────────────────

async def route_request(
    task_name: str,
    system_prompt: str,
    user_message: str,
    max_tokens: int = 4000,
) -> tuple[str, str]:
    """Route request to the right model tier.

    Returns (response_text, model_used).
    Raises LLMError if all providers in the chain fail.
    """
    task_type = TASK_TIER.get(task_name, TaskType.CRITICAL)

    if task_type == TaskType.CRITICAL:
        chain = [
            ("claude-sonnet-4-6", _call_sonnet),
            ("claude-haiku-4-5", _call_haiku),
            ("gemini-2.5-flash", _call_gemini),
        ]
    else:
        chain = [
            ("gemini-2.5-flash", _call_gemini),
            ("claude-haiku-4-5", _call_haiku),
        ]

    errors = []
    for name, fn in chain:
        log.info("Trying %s for task '%s'", name, task_name)
        result = await fn(system_prompt, user_message, max_tokens)
        if result and result.strip():
            return result, name
        errors.append(name)

    raise LLMError(f"All providers failed for '{task_name}': {errors}")


# ─── Backward compat ─────────────────────────────────────────────────────────

async def complete(
    system_prompt: str,
    user_message: str,
    max_tokens: int = 4000,
) -> tuple[str, str]:
    """Backward-compatible — routes as CRITICAL."""
    return await route_request("reporte", system_prompt, user_message, max_tokens)


# ─── Provider status display ─────────────────────────────────────────────────

def format_provider_status() -> str:
    """Format provider status for /providers command with cost tracking."""
    costs = get_cost_estimate()
    lines = [
        "\U0001f916 LLM HYBRID ROUTING STATUS",
        "",
        "CRITICAL ANALYSIS (Sonnet 4.6):",
    ]

    s = _usage["claude-sonnet-4-6"]
    s_ok = "\U0001f7e2" if ANTHROPIC_API_KEY else "\u26aa"
    lines.append(f"  {s_ok} /reporte, /tesis, /kill")
    lines.append(f"  Requests: {s['calls']} | Errors: {s['errors']}")
    lines.append(f"  Tokens: {s['input_tokens']:,} in + {s['output_tokens']:,} out")
    lines.append(f"  Costo: ${costs.get('claude-sonnet-4-6', 0):.4f}")
    lines.append("")

    lines.append("ROUTINE PROCESSING (Gemini 2.5 Flash FREE):")
    g = _usage["gemini-2.5-flash"]
    g_ok = "\U0001f7e2" if GEMINI_API_KEY else "\u26aa"
    lines.append(f"  {g_ok} intel_processor, summaries, sentiment")
    lines.append(f"  Requests: {g['calls']} | Errors: {g['errors']}")
    lines.append(f"  Costo: $0.00 (free tier)")
    lines.append("")

    lines.append("FALLBACK (Haiku 4.5):")
    h = _usage["claude-haiku-4-5"]
    h_ok = "\U0001f7e2" if ANTHROPIC_API_KEY else "\u26aa"
    lines.append(f"  {h_ok} Activo si Sonnet/Gemini fallan")
    lines.append(f"  Requests: {h['calls']} | Errors: {h['errors']}")
    lines.append(f"  Costo: ${costs.get('claude-haiku-4-5', 0):.4f}")
    lines.append("")

    total_cost = costs.get("total", 0)
    lines.append(f"COSTO SESI\u00d3N: ${total_cost:.4f}")

    try:
        start_dt = datetime.strptime(_session_start, "%Y-%m-%d %H:%M UTC")
        start_dt = start_dt.replace(tzinfo=timezone.utc)
        now_dt = datetime.now(timezone.utc)
        hours_elapsed = max((now_dt - start_dt).total_seconds() / 3600, 0.1)
        daily_est = (total_cost / hours_elapsed) * 24
        monthly_est = daily_est * 30
        yearly_est = daily_est * 365
        lines.append(f"COSTO D\u00cdA ESTIMADO: ~${daily_est:.2f}")
        lines.append(f"COSTO MES ESTIMADO: ~${monthly_est:.2f}")
        lines.append(f"COSTO A\u00d1O ESTIMADO: ~${yearly_est:.0f}")
    except Exception:
        pass

    if _last_provider:
        lines.append("")
        lines.append(
            f"\u00daltimo an\u00e1lisis: {_last_provider} ({_last_provider_ts})"
        )

    lines.append(f"\nSesi\u00f3n desde: {_session_start}")

    return "\n".join(lines)
