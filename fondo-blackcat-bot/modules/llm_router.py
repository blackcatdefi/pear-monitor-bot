"""Hybrid LLM router — picks the right model for each task type.

Architecture:
  Tier 1 (CRITICAL): Claude Sonnet 4.6 — complex analysis, thesis, kill scenarios
  Tier 2 (ROUTINE):  Gemini 2.5 Flash — intel parsing, sentiment, entity extraction
  Tier 3 (FALLBACK): Claude Haiku 4.5 — cheap fallback when primary tier fails

Fallback chains:
  Critical: Sonnet → Haiku → Gemini → LLMError
  Routine:  Gemini → Haiku → LLMError

Round 7: usage/cost counters persisted to SQLite (intel_memory.llm_usage) so
/providers survives redeploys and reports real session/day/month totals.
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


# Pricing per 1M tokens (USD) — Anthropic public pricing Apr 2026.
_PRICING = {
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5-20251001": {"input": 1.0, "output": 5.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
    "gemini-2.5-flash": {"input": 0.0, "output": 0.0},  # free tier
}


def _cost_for(model: str, in_tok: int, out_tok: int) -> float:
    p = _PRICING.get(model, {"input": 0.0, "output": 0.0})
    return (in_tok / 1_000_000.0) * p["input"] + (out_tok / 1_000_000.0) * p["output"]


# Last-provider pointer (kept in memory — just for the status line)
_last_provider: str | None = None
_last_provider_ts: str | None = None
_session_start = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _persist_usage(
    task_name: str,
    model: str,
    in_tok: int,
    out_tok: int,
    success: bool,
) -> None:
    """Persist a single call to intel_memory.llm_usage — survives redeploys."""
    try:
        from modules import intel_memory
        cost = _cost_for(model, in_tok or 0, out_tok or 0)
        intel_memory.track_llm_usage(
            task_name=task_name,
            model_used=model,
            tokens_in=in_tok or 0,
            tokens_out=out_tok or 0,
            cost_usd=cost,
            success=success,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("persist_usage failed (%s/%s): %s", task_name, model, exc)


def _track_success(provider: str, in_tok: int, out_tok: int, task_name: str) -> None:
    global _last_provider, _last_provider_ts
    _last_provider = provider
    _last_provider_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    _persist_usage(task_name, provider, in_tok, out_tok, True)


def _track_error(provider: str, task_name: str) -> None:
    _persist_usage(task_name, provider, 0, 0, False)


# ─── Provider implementations ────────────────────────────────────────────────

async def _call_sonnet(
    system_prompt: str, user_message: str, max_tokens: int, task_name: str,
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
        in_tok = getattr(resp.usage, "input_tokens", 0) if hasattr(resp, "usage") else 0
        out_tok = getattr(resp.usage, "output_tokens", 0) if hasattr(resp, "usage") else 0
        _track_success("claude-sonnet-4-6", in_tok, out_tok, task_name)
        log.info("Sonnet OK (%d chars, %d+%d tok)", len(text), in_tok, out_tok)
        return text
    except Exception as e:
        _track_error("claude-sonnet-4-6", task_name)
        log.warning("Sonnet failed: %s", e)
        return None


async def _call_haiku(
    system_prompt: str, user_message: str, max_tokens: int, task_name: str,
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
        in_tok = getattr(resp.usage, "input_tokens", 0) if hasattr(resp, "usage") else 0
        out_tok = getattr(resp.usage, "output_tokens", 0) if hasattr(resp, "usage") else 0
        _track_success("claude-haiku-4-5-20251001", in_tok, out_tok, task_name)
        log.info("Haiku OK (%d chars, %d+%d tok)", len(text), in_tok, out_tok)
        return text
    except Exception as e:
        _track_error("claude-haiku-4-5-20251001", task_name)
        log.warning("Haiku failed: %s", e)
        return None


async def _call_gemini(
    system_prompt: str, user_message: str, max_tokens: int, task_name: str,
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
                _track_error("gemini-2.5-flash", task_name)
                return None
            if resp.status_code >= 500:
                log.warning("Gemini server error %d", resp.status_code)
                _track_error("gemini-2.5-flash", task_name)
                return None
            resp.raise_for_status()
            data = resp.json()
            candidates = data.get("candidates", [])
            if not candidates:
                log.warning("Gemini: no candidates")
                _track_error("gemini-2.5-flash", task_name)
                return None
            parts = candidates[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts)
            if not text:
                _track_error("gemini-2.5-flash", task_name)
                return None
            usage_meta = data.get("usageMetadata", {})
            in_tok = usage_meta.get("promptTokenCount", 0)
            out_tok = usage_meta.get("candidatesTokenCount", 0)
            _track_success("gemini-2.5-flash", in_tok, out_tok, task_name)
            log.info("Gemini OK (%d chars, %d+%d tok)", len(text), in_tok, out_tok)
            return text
    except Exception as e:
        _track_error("gemini-2.5-flash", task_name)
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
            ("claude-haiku-4-5-20251001", _call_haiku),
            ("gemini-2.5-flash", _call_gemini),
        ]
    else:
        chain = [
            ("gemini-2.5-flash", _call_gemini),
            ("claude-haiku-4-5-20251001", _call_haiku),
        ]

    errors = []
    for name, fn in chain:
        log.info("Trying %s for task '%s'", name, task_name)
        result = await fn(system_prompt, user_message, max_tokens, task_name)
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


# ─── Cost helper (SQLite-backed, stays for legacy callers) ───────────────────

def get_cost_estimate() -> dict[str, float]:
    """Total cost-by-provider for the current UTC day (SQLite aggregation)."""
    try:
        from modules import intel_memory
        stats = intel_memory.get_usage_stats("today")
        out: dict[str, float] = {}
        total = 0.0
        for s in stats:
            out[s["model_used"]] = round(s.get("cost_usd", 0) or 0, 4)
            total += s.get("cost_usd", 0) or 0
        out["total"] = round(total, 4)
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("get_cost_estimate failed: %s", exc)
        return {"total": 0.0}


# ─── Provider status display ─────────────────────────────────────────────────

def _sum(stats: list[dict], model_substr: str, field: str) -> float:
    return sum((s.get(field) or 0) for s in stats if model_substr in (s.get("model_used") or ""))


def format_provider_status() -> str:
    """Format /providers dashboard — reads SQLite so counters survive redeploys."""
    try:
        from modules import intel_memory
        session = intel_memory.get_usage_stats("session")
        today = intel_memory.get_usage_stats("today")
        month = intel_memory.get_usage_stats("month")
    except Exception as exc:  # noqa: BLE001
        log.warning("format_provider_status: get_usage_stats failed: %s", exc)
        session = today = month = []

    lines = ["\U0001f916 LLM HYBRID ROUTING STATUS", ""]

    # CRITICAL — Sonnet 4.6
    lines.append("CRITICAL ANALYSIS (Sonnet 4.6):")
    s_ok = "\U0001f7e2" if ANTHROPIC_API_KEY else "\u26aa"
    lines.append(f"  {s_ok} /reporte, /tesis, /kill")
    lines.append(
        f"  Sesi\u00f3n: {int(_sum(session, 'sonnet', 'reqs'))} req | "
        f"Hoy: {int(_sum(today, 'sonnet', 'reqs'))} req | "
        f"Mes: {int(_sum(month, 'sonnet', 'reqs'))} req"
    )
    lines.append(
        f"  Tokens hoy: {int(_sum(today, 'sonnet', 'tokens_in')):,} in + "
        f"{int(_sum(today, 'sonnet', 'tokens_out')):,} out"
    )
    lines.append(
        f"  Costo hoy: ${_sum(today, 'sonnet', 'cost_usd'):.4f} | "
        f"mes: ${_sum(month, 'sonnet', 'cost_usd'):.2f}"
    )
    lines.append("")

    # ROUTINE — Gemini free
    lines.append("ROUTINE PROCESSING (Gemini 2.5 Flash FREE):")
    g_ok = "\U0001f7e2" if GEMINI_API_KEY else "\u26aa"
    lines.append(f"  {g_ok} intel_processor, summaries, sentiment")
    lines.append(
        f"  Sesi\u00f3n: {int(_sum(session, 'gemini', 'reqs'))} req | "
        f"Hoy: {int(_sum(today, 'gemini', 'reqs'))} req | "
        f"Mes: {int(_sum(month, 'gemini', 'reqs'))} req"
    )
    lines.append(f"  Costo: $0.00 (free tier)")
    lines.append("")

    # FALLBACK — Haiku 4.5
    lines.append("FALLBACK (Haiku 4.5):")
    h_ok = "\U0001f7e2" if ANTHROPIC_API_KEY else "\u26aa"
    lines.append(f"  {h_ok} Activo si Sonnet/Gemini fallan")
    lines.append(
        f"  Sesi\u00f3n: {int(_sum(session, 'haiku', 'reqs'))} req | "
        f"Hoy: {int(_sum(today, 'haiku', 'reqs'))} req | "
        f"Mes: {int(_sum(month, 'haiku', 'reqs'))} req"
    )
    lines.append(
        f"  Costo hoy: ${_sum(today, 'haiku', 'cost_usd'):.4f} | "
        f"mes: ${_sum(month, 'haiku', 'cost_usd'):.2f}"
    )
    lines.append("")

    total_today = sum((s.get("cost_usd") or 0) for s in today)
    total_month = sum((s.get("cost_usd") or 0) for s in month)
    lines.append(f"COSTO HOY: ${total_today:.4f}")
    lines.append(f"COSTO MES: ${total_month:.2f}")
    lines.append(f"PROYECCI\u00d3N MES (si sigue ritmo hoy): ~${total_today * 30:.2f}")

    # Errors summary
    err_today = sum((s.get("errors") or 0) for s in today)
    if err_today:
        lines.append(f"\nErrores hoy: {err_today}")

    if _last_provider:
        lines.append("")
        lines.append(f"\u00daltimo an\u00e1lisis: {_last_provider} ({_last_provider_ts})")

    lines.append(f"\nSesi\u00f3n desde: {_session_start}")
    return "\n".join(lines)
