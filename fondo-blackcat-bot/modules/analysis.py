"""LLM-powered report generation + persistent thesis.

Uses hybrid router (Sonnet for critical, Gemini for routine, Haiku fallback)
via modules.llm_router. Falls back to degraded raw-data report if all fail.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from config import (
    DATA_DIR,
    LAST_ANALYSIS_FILE,
)
from modules.llm_router import route_request, LLMError
from templates.formatters import compile_raw_data
from templates.system_prompt import SYSTEM_PROMPT, THESIS_PROMPT, build_fund_state_block
# R-FINAL bug-1: prepend an on-chain authoritative state block ABOVE the
# legacy hardcoded fund_state. The autodetect block declares basket-active
# status from real positions, so the LLM stops emitting "ANOMALÍA CRÍTICA"
# when fund_state.py is stale (basket v6 was active 30 abr while
# BASKET_STATUS still said v4 closed).
from auto.fund_state_v2 import build_authoritative_state_block as _onchain_state_block
# R-BOT-TERMINOLOGY-UNIFY (2026-05-07) — Bug #5 LMEC bear invalidation
# triggers. Injected at the top of the prompt and surfaced in /tesis.
from modules.lmec_triggers import evaluate_lmec_triggers, format_lmec_block


def _lmec_state_block(market: dict[str, Any] | None = None) -> str:
    """Return the LMEC triggers block ready for prompt injection.

    Uses ``market`` for live BTC price; the rest of the legs are env-var
    driven (TradingView weekly close inputs). Errors are swallowed so a
    LMEC failure never breaks /reporte or /tesis generation.
    """
    try:
        result = evaluate_lmec_triggers(market)
    except Exception:  # noqa: BLE001
        log.exception("lmec_triggers evaluation failed")
        return ""
    block = [
        "═══════ LMEC TRIGGERS — BEAR INVALIDATION ═══════",
        "Estas son las 4 condiciones formales que invalidan la tesis bear.",
        "Si ≥1 está ✅ VALIDA, la convicción global del fondo debe bajar.",
        "",
        format_lmec_block(result),
        "═══════ FIN LMEC TRIGGERS ═══════",
        "",
    ]
    return "\n".join(block)


async def _full_state_block(market: dict[str, Any] | None = None) -> str:
    """Return on-chain truth + LMEC triggers + hardcoded legacy block.

    Ordering matters: the on-chain block goes FIRST so the LLM treats
    on-chain reality as authoritative; the LMEC triggers go SECOND so
    bear-invalidation conditions are visible before the static fund
    constants. Any contradicting stale constants below are explicitly
    framed as lower-priority.
    """
    try:
        onchain = await _onchain_state_block()
    except Exception:  # noqa: BLE001
        onchain = ""
    lmec = _lmec_state_block(market)
    return (onchain or "") + (lmec or "") + build_fund_state_block()

log = logging.getLogger(__name__)

THESIS_FILE = os.path.join(DATA_DIR, "thesis_state.json")
# Plain-text, always-written thesis snapshot. Lives alongside thesis_state.json
# but survives even when the structured LLM-JSON update fails (which was the
# root cause of /tesis returning "No hay tesis guardada" on 2026-04-22 after
# a successful /reporte). /tesis falls back to this file when thesis_state.json
# has no `components` key.
THESIS_LATEST_FILE = os.path.join(DATA_DIR, "tesis_latest.md")
MAX_HISTORY = 30  # keep last 30 thesis snapshots


# ─── Persistent thesis state ──────────────────────────────────────────────────


def _load_thesis() -> dict[str, Any]:
    """Load thesis state from disk. Returns empty dict if none.

    R-BOT-TERMINOLOGY-UNIFY (2026-05-07) — auto-migrate legacy key
    ``alt_short_bleed`` → ``super_basket_stage_6`` in components/history
    on load so the rename is forward-compatible without losing
    accumulated thesis state. The migration is idempotent and silent
    when there's nothing to rename.
    """
    if not os.path.isfile(THESIS_FILE):
        return {}
    try:
        with open(THESIS_FILE) as f:
            state = json.load(f)
    except Exception:  # noqa: BLE001
        log.warning("Could not load thesis state from %s", THESIS_FILE)
        return {}
    return _migrate_thesis_state(state)


def _migrate_thesis_state(state: dict[str, Any]) -> dict[str, Any]:
    """Rename legacy ``alt_short_bleed`` key to ``super_basket_stage_6``.

    Touches:
      * ``state["components"]`` (current thesis snapshot)
      * each entry in ``state["history"]`` ``components`` dict.

    Returns the (possibly mutated) state dict. Safe to call repeatedly —
    if the migration already ran the function is a no-op.
    """
    if not isinstance(state, dict):
        return state
    LEGACY = "alt_short_bleed"
    NEW = "super_basket_stage_6"
    migrated = False
    components = state.get("components")
    if isinstance(components, dict) and LEGACY in components:
        if NEW not in components:
            components[NEW] = components[LEGACY]
            migrated = True
        del components[LEGACY]
        state["components"] = components
    history = state.get("history")
    if isinstance(history, list):
        for entry in history:
            if not isinstance(entry, dict):
                continue
            ec = entry.get("components")
            if isinstance(ec, dict) and LEGACY in ec:
                if NEW not in ec:
                    ec[NEW] = ec[LEGACY]
                    migrated = True
                del ec[LEGACY]
                entry["components"] = ec
    if migrated:
        log.info(
            "thesis migration: renamed legacy %r → %r in thesis state",
            LEGACY,
            NEW,
        )
    return state


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
    "super_basket_stage_6": {"status": "VALIDA|NEUTRO|INVALIDA", "detail": "dato específico corto", "action": "MANTENER|AGREGAR|REDUCIR|SALIR"},
    "hype_flywheel": {"status": "VALIDA|NEUTRO|INVALIDA", "detail": "dato específico corto", "action": "MANTENER|AGREGAR|REDUCIR|SALIR"},
    "fed_hawkish": {"status": "VALIDA|NEUTRO|INVALIDA", "detail": "dato específico corto", "action": "MANTENER|AGREGAR|REDUCIR|SALIR"},
    "lmec_bear_invalidation": {"status": "VALIDA|NEUTRO|INVALIDA", "detail": "dato específico corto", "action": "MANTENER|AGREGAR|REDUCIR|SALIR"}
  },
  "overall_conviction": "1-10 (10=máxima convicción en la tesis)",
  "new_learnings": ["aprendizaje nuevo 1 de este reporte (si hay)", "aprendizaje 2 (si hay)"],
  "thesis_evolution": "1-2 oraciones: cómo cambió la tesis respecto al reporte anterior (o 'primera ejecución' si no hay previo)",
  "summary": "Resumen ejecutivo de la tesis actualizada en 3-4 líneas para mostrar al usuario"
}"""


