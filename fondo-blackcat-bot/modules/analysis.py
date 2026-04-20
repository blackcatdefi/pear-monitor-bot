"""Anthropic Claude integration for report generation + persistent thesis."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from anthropic import AsyncAnthropic, APIError, RateLimitError

from config import (
    ANTHROPIC_API_KEY,
    ANTHROPIC_MODEL,
    DATA_DIR,
    HAIKU_MODEL,
    LAST_ANALYSIS_FILE,
    USE_HAIKU_FALLBACK,
)
from templates.formatters import compile_raw_data
from templates.system_prompt import SYSTEM_PROMPT, THESIS_PROMPT

log = logging.getLogger(__name__)

_client: AsyncAnthropic | None = None

THESIS_FILE = os.path.join(DATA_DIR, "thesis_state.json")
MAX_HISTORY = 30  # keep last 30 thesis snapshots


def get_client() -> AsyncAnthropic | None:
    global _client
    if not ANTHROPIC_API_KEY:
        return None
    if _client is None:
        _client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    return _client


# ─── Persistent thesis state ───────────────────────────────────────────────


def _load_thesis() -> dict[str, Any]:
    """Load thesis state from disk. Returns empty dict if none."""
    if not os.path.isfile(THESIS_FILE):
        return {}
    try:
        with open(THESIS_FILE) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        log.warning("Could not load thesis state from %s", THESIS_FILE)
        return {}


def _save_thesis(state: dict[str, Any]) -> None:
    """Save thesis state to disk."""
    try:
        os.makedirs(os.path.dirname(THESIS_FILE), exist_ok=True)
        with open(THESIS_FILE, "w") as f:
            json.dump(state, f, indent=2, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        log.exception("Could not save thesis state to %s", THESIS_FILE)


def _thesis_context(state: dict[str, Any]) -> str:
    """Format previous thesis state for injection into the prompt."""
    if not state or not state.get("current"):
        return ""
    parts = [
        "\n\n═══════ ESTADO PREVIO DE LA TESIS (auto-actualizado) ═══════",
        f"Última actualización: {state.get('last_updated', 'desconocido')}",
        f"Reportes acumulados: {state.get('report_count', 0)}",
        "",
        state["current"],
    ]
    # Include key learnings if present
    learnings = state.get("key_learnings", [])
    if learnings:
        parts.append("")
        parts.append("APRENDIZAJES ACUMULADOS:")
        for l in learnings[-10:]:  # last 10
            parts.append(f"  • [{l.get('date', '?')}] {l.get('text', '')}")
    parts.append("═══════ FIN ESTADO PREVIO ═══════")
    return "\n".join(parts)


THESIS_UPDATE_PROMPT = """Basándote en el reporte que acabás de generar y el estado previo de la tesis (si hay), actualiza el estado de la tesis.

Respondé EXCLUSIVAMENTE en este formato JSON (sin markdown, sin backticks, solo JSON puro):

