"""Unified LLM provider cascade.

Order of execution:
  1. Gemini (free, primary)
  2. OpenRouter free models (fallback)
  3. Groq (fallback)
  4. Anthropic (optional, only if USE_ANTHROPIC_FALLBACK=true and credit exists)

All providers return text completion given (system_prompt, user_message).
Fallback triggers on: rate limit (429), server error (5xx), quota error (4xx with
specific error codes), network timeout.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

# Env vars
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
USE_ANTHROPIC_FALLBACK = os.getenv("USE_ANTHROPIC_FALLBACK", "false").lower() == "true"

# Timeouts
REQUEST_TIMEOUT = 60.0

# ─── Usage tracking (in-memory, resets on redeploy) ─────────────────────────
_usage: dict[str, dict[str, Any]] = {
    "gemini-2.5-flash": {"calls": 0, "errors": 0, "last_success": None, "last_error": None},
    "openrouter-free": {"calls": 0, "errors": 0, "last_success": None, "last_error": None},
    "groq-llama-3.3": {"calls": 0, "errors": 0, "last_success": None, "last_error": None},
    "anthropic-haiku": {"calls": 0, "errors": 0, "last_success": None, "last_error": None},
}
_last_provider: str | None = None
_last_provider_ts: str | None = None


def get_usage_stats() -> dict[str, Any]:
    """Return usage stats for /providers command."""
    return {
        "providers": _usage,
        "last_provider": _last_provider,
        "last_provider_ts": _last_provider_ts,
        "total_calls": sum(p["calls"] for p in _usage.values()),
        "total_errors": sum(p["errors"] for p in _usage.values()),
    }


class LLMError(Exception):
    """Raised when all providers fail."""
    pass


async def _try_gemini(system_prompt: str, user_message: str, max_tokens: int = 4000) -> Optional[str]:
    """Google Gemini 2.5 Flash — primary, free tier."""
    if not GEMINI_API_KEY:
        return None

    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    params = {"key": GEMINI_API_KEY}

    payload = {
        "system_instruction": {
            "parts": [{"text": system_prompt}]
        },
        "contents": [{
            "role": "user",
            "parts": [{"text": user_message}]
        }],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": 0.3,
        }
    }

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(url, params=params, json=payload)
            if resp.status_code == 429:
                log.warning("Gemini rate limit hit")
                return None
            if resp.status_code >= 500:
                log.warning("Gemini server error %d", resp.status_code)
                return None
            resp.raise_for_status()
            data = resp.json()

            candidates = data.get("candidates", [])
            if not candidates:
                log.warning("Gemini returned no candidates")
                return None

            parts = candidates[0].get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts)
            return text if text else None
    except Exception as e:
        log.warning("Gemini call failed: %s", e)
        return None


async def _try_openrouter(system_prompt: str, user_message: str, max_tokens: int = 4000) -> Optional[str]:
    """OpenRouter free models — fallback 1.

    Uses DeepSeek V3 :free variant. If unavailable, tries other free models.
    """
    if not OPENROUTER_API_KEY:
        return None

    # Order of preference for free models (most capable first)
    free_models = [
        "deepseek/deepseek-chat-v3.1:free",
        "meta-llama/llama-3.3-70b-instruct:free",
        "google/gemini-2.0-flash-exp:free",
        "qwen/qwen-2.5-72b-instruct:free",
    ]

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": "https://blackcatdefi-bot.local",
        "X-Title": "Fondo Black Cat Bot",
    }

    for model in free_models:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code == 429:
                    log.warning("OpenRouter rate limit on %s, trying next", model)
                    continue
                if resp.status_code >= 500:
                    log.warning("OpenRouter server error on %s", model)
                    continue
                resp.raise_for_status()
                data = resp.json()
                text = data["choices"][0]["message"]["content"]
                return text
        except Exception as e:
            log.warning("OpenRouter %s failed: %s", model, e)
            continue

    return None


async def _try_groq(system_prompt: str, user_message: str, max_tokens: int = 4000) -> Optional[str]:
    """Groq Llama 3.3 70B — fallback 2, fastest free tier."""
    if not GROQ_API_KEY:
        return None

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}

    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code == 429:
                log.warning("Groq rate limit hit")
                return None
            if resp.status_code >= 500:
                log.warning("Groq server error %d", resp.status_code)
                return None
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
    except Exception as e:
        log.warning("Groq call failed: %s", e)
        return None


async def _try_anthropic(system_prompt: str, user_message: str, max_tokens: int = 4000) -> Optional[str]:
    """Anthropic Claude — fallback 3, only if USE_ANTHROPIC_FALLBACK=true and credit exists."""
    if not USE_ANTHROPIC_FALLBACK or not ANTHROPIC_API_KEY:
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
        return resp.content[0].text
    except Exception as e:
        log.warning("Anthropic call failed: %s", e)
        return None


async def complete(
    system_prompt: str,
    user_message: str,
    max_tokens: int = 4000,
) -> tuple[str, str]:
    """Try providers in cascade. Returns (text, provider_used).

    Raises LLMError if all providers fail.
    """
    global _last_provider, _last_provider_ts

    providers = [
        ("gemini-2.5-flash", _try_gemini),
        ("openrouter-free", _try_openrouter),
        ("groq-llama-3.3", _try_groq),
        ("anthropic-haiku", _try_anthropic),
    ]

    errors = []
    for name, fn in providers:
        log.info("Trying provider: %s", name)
        _usage[name]["calls"] += 1
        result = await fn(system_prompt, user_message, max_tokens)
        if result:
            log.info("Provider %s succeeded (%d chars)", name, len(result))
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            _usage[name]["last_success"] = now
            _last_provider = name
            _last_provider_ts = now
            return result, name
        _usage[name]["errors"] += 1
        _usage[name]["last_error"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        errors.append(name)

    raise LLMError(f"All providers failed: {errors}")


def format_provider_status() -> str:
    """Format provider status for /providers command."""
    stats = get_usage_stats()
    lines = [
        "\U0001f916 LLM PROVIDERS STATUS",
        "",
        "PRIMARY:",
    ]

    # Gemini
    g = stats["providers"]["gemini-2.5-flash"]
    g_icon = "\U0001f7e2" if GEMINI_API_KEY else "\u26aa"
    g_status = f"calls: {g['calls']}, errors: {g['errors']}" if GEMINI_API_KEY else "NO API KEY"
    lines.append(f"  {g_icon} Gemini 2.5 Flash \u2014 {g_status}")

    lines.append("")
    lines.append("FALLBACKS:")

    # OpenRouter
    o = stats["providers"]["openrouter-free"]
    o_icon = "\U0001f7e2" if OPENROUTER_API_KEY else "\u26aa"
    o_status = f"calls: {o['calls']}, errors: {o['errors']}" if OPENROUTER_API_KEY else "NO API KEY"
    lines.append(f"  {o_icon} OpenRouter (DeepSeek V3 :free) \u2014 {o_status}")

    # Groq
    q = stats["providers"]["groq-llama-3.3"]
    q_icon = "\U0001f7e2" if GROQ_API_KEY else "\u26aa"
    q_status = f"calls: {q['calls']}, errors: {q['errors']}" if GROQ_API_KEY else "NO API KEY"
    lines.append(f"  {q_icon} Groq (Llama 3.3 70B) \u2014 {q_status}")

    # Anthropic
    a = stats["providers"]["anthropic-haiku"]
    if USE_ANTHROPIC_FALLBACK and ANTHROPIC_API_KEY:
        a_icon = "\U0001f7e2"
        a_status = f"calls: {a['calls']}, errors: {a['errors']}"
    else:
        a_icon = "\u26aa"
        a_status = "DISABLED (USE_ANTHROPIC_FALLBACK=false)" if ANTHROPIC_API_KEY else "NO API KEY"
    lines.append(f"  {a_icon} Anthropic (Claude Haiku) \u2014 {a_status}")

    lines.append("")

    if stats["last_provider"]:
        lines.append(f"\u00daltimo an\u00e1lisis generado por: {stats['last_provider']} ({stats['last_provider_ts']})")

    total = stats["total_calls"]
    lines.append(f"Total requests (desde \u00faltimo deploy): {total}")

    # Cost estimate: $0 for free providers
    anthropic_calls = stats["providers"]["anthropic-haiku"]["calls"] - stats["providers"]["anthropic-haiku"]["errors"]
    if anthropic_calls > 0:
        est_cost = anthropic_calls * 0.003  # rough estimate per Haiku call
        lines.append(f"Costo estimado: ~${est_cost:.3f} (solo Anthropic)")
    else:
        lines.append("Costo total: $0.00 \u2705")

    return "\n".join(lines)
