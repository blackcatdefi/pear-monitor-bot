"""Anthropic Claude integration for report generation + persistent thesis."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from anthropic import AsyncAnthropic

from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL, DATA_DIR
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
        "\n\n\u2550\u2550\u2550 ESTADO PREVIO DE LA TESIS (auto-actualizado) \u2550\u2550\u2550",
        f"\u00daltima actualizaci\u00f3n: {state.get('last_updated', 'desconocido')}",
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
            parts.append(f"  \u2022 [{l.get('date', '?')}] {l.get('text', '')}")
    parts.append("\u2550\u2550\u2550 FIN ESTADO PREVIO \u2550\u2550\u2550")
    return "\n".join(parts)


THESIS_UPDATE_PROMPT = """Bas\u00e1ndote en el reporte que acab\u00e1s de generar y el estado previo de la tesis (si hay), actualiz\u00e1 el estado de la tesis.

Respond\u00e9 EXCLUSIVAMENTE en este formato JSON (sin markdown, sin backticks, solo JSON puro):

{
  "components": {
    "war_trade": {"status": "VALIDA|NEUTRO|INVALIDA", "detail": "dato espec\u00edfico corto", "action": "MANTENER|AGREGAR|REDUCIR|SALIR"},
    "alt_short_bleed": {"status": "VALIDA|NEUTRO|INVALIDA", "detail": "dato espec\u00edfico corto", "action": "MANTENER|AGREGAR|REDUCIR|SALIR"},
    "hype_flywheel": {"status": "VALIDA|NEUTRO|INVALIDA", "detail": "dato espec\u00edfico corto", "action": "MANTENER|AGREGAR|REDUCIR|SALIR"},
    "fed_hawkish": {"status": "VALIDA|NEUTRO|INVALIDA", "detail": "dato espec\u00edfico corto", "action": "MANTENER|AGREGAR|REDUCIR|SALIR"},
    "trade_del_ciclo": {"status": "VALIDA|NEUTRO|INVALIDA", "detail": "dato espec\u00edfico corto", "action": "MANTENER|AGREGAR|REDUCIR|SALIR"}
  },
  "overall_conviction": "1-10 (10=m\u00e1xima convicci\u00f3n en la tesis)",
  "new_learnings": ["aprendizaje nuevo 1 de este reporte (si hay)", "aprendizaje 2 (si hay)"],
  "thesis_evolution": "1-2 oraciones: c\u00f3mo cambi\u00f3 la tesis respecto al reporte anterior (o 'primera ejecuci\u00f3n' si no hay previo)",
  "summary": "Resumen ejecutivo de la tesis actualizada en 3-4 l\u00edneas para mostrar al usuario"
}"""


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
        status_map = {"VALIDA": "\u2705", "NEUTRO": "\u26a0\ufe0f", "INVALIDA": "\U0001f534"}

        summary_lines = [f"\U0001f4ca TESIS ACTUALIZADA \u2014 {now}"]
        summary_lines.append(f"Convicci\u00f3n global: {update.get('overall_conviction', '?')}/10")
        summary_lines.append("")

        for key, label in [
            ("war_trade", "War Trade"),
            ("alt_short_bleed", "Alt Short Bleed"),
            ("hype_flywheel", "HYPE Flywheel"),
            ("fed_hawkish", "Fed Hawkish"),
            ("trade_del_ciclo", "Trade del Ciclo"),
        ]:
            c = components.get(key, {})
            icon = status_map.get(c.get("status", ""), "\u2753")
            summary_lines.append(
                f"{icon} {label}: {c.get('detail', 'n/a')} \u2192 {c.get('action', '?')}"
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
        return f"\U0001f9ec TESIS AUTO-ACTUALIZADA (reporte #{new_state['report_count']})\n\n{current_text}\n\n{user_summary}"

    except json.JSONDecodeError as e:
        log.warning("Thesis update JSON parse failed: %s \u2014 raw: %s", e, raw[:200])
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
    """Generate report + auto-update thesis.

    Returns (report_text, thesis_update_text_or_None).
    """
    client = get_client()
    if client is None:
        return "\u274c ANTHROPIC_API_KEY no configurada \u2014 no se puede generar el reporte.", None

    user_content = compile_raw_data(portfolio, hyperlend, market, unlocks, telegram_intel)

    # Inject previous thesis state into the system prompt
    state = _load_thesis()
    prev_thesis = _thesis_context(state)
    full_system = SYSTEM_PROMPT + prev_thesis

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

        report_text = "\n".join(parts).strip() or "(reporte vac\u00edo)"

        # Auto-update thesis in the background
        thesis_update = await _update_thesis_state(report_text, user_content)

        return report_text, thesis_update

    except Exception as exc:  # noqa: BLE001
        log.exception("Anthropic call failed")
        return f"\u274c Error generando reporte con Claude: {exc}", None


async def generate_thesis_check(
    portfolio: list[dict[str, Any]] | None,
    hyperlend: dict[str, Any] | None,
    market: dict[str, Any] | None,
) -> str:
    """Thesis check \u2014 uses persistent state + fresh data."""
    client = get_client()
    if client is None:
        return "\u274c ANTHROPIC_API_KEY no configurada."

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

        return "\n".join(parts).strip() or "(an\u00e1lisis vac\u00edo)"

    except Exception as exc:  # noqa: BLE001
        log.exception("Thesis check failed")
        return f"\u274c Error: {exc}"