def _extract_report_sections(report_text: str) -> dict[str, str]:
    """Slice numbered sections 3 (MACRO) and 6 (RESUMEN EJECUTIVO) from a report.

    The system prompt pins the report format: sections are prefixed with
    '3. MACRO & GUERRA' / '6. RESUMEN EJECUTIVO' etc. We cut between headers.
    Falls back to {} if the format is not recognised.
    """
    import re

    out: dict[str, str] = {}
    # Numbered headers like "3. MACRO & GUERRA" or "6. RESUMEN EJECUTIVO"
    # capture until the next numbered header or the end-of-report marker.
    for key, label in (
        ("macro", r"3\.\s*MACRO\s*&\s*GUERRA"),
        ("resumen", r"6\.\s*RESUMEN\s*EJECUTIVO"),
    ):
        pattern = rf"{label}(.*?)(?=^\s*\d+\.\s+[A-Z\u00c0-\u024f]+|\u2550{{2,}}\s*FIN|\Z)"
        m = re.search(pattern, report_text, flags=re.IGNORECASE | re.DOTALL | re.MULTILINE)
        if m:
            out[key] = m.group(1).strip()
    return out


def _save_tesis_latest(report_text: str, provider: str = "unknown") -> None:
    """Persist a human-readable thesis snapshot to disk.

    This is the /tesis fallback path. Always writes, even when the LLM-JSON
    thesis update later fails (that was the 2026-04-22 bug: /tesis said "No
    hay tesis guardada" immediately after a successful /reporte because the
    structured thesis save depended on the LLM returning parseable JSON).
    """
    try:
        sections = _extract_report_sections(report_text)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M UTC")
        lines = [
            f"# Tesis \u2014 {ts}",
            f"_Generada por /reporte (provider: {provider})_",
            "",
        ]
        if sections.get("macro"):
            lines.append("## Macro & Guerra")
            lines.append(sections["macro"])
            lines.append("")
        if sections.get("resumen"):
            lines.append("## Resumen ejecutivo")
            lines.append(sections["resumen"])
            lines.append("")
        if not sections:
            # Preserve the full report if header parsing failed — better than
            # losing the data entirely.
            lines.append("## Reporte completo (parse de secciones fall\u00f3)")
            lines.append(report_text)
            lines.append("")
        os.makedirs(os.path.dirname(THESIS_LATEST_FILE), exist_ok=True)
        with open(THESIS_LATEST_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        log.info("Thesis snapshot written to %s (%d sections)", THESIS_LATEST_FILE, len(sections))
    except Exception:
        log.exception("Could not write thesis snapshot to %s", THESIS_LATEST_FILE)


def load_tesis_latest() -> tuple[str | None, str | None]:
    """Return (content, last_modified_iso) or (None, None) if missing."""
    if not os.path.isfile(THESIS_LATEST_FILE):
        return None, None
    try:
        with open(THESIS_LATEST_FILE, encoding="utf-8") as f:
            content = f.read()
        mtime = os.path.getmtime(THESIS_LATEST_FILE)
        iso = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return content, iso
    except Exception:
        log.exception("Could not load thesis snapshot")
        return None, None


def _save_last_analysis(report_text: str, provider: str = "unknown") -> None:
    """Cache last successful analysis to disk."""
    try:
        cache_data = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "report_text": report_text,
            "provider": provider,
        }
        os.makedirs(os.path.dirname(LAST_ANALYSIS_FILE), exist_ok=True)
        with open(LAST_ANALYSIS_FILE, "w") as f:
            json.dump(cache_data, f, indent=2, ensure_ascii=False, default=str)
        log.info("Cached last successful analysis to %s (provider: %s)", LAST_ANALYSIS_FILE, provider)
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