{
  "components": {
    "war_trade": {"status": "VALIDA|NEUTRO|INVALIDA", "detail": "dato específico corto", "action": "MANTENER|AGREGAR|REDUCIR|SALIR"},
    "alt_short_bleed": {"status": "VALIDA|NEUTRO|INVALIDA", "detail": "dato específico corto", "action": "MANTENER|AGREGAR|REDUCIR|SALIR"},
    "hype_flywheel": {"status": "VALIDA|NEUTRO|INVALIDA", "detail": "dato específico corto", "action": "MANTENER|AGREGAR|REDUCIR|SALIR"},
    "fed_hawkish": {"status": "VALIDA|NEUTRO|INVALIDA", "detail": "dato específico corto", "action": "MANTENER|AGREGAR|REDUCIR|SALIR"},
    "trade_del_ciclo": {"status": "VALIDA|NEUTRO|INVALIDA", "detail": "dato específico corto", "action": "MANTENER|AGREGAR|REDUCIR|SALIR"}
  },
  "overall_conviction": "1-10 (10=máxima convicción en la tesis)",
  "new_learnings": ["aprendizaje nuevo 1 de este reporte (si hay)", "aprendizaje 2 (si hay)"],
  "thesis_evolution": "1-2 oraciones: cómo cambió la tesis respecto al reporte anterior (o 'primera ejecución' si no hay previo)",
  "summary": "Resumen ejecutivo de la tesis actualizada en 3-4 líneas para mostrar al usuario"
}"""


def _save_last_analysis(report_text: str) -> None:
    """Cache last successful analysis to disk."""
    try:
        cache_data = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "report_text": report_text,
        }
        os.makedirs(os.path.dirname(LAST_ANALYSIS_FILE), exist_ok=True)
        with open(LAST_ANALYSIS_FILE, "w") as f:
            json.dump(cache_data, f, indent=2, ensure_ascii=False, default=str)
        log.info("Cached last successful analysis to %s", LAST_ANALYSIS_FILE)
    except Exception:
        log.warning("Could not cache last analysis to %s", LAST_ANALYSIS_FILE)


def _load_last_analysis() -> dict[str, Any] | None:
    """Load last successful analysis cache."""
    if not os.path.isfile(LAST_ANALYSIS_FILE):
        return None
    try:
        with open(LAST_ANALYSIS_FILE) as f:
            return json.load(f)
    except Exception:
        log.warning("Could not load last analysis cache")
        return None


def _format_api_error_msg(error: Exception) -> tuple[str, str]:
    """Identify API error type and return (error_type, resolution_url)."""
    error_str = str(error).lower()

    if "insufficient" in error_str and "credits" in error_str:
        return ("credits_insufficient", "https://console.anthropic.com/settings/billing")
    elif "429" in error_str or "rate" in error_str or "rate_limit" in error_str:
        return ("rate_limited", "https://console.anthropic.com/settings/billing")
    elif "401" in error_str or "unauthorized" in error_str:
        return ("auth_failed", "https://console.anthropic.com/settings/billing")
    else:
        return ("api_error", "https://status.anthropic.com")


async def _update_thesis_state(report_text: str, user_data: str) -> str | None:
    """Call Claude to extract updated thesis state from the report."""
    client = get_client()
    if client is None:
        return None

    state = _load_thesis()
    prev_context = _thesis_context(state)

    try:
        resp = await client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=2000,
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT + prev_context,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[
                {"role": "user", "content": user_data},
                {"role": "assistant", "content": report_text},
                {"role": "user", "content": THESIS_UPDATE_PROMPT},
            ],
        )

        raw = ""
        for block in resp.content:
            if hasattr(block, "text"):
                raw += block.text

        # Parse the JSON response
        raw = raw.strip()
        # Remove any markdown code block wrapping if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

        update = json.loads(raw)

        # Build the human-readable thesis summary
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        components = update.get("components", {})
        status_map = {"VALIDA": "✅", "NEUTRO": "⚠️", "INVALIDA": "🔴"}

        summary_lines = [f"📊 TESIS ACTUALIZADA — {now}"]
        summary_lines.append(f"Convicción global: {update.get('overall_conviction', '?')}/10")
        summary_lines.append("")

        for key, label in [
            ("war_trade", "War Trade"),
            ("alt_short_bleed", "Alt Short Bleed"),
            ("hype_flywheel", "HYPE Flywheel"),
            ("fed_hawkish", "Fed Hawkish"),
            ("trade_del_ciclo", "Trade del Ciclo"),
        ]:
            c = components.get(key, {})
            icon = status_map.get(c.get("status", ""), "❓")
            summary_lines.append(
                f"{icon} {label}: {c.get('detail', 'n/a')} → {c.get('action', '?')}"
            )

        summary_lines.append("")
        summary_lines.append(update.get("thesis_evolution", ""))

        current_text = "\n".join(summary_lines)

        # Update state
        new_learnings = update.get("new_learnings", [])
        existing_learnings = state.get("key_learnings", [])
        for l in new_learnings:
            if l and l.strip():
                existing_learnings.append({"date": now, "text": l.strip()})

        # Trim to last 50 learnings
        existing_learnings = existing_learnings[-50:]

        # Add to history
        history = state.get("history", [])
        history.append({
            "date": now,
            "conviction": update.get("overall_conviction"),
            "components": components,
        })
        history = history[-MAX_HISTORY:]

        new_state = {
            "last_updated": now,
            "report_count": state.get("report_count", 0) + 1,
            "current": current_text,
            "components": components,
            "overall_conviction": update.get("overall_conviction"),
            "thesis_evolution": update.get("thesis_evolution"),
            "key_learnings": existing_learnings,
            "history": history,
        }
        _save_thesis(new_state)
        log.info("Thesis state updated (report #%d)", new_state["report_count"])

        # Return user-facing summary
        user_summary = update.get("summary", current_text)
        return f"🧬 TESIS AUTO-ACTUALIZADA (reporte #{new_state['report_count']})\n\n{current_text}\n\n{user_summary}"

    except json.JSONDecodeError as e:
        log.warning("Thesis update JSON parse failed: %s — raw: %s", e, raw[:200])
        return None
    except Exception:  # noqa: BLE001
        log.exception("Thesis auto-update failed")
        return None


# ─── Report generation ─────────────────────────────────────────────────────


async def generate_report(
    portfolio: list[dict[str, Any]] | None,
    hyperlend: dict[str, Any] | None,
    market: dict[str, Any] | None,
    unlocks: dict[str, Any] | None,
    telegram_intel: dict[str, Any] | None,
) -> tuple[str, str | None]:
    """Generate report + auto-update thesis with graceful degradation on API failure.

    Returns (report_text, thesis_update_text_or_None).
    On API failure: returns raw data + error message + cached analysis if available.
    Implements fallback to Haiku model if primary fails and USE_HAIKU_FALLBACK is enabled.
    """
    client = get_client()
    if client is None:
        return "❌ ANTHROPIC_API_KEY no configurada — no se puede generar el reporte.", None

    user_content = compile_raw_data(portfolio, hyperlend, market, unlocks, telegram_intel)

    # Inject previous thesis state into the system prompt
    state = _load_thesis()
    prev_thesis = _thesis_context(state)
    full_system = SYSTEM_PROMPT + prev_thesis

    # Try primary model
    try:
        resp = await client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=8000,
            system=[{
                "type": "text",
                "text": full_system,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_content}],
        )

        parts = []
        for block in resp.content:
            if hasattr(block, "text"):
                parts.append(block.text)

        report_text = "\n".join(parts).strip() or "(reporte vacío)"

        # Cache successful report
        _save_last_analysis(report_text)

        # Auto-update thesis in the background
        thesis_update = await _update_thesis_state(report_text, user_content)

        return report_text, thesis_update

    except (APIError, RateLimitError) as api_exc:
        error_type, resolution_url = _format_api_error_msg(api_exc)
        log.warning("Anthropic API error (%s): %s", error_type, api_exc)

        # Try Haiku fallback if enabled
        if USE_HAIKU_FALLBACK:
            log.info("USE_HAIKU_FALLBACK=true, retrying with Haiku model...")
            try:
                resp = await client.messages.create(
                    model=HAIKU_MODEL,
                    max_tokens=4000,
                    system=[{
                        "type": "text",
                        "text": full_system,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    messages=[{"role": "user", "content": user_content}],
                )

                parts = []
                for block in resp.content:
                    if hasattr(block, "text"):
                        parts.append(block.text)

                report_text = "\n".join(parts).strip() or "(reporte vacío con Haiku)"
                _save_last_analysis(report_text)
                thesis_update = await _update_thesis_state(report_text, user_content)
                return report_text, thesis_update

            except Exception as haiku_exc:
                log.warning("Haiku fallback also failed: %s", haiku_exc)

        # Both attempts failed — return degraded report
        degraded = _build_degraded_report(portfolio, hyperlend, market, unlocks, telegram_intel, error_type, resolution_url)
        return degraded, None

    except Exception as exc:  # noqa: BLE001
        log.exception("Anthropic call failed (non-API error)")
        error_type, resolution_url = _format_api_error_msg(exc)
        degraded = _build_degraded_report(portfolio, hyperlend, market, unlocks, telegram_intel, error_type, resolution_url)
        return degraded, None


def _build_degraded_report(
    portfolio: list[dict[str, Any]] | None,
    hyperlend: dict[str, Any] | None,
    market: dict[str, Any] | None,
    unlocks: dict[str, Any] | None,
    telegram_intel: dict[str, Any] | None,
    error_type: str,
    resolution_url: str,
) -> str:
    """Build a degraded report with raw data when API fails."""
    lines = [
        "⚠️ ANÁLISIS IA TEMPORALMENTE NO DISPONIBLE",
        f"Razón: {error_type}",
        f"Resolver en: {resolution_url}",
        "",
        "El reporte abajo tiene la data cruda — revisar manualmente.",
        "",
        "═════════════════════════════════════════════════════════════",
        "",
    ]

    # Add raw data snapshot
    if portfolio:
        lines.append("PORTFOLIO:")
        for p in portfolio:
            if isinstance(p, dict) and p.get("status") == "ok":
                data = p.get("data", {})
                label = data.get("label", "?")
                eq = data.get("account_value", 0)
                upnl = data.get("unrealized_pnl_total", 0)
                lines.append(f"  • {label}: Equity ${eq:,.0f} | UPnL ${upnl:,.0f}")
        lines.append("")

    if hyperlend and isinstance(hyperlend, list):
        lines.append("HYPERLEND:")
        for hl in hyperlend:
            if hl.get("status") == "ok":
                data = hl.get("data", {})
                label = data.get("label", "?")
                coll = data.get("total_collateral_usd", 0)
                hf = data.get("health_factor", "?")
                lines.append(f"  • {label}: Collateral ${coll:,.0f} | HF {hf}")
        lines.append("")

    if market and isinstance(market, dict) and market.get("status") == "ok":
        lines.append("MARKET DATA:")
        data = market.get("data", {})
        btc_px = data.get("BTC", {}).get("price", "?")
        hype_px = data.get("HYPE", {}).get("price", "?")
        lines.append(f"  • BTC: ${btc_px}")
        lines.append(f"  • HYPE: ${hype_px}")
        lines.append("")

    # Include cached analysis if available
    cached = _load_last_analysis()
    if cached:
        ts = cached.get("timestamp_utc", "?")[:16]
        hours_ago = "?"
        try:
            cached_dt = datetime.fromisoformat(cached.get("timestamp_utc", ""))
            now_dt = datetime.now(timezone.utc)
            hours_ago = int((now_dt - cached_dt.replace(tzinfo=timezone.utc)).total_seconds() / 3600)
        except Exception:
            pass

        lines.append("═════════════════════════════════════════════════════════════")
        lines.append(f"📎 ÚLTIMO ANÁLISIS IA DISPONIBLE (hace {hours_ago}h)")
        lines.append(f"Timestamp: {ts}")
        lines.append("")
        lines.append(cached.get("report_text", ""))

    return "\n".join(lines)


async def generate_thesis_check(
    portfolio: list[dict[str, Any]] | None,
    hyperlend: dict[str, Any] | None,
    market: dict[str, Any] | None,
) -> str:
    """Thesis check — uses persistent state + fresh data with graceful degradation."""
    client = get_client()
    if client is None:
        return "❌ ANTHROPIC_API_KEY no configurada."

    user_content = compile_raw_data(portfolio, hyperlend, market, None, None)

    # Inject thesis state into thesis prompt
    state = _load_thesis()
    prev_thesis = _thesis_context(state)
    full_prompt = THESIS_PROMPT + prev_thesis

    try:
        resp = await client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=2000,
            system=[{
                "type": "text",
                "text": full_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_content}],
        )

        parts = []
        for block in resp.content:
            if hasattr(block, "text"):
                parts.append(block.text)

        return "\n".join(parts).strip() or "(análisis vacío)"

    except (APIError, RateLimitError) as api_exc:
        error_type, resolution_url = _format_api_error_msg(api_exc)
        log.warning("Thesis check API error (%s): %s", error_type, api_exc)

        # Try Haiku fallback
        if USE_HAIKU_FALLBACK:
            try:
                resp = await client.messages.create(
                    model=HAIKU_MODEL,
                    max_tokens=1000,
                    system=[{
                        "type": "text",
                        "text": full_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    messages=[{"role": "user", "content": user_content}],
                )
                parts = []
                for block in resp.content:
                    if hasattr(block, "text"):
                        parts.append(block.text)
                return "\n".join(parts).strip() or "(análisis con Haiku)"
            except Exception:
                pass

        # Fallback: return cached state if available
        if state.get("current"):
            return f"⚠️ Usando estado previo de la tesis (análisis en vivo no disponible):\n\n{state['current']}"

        return f"❌ Error de IA ({error_type}). Resolver en {resolution_url}"

    except Exception as exc:  # noqa: BLE001
        log.exception("Thesis check failed")
        error_type, resolution_url = _format_api_error_msg(exc)
        return f"❌ Error: {error_type}. Ver {resolution_url}"