async def _update_thesis_state(
    report_text: str,
    user_data: str,
    market: dict[str, Any] | None = None,
) -> str | None:
    """Call LLM to extract updated thesis state from the report.

    R-BOT-TERMINOLOGY-UNIFY (2026-05-07): the LMEC bear-invalidation
    block is appended to the user-facing thesis message so BCD sees the
    4 trigger statuses on every /reporte alongside the per-component
    status icons.
    """
    state = _load_thesis()
    prev_context = _thesis_context(state)

    thesis_user_msg = (
        f"REPORTE GENERADO:\n{report_text}\n\n"
        f"DATA CRUDA UTILIZADA:\n{user_data}\n\n"
        f"{THESIS_UPDATE_PROMPT}"
    )

    try:
        raw, provider = await route_request(
            "tesis_update",
            (await _full_state_block()) + SYSTEM_PROMPT + prev_context,
            thesis_user_msg,
            max_tokens=2000,
        )

        # Clean up response
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

        # Extract JSON from response
        json_start = raw.find("{")
        json_end = raw.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            raw = raw[json_start:json_end]

        update = json.loads(raw)

        # Build thesis summary
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        components = update.get("components", {})

        status_map = {"VALIDA": "\u2705", "NEUTRO": "\u26a0\ufe0f", "INVALIDA": "\U0001f534"}

        summary_lines = [f"\U0001f4ca TESIS ACTUALIZADA \u2014 {now}"]
        summary_lines.append(f"Convicci\u00f3n global: {update.get('overall_conviction', '?')}/10")
        summary_lines.append("")

        for key, label in [
            ("war_trade", "War Trade"),
            ("super_basket_stage_6", "Super Basket Stage 6"),
            ("hype_flywheel", "HYPE Flywheel"),
            ("fed_hawkish", "Fed Hawkish"),
            # R-NOPRELIQ + REMOVE BLOFIN (2026-05-15): Trade del Ciclo (Blofin) ELIMINADO.
            ("lmec_bear_invalidation", "LMEC Bear Invalidation"),
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
        existing_learnings = existing_learnings[-50:]

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
        log.info("Thesis state updated (report #%d) via %s", new_state["report_count"], provider)

        user_summary = update.get("summary", current_text)
        # R-BOT-TERMINOLOGY-UNIFY: surface the 4 LMEC bear-invalidation
        # legs alongside the LLM thesis update so BCD has the full
        # picture in one Telegram message.
        try:
            lmec_block = format_lmec_block(evaluate_lmec_triggers(market))
        except Exception:  # noqa: BLE001
            log.exception("LMEC block render failed (non-fatal)")
            lmec_block = ""
        out = (
            f"\U0001f9ec TESIS AUTO-ACTUALIZADA (reporte #{new_state['report_count']})"
            f"\n\n{current_text}\n\n{user_summary}"
            f"\n\n_Tesis actualizada por: {provider}_"
        )
        if lmec_block:
            out = out + "\n\n" + lmec_block
        return out

    except json.JSONDecodeError as e:
        log.warning("Thesis update JSON parse failed: %s \u2014 raw: %s", e, raw[:200] if raw else "empty")
        return None
    except LLMError:
        log.warning("Thesis auto-update failed \u2014 all LLM providers down")
        return None
    except Exception:  # noqa: BLE001
        log.exception("Thesis auto-update failed")
        return None


# ─── Report generation ─────────────────────────────────────────────────────────


async def generate_report(
    portfolio: list[dict[str, Any]] | None,
    hyperlend: dict[str, Any] | None,
    market: dict[str, Any] | None,
    unlocks: dict[str, Any] | None,
    telegram_intel: dict[str, Any] | None,
) -> tuple[str, str | None]:
    """Generate report + auto-update thesis via hybrid LLM router.

    Returns (report_text, thesis_update_text_or_None).
    Critical tasks route to Sonnet first, fallback to Haiku, then Gemini.
    If all fail: returns degraded raw data report + cached analysis.
    """
    # R-FUNDING-TRUTH (2026-06-15): fetch live 8h funding rates so the FULL
    # ANALYSIS LLM context carries the PRE-COMPUTED per-position funding verdict
    # (PAGA/RECIBE + carry-caro), the single source of truth shared with the
    # funding_por_posición block. Keyless HL endpoint; {} on failure (the verdict
    # then falls back to the realized-carry sign). NEVER breaks the report.
    _funding_rates: dict[str, Any] = {}
    try:
        from modules.funding_tracker import fetch_funding_rates
        _funding_rates = await fetch_funding_rates()
    except Exception:  # noqa: BLE001
        _funding_rates = {}

    user_content = compile_raw_data(
        portfolio, hyperlend, market, unlocks, telegram_intel,
        funding_rates=_funding_rates,
    )

    state = _load_thesis()
    prev_thesis = _thesis_context(state)
    # Fund-state block is injected at the TOP so the LLM sees it before any
    # stale prose below. Ground truth: HF thresholds, basket status, flywheel
    # pair trade design note. (R-NOPRELIQ + REMOVE BLOFIN 2026-05-15: Trade del
    # Ciclo Blofin ELIMINADO de los inyectables.)
    # R-BOT-TERMINOLOGY-UNIFY (2026-05-07): pass market so the LMEC
    # bear-invalidation block uses live BTC for condition #1 / #4.
    full_system = (await _full_state_block(market)) + SYSTEM_PROMPT + prev_thesis

    try:
        report_text, provider = await route_request(
            "reporte", full_system, user_content, max_tokens=8000,
        )

        if not report_text.strip():
            report_text = "(reporte vac\u00edo)"

        # ── R-REPORTE-LIVE (2026-06-03) FIX 3: header/body self-consistency ──
        # Drop body lines that contradict the current venue/state (a live
        # HyperLend HF when the flywheel is in PM) or that suggest a bearish
        # close on a CYCLE-ACCUMULATION leg. NEVER breaks the report.
        if os.getenv("REPORT_CONSISTENCY_ENABLED", "true").lower() == "true":
            try:
                from config import FLYWHEEL_DEPRECATED as _FLY_DEP_CONS
            except Exception:  # noqa: BLE001
                _FLY_DEP_CONS = True
            try:
                from modules.position_classifier import classify_portfolio, cycle_coins
                from modules.report_consistency import enforce_consistency
                _cycle = cycle_coins(classify_portfolio(portfolio, market))
                report_text, _dropped = enforce_consistency(
                    report_text,
                    flywheel_deprecated=_FLY_DEP_CONS,
                    cycle_coins=_cycle,
                )
                if _dropped:
                    log.info(
                        "report_consistency dropped %d contradicting line(s)",
                        len(_dropped),
                    )
            except Exception:  # noqa: BLE001
                log.exception("report_consistency pass failed (non-fatal)")

        # ── R-BOT-DEFINITIVE WI-8: fund hard-rules strike filter — remove any
        # line proposing to sell HYPE, repay debt with HYPE, reopen ZEC, or
        # close the basket on environment grounds. Logged, never fatal.
        try:
            from modules.fund_rules import strike_forbidden_lines
            report_text, _struck = strike_forbidden_lines(report_text)
            if _struck:
                log.info("fund_rules struck %d forbidden line(s)", len(_struck))
        except Exception:  # noqa: BLE001
            log.exception("fund_rules strike pass failed (non-fatal)")

        report_text += f"\n\n_An\u00e1lisis generado por: {provider}_"

        _save_last_analysis(report_text, provider)
        # Persist plain-text thesis snapshot BEFORE the LLM-JSON update, so
        # /tesis has something to show even if the structured update below
        # fails (JSON parse / LLMError). Root cause fix for 2026-04-22 bug.
        _save_tesis_latest(report_text, provider)

        thesis_update = await _update_thesis_state(report_text, user_content, market)

        return report_text, thesis_update

    except LLMError as e:
        log.warning("All LLM providers failed for report: %s", e)
        degraded = _build_degraded_report(
            portfolio, hyperlend, market, unlocks, telegram_intel,
            "all_providers_failed",
            "Verificar ANTHROPIC_API_KEY y GEMINI_API_KEY en Railway",
        )
        return degraded, None

    except Exception as exc:  # noqa: BLE001
        log.exception("Report generation failed (unexpected error)")
        degraded = _build_degraded_report(
            portfolio, hyperlend, market, unlocks, telegram_intel,
            f"unexpected_error: {exc}",
            "Revisar logs en Railway",
        )
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
    """Build a degraded report with raw data when all LLM providers fail."""
    lines = [
        "\u26a0\ufe0f AN\u00c1LISIS IA TEMPORALMENTE NO DISPONIBLE",
        f"Raz\u00f3n: {error_type}",
        f"Resolver: {resolution_url}",
        "",
        "El reporte abajo tiene la data cruda \u2014 revisar manualmente.",
        "",
        "\u2550" * 50,
        "",
    ]

    if portfolio:
        lines.append("PORTFOLIO:")
        for p in portfolio:
            if isinstance(p, dict) and p.get("status") == "ok":
                data = p.get("data", {})
                label = data.get("label", "?")
                eq = data.get("account_value", 0)
                upnl = data.get("unrealized_pnl_total", 0)
                lines.append(f"  \u2022 {label}: Equity ${eq:,.0f} | UPnL ${upnl:,.0f}")
        lines.append("")

    # R-PMCORE (2026-06-01): el flywheel HyperLend está CERRADO — el fondo
    # migró 100% a HyperLiquid Portfolio Margin. Avisar al LLM para que NO
    # razone sobre colateral/HF/deuda stale como posición viva.
    try:
        from config import FLYWHEEL_DEPRECATED as _FLY_DEP_AN
    except Exception:  # noqa: BLE001
        _FLY_DEP_AN = True
    if _FLY_DEP_AN:
        lines.append("HYPERLEND: CERRADO (flywheel migrado a Portfolio Margin).")
        lines.append(
            "  El core del fondo ahora es HYPE spot como colateral cross en "
            "Portfolio Margin (cuenta primaria 0xc7ae). Cualquier "
            "colateral/deuda de HyperLend (métricas legacy) es CACHE STALE de "
            "wallets cerradas — NO contar como posición viva ni en equity."
        )
        lines.append("")

    if hyperlend and isinstance(hyperlend, list) and not _FLY_DEP_AN:
        # R-HF-RENDER (3 may 2026): respect hf_status from cache-aware
        # reader so the LLM context never sees raw "HF nan" / "HF inf"
        # — render the same UNKNOWN fallback the user-facing /reporte uses.
        import math as _math
        lines.append("HYPERLEND:")
        for hl in hyperlend:
            if hl.get("status") != "ok":
                continue
            data = hl.get("data", {})
            label = data.get("label", "?")
            coll = data.get("total_collateral_usd", 0) or 0
            hf_status = (hl.get("hf_status") or "OK").upper()
            if hf_status == "UNKNOWN":
                last_hf = data.get("last_known_hf")
                age_s = data.get("age_seconds")
                age_label = (
                    f"{int(age_s)//60}min" if age_s is not None and age_s >= 60
                    else (f"{int(age_s)}s" if age_s is not None else "?")
                )
                if last_hf is None:
                    hf_repr = "UNKNOWN (RPC rate-limited, no cache)"
                elif isinstance(last_hf, str) and last_hf.lower() == "inf":
                    hf_repr = f"UNKNOWN (last known ∞ {age_label} ago)"
                else:
                    try:
                        hf_repr = f"UNKNOWN (last known {float(last_hf):.4f} {age_label} ago)"
                    except (TypeError, ValueError):
                        hf_repr = "UNKNOWN (cache parse error)"
                last_coll = data.get("last_known_collateral_usd") or coll
                lines.append(
                    f"  \u2022 {label}: Collateral ${last_coll:,.0f} | HF {hf_repr}"
                )
                continue
            hf = data.get("health_factor")
            try:
                if hf is None:
                    hf_str = "—"
                elif isinstance(hf, float) and _math.isnan(hf):
                    hf_str = "—"
                elif isinstance(hf, float) and _math.isinf(hf):
                    hf_str = "∞ (no debt)"
                else:
                    hf_str = f"{float(hf):.4f}"
            except (TypeError, ValueError):
                hf_str = "—"
            lines.append(f"  \u2022 {label}: Collateral ${coll:,.0f} | HF {hf_str}")
        lines.append("")

    if market and isinstance(market, dict) and market.get("status") == "ok":
        lines.append("MARKET DATA:")
        data = market.get("data", {})
        btc_px = data.get("BTC", {}).get("price", "?")
        hype_px = data.get("HYPE", {}).get("price", "?")
        lines.append(f"  \u2022 BTC: ${btc_px}")
        lines.append(f"  \u2022 HYPE: ${hype_px}")
        lines.append("")

    cached = _load_last_analysis()
    if cached:
        ts = cached.get("timestamp_utc", "?")[:16]
        provider = cached.get("provider", "unknown")
        hours_ago = "?"
        try:
            cached_dt = datetime.fromisoformat(cached.get("timestamp_utc", ""))
            now_dt = datetime.now(timezone.utc)
            hours_ago = int((now_dt - cached_dt.replace(tzinfo=timezone.utc)).total_seconds() / 3600)
        except Exception:
            pass

        lines.append("\u2550" * 50)
        lines.append(f"\U0001f4ce \u00daLTIMO AN\u00c1LISIS IA DISPONIBLE (hace {hours_ago}h, v\u00eda {provider})")
        lines.append(f"Timestamp: {ts}")
        lines.append("")
        lines.append(cached.get("report_text", ""))

    return "\n".join(lines)


async def generate_thesis_check(
    portfolio: list[dict[str, Any]] | None,
    hyperlend: dict[str, Any] | None,
    market: dict[str, Any] | None,
) -> str:
    """Thesis check — uses persistent state + fresh data via Sonnet."""
    user_content = compile_raw_data(portfolio, hyperlend, market, None, None)

    state = _load_thesis()
    prev_thesis = _thesis_context(state)
    full_prompt = (await _full_state_block(market)) + THESIS_PROMPT + prev_thesis

    try:
        text, provider = await route_request(
            "tesis", full_prompt, user_content, max_tokens=2000,
        )
        result = text.strip() if text else "(an\u00e1lisis vac\u00edo)"
        result += f"\n\n_Tesis generada por: {provider}_"
        # R-BOT-TERMINOLOGY-UNIFY (2026-05-07): append the LMEC bear
        # invalidation triggers block so /tesis always shows the 4
        # condition checks regardless of LLM verbosity.
        try:
            result = result + "\n\n" + format_lmec_block(
                evaluate_lmec_triggers(market)
            )
        except Exception:  # noqa: BLE001
            log.exception("LMEC block append failed for /tesis (non-fatal)")
        return result

    except LLMError:
        log.warning("All LLM providers failed for thesis check")
        if state.get("current"):
            return (
                "\u26a0\ufe0f Usando estado previo de la tesis (an\u00e1lisis en vivo no disponible):\n\n"
                f"{state['current']}"
            )
        return (
            "\u274c Todos los providers de IA fallaron. "
            "Verificar ANTHROPIC_API_KEY y GEMINI_API_KEY en Railway."
        )

    except Exception as exc:  # noqa: BLE001
        log.exception("Thesis check failed")
        return f"\u274c Error: {exc}"
