"""R-FULLANALYSIS-PM-TRUTH (2026-06-08) — single source of truth for the PM
math that the LLM FULL ANALYSIS narrative consumes.

The bug (live /reporte across 2026-06-07 / 2026-06-08 runs)
-----------------------------------------------------------
The bot has TWO places that talk about Portfolio Margin health:

  1. The DESTACADO panel + ``format_pm_state_telegram`` (modules.pm_panel).
     These were fixed in prior passes (R-PM-LIQ, R-PM-RATIO-RELABEL,
     R-PM-MARGIN-MODE-FIX) and are CORRECT. They lead with the aave-style
     Health Factor (~1.58), the real maintenance-LTV liq price (~$40.79),
     and label the borrow ratio as "Borrow utilization (vs 50% max-borrow)".

  2. The FULL ANALYSIS section (the long Spanish "REPORTE DIARIO" the LLM
     writes). The LLM received ONLY the raw portfolio JSON — no pre-computed
     PM block — so it re-derived PM health ON ITS OWN with the WRONG formula:
       * "aave-HF estimado: capacidad / deuda = $42,440 / $39,431 = 1.076"
         → that is borrow utilisation INVERTED, NOT the health factor. The
         real aave-HF uses the maintenance/liquidation threshold:
         ``aave_HF = Σ(value × liq_threshold) / debt`` with
         ``liq_threshold = 0.5 + 0.5 × ltv`` (0.75 for a 0.5-LTV HYPE) → ~1.58.
       * "Liq price HYPE: deuda / (qty × LTV 0.50) = $59.89" → that is the
         max-borrow line, NOT liquidation. Real liq uses 0.75 → ~$40.79.
     The narrative then screamed "ZONA DE RIESGO REAL / PRIORIDAD #1 repagar"
     while the panel correctly read 🟢 SALUDABLE. Contradiction.

The fix
-------
Stop letting the LLM do PM arithmetic. Compute the PMState ONCE (the SAME
``compute_pm_state`` the panel uses) and inject a pre-computed, authoritative
PM block into the LLM user content. The model is told to REPORT these values
VERBATIM and never recompute. Single source of truth → the panel and the
narrative always carry identical numbers.

Everything here is PARAM-DRIVEN from the PMState (``liq_threshold`` is the
data-derived ``0.5 + 0.5×ltv``; the max-borrow LTV is ``pm.max_ltv``). Nothing
is hardcoded. NEVER raises — a failure returns "" so /reporte is never broken.
"""
from __future__ import annotations

from typing import Any

# Phrases that are LIQUIDATION/urgency language. They are allowed ONLY when the
# aave-HF is genuinely in the danger zone (< HF_DANGER). When the position is
# healthy these must never leak into the narrative context.
_URGENT_FORBIDDEN_WHEN_HEALTHY = (
    "PRIORIDAD #1",
    "ALERTA CRÍTICA",
    "ZONA DE RIESGO REAL",
    "repagar urgente",
    "urgente",
)

# Narrative risk bands keyed to the SAME aave-HF the panel colours by
# (modules.pm_panel.headline_color / portfolio_margin.risk_tier):
#   ≥ 1.30 → healthy (green)         · no urgency, at most a max-borrow note
#   1.15 ≤ HF < 1.30 → caution       · monitor, no immediate action
#   < 1.15 → real-risk language OK   · reduce debt / add collateral
HF_HEALTHY_MIN = 1.30
HF_CAUTION_MIN = 1.15


def _f(v: Any) -> float:
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def select_primary_pm_state(
    wallets: list[dict[str, Any]] | None,
    market: dict[str, Any] | None = None,
):
    """Compute the PMState for the primary PM wallet — the SINGLE source.

    Mirrors ``templates.formatters._pm_health_for_header`` exactly (same wallet
    selection, same ltv map, same cross maintenance margin) so the LLM block and
    the DESTACADO panel consume an identical PMState. Returns ``None`` when the
    primary wallet / PM data is unavailable. NEVER raises.
    """
    try:
        from config import PM_PRIMARY_WALLET as _PMW
        from modules.portfolio_margin import compute_pm_state
    except Exception:  # noqa: BLE001
        return None
    try:
        pmw = (_PMW or "").lower()
        primary = None
        for w in wallets or []:
            if isinstance(w, dict) and w.get("status") == "ok":
                wd = w.get("data") or {}
                if (wd.get("wallet") or "").lower() == pmw:
                    primary = wd
                    break
        if primary is None:
            return None
        try:
            from modules.hl_borrow_lend import get_collateral_ltv_map
            _ltv = get_collateral_ltv_map()
        except Exception:  # noqa: BLE001
            _ltv = {}
        try:
            from templates.formatters import _build_price_map
            _prices = _build_price_map(market)
        except Exception:  # noqa: BLE001
            _prices = {}
        _cmm = _f(primary.get("cross_maintenance_margin_used"))
        pm = compute_pm_state(
            primary.get("spot_balances") or [],
            primary.get("positions") or [],
            _prices,
            open_orders=primary.get("open_orders") or [],
            ltv_map=_ltv,
            perp_cross_mm=_cmm,
        )
        if pm is None or not pm.has_data or pm.collateral_usd <= 0:
            return None
        return pm
    except Exception:  # noqa: BLE001
        return None


def _risk_directive(pm) -> str:
    """One-line narrative directive keyed to the aave-HF band (panel parity).

    Healthy → no urgency. Caution → monitor. Danger (< 1.15) → real-risk
    wording allowed. NEVER emits urgency language when the HF is healthy.
    """
    has_debt = pm.debt_usd > 1.0
    if not has_debt:
        return (
            "Sin deuda USDC/USDH abierta — el PM no tiene riesgo de "
            "liquidación. Reportá colateral y capacidad de borrow disponible."
        )
    hf = _f(pm.aave_hf)
    if hf >= HF_HEALTHY_MIN:
        return (
            f"aave-HF {hf:.2f} SALUDABLE (≥{HF_HEALTHY_MIN:.2f}): el PM está "
            "sano y lejos de la liquidación. NO uses lenguaje de alarma ni "
            "marques el repago de deuda como acción prioritaria; mantené el "
            "hedge y reportá el estado como saludable."
        )
    if hf >= HF_CAUTION_MIN:
        return (
            f"aave-HF {hf:.2f} en zona de OBSERVACIÓN "
            f"({HF_CAUTION_MIN:.2f}–{HF_HEALTHY_MIN:.2f}): monitorear la "
            "distancia a liquidación, sin acción inmediata de margen."
        )
    # hf < HF_CAUTION_MIN — real-risk language is warranted here.
    return (
        f"RIESGO REAL: aave-HF {hf:.2f} {pm.risk_label} (por debajo de "
        f"{HF_CAUTION_MIN:.2f}). La posición está cerca de la liquidación — "
        "considerá reducir deuda o agregar colateral HYPE."
    )


def build_pm_llm_block(pm) -> str:
    """Render the authoritative pre-computed PM block for the LLM context.

    The model must REPORT these values VERBATIM and NEVER recompute aave-HF,
    the liq price, or the borrow utilization. Missing values render as "n/d".
    Returns "" when there is no PM data (so empty/idle wallets inject nothing).
    NEVER raises. PARAM-DRIVEN: ``liq_threshold`` and ``max_ltv`` come from the
    PMState (data-derived), never hardcoded.
    """
    if pm is None or not getattr(pm, "has_data", False) or pm.collateral_usd <= 0:
        return ""
    try:
        from modules.pm_panel import borrow_utilization_status
    except Exception:  # noqa: BLE001
        def borrow_utilization_status(u: float):  # type: ignore[misc]
            if u < 90.0:
                return "borrow headroom OK", False
            if u <= 100.0:
                return "NEAR MAX-BORROW: limited new draws", False
            return "OVER MAX-BORROW: no new draws; reduce or add collateral", False

    has_debt = pm.debt_usd > 1.0
    util_pct = _f(pm.ratio) * 100.0
    util_label, _ = borrow_utilization_status(util_pct)
    max_borrow_pct = (_f(pm.max_ltv) or 0.50) * 100.0

    def _money(v: float) -> str:
        return f"${v:,.0f}"

    lines: list[str] = []
    lines.append(
        "═══════ PORTFOLIO MARGIN — VALORES PRE-CALCULADOS (AUTORITATIVO) ═══════"
    )
    lines.append(
        "Estos valores YA están calculados por el motor del fondo "
        "(modules.portfolio_margin.compute_pm_state, la MISMA fuente que el "
        "panel DESTACADO). REPORTALOS VERBATIM. PROHIBIDO recalcular o estimar "
        "aave-HF, liq price o borrow utilization por tu cuenta: no dividas la "
        "capacidad de borrow por la deuda para sacar el aave-HF (eso da la "
        "utilización invertida), ni la deuda por el notional de HYPE al LTV de "
        "borrow para sacar el liq price (esa es la línea de max-borrow, no la de "
        "liquidación). Si un campo dice 'n/d', escribí 'n/d', no lo derives."
    )
    lines.append("")

    # ── Core collateral / debt ──
    coll = pm.collateral_usd
    coll_line = f"• Colateral HYPE: {_money(coll)}"
    if pm.hype_qty > 0 and pm.hype_px > 0:
        coll_line += f"  ({pm.hype_qty:,.1f} HYPE × ${pm.hype_px:,.2f} oráculo)"
    lines.append(coll_line)
    lines.append(f"• Deuda (USDC/USDH borrowed): {_money(pm.debt_usd) if has_debt else '$0'}")

    # ── Borrow utilization axis (NOT liquidation) ──
    lines.append(
        f"• Borrow utilization (vs {max_borrow_pct:.0f}% max-borrow): "
        f"{util_pct:.1f}%  — {util_label}"
    )
    lines.append(
        f"• Head-room de borrow disponible: "
        f"{_money(pm.available_usd) if has_debt else _money(pm.available_usd)}"
    )

    # ── Real risk axis (aave-HF + maintenance liq price) ──
    if has_debt:
        lines.append(
            f"• aave-HF (riesgo real, liq-threshold {pm.liq_threshold:.2f} = "
            f"0.5 + 0.5×ltv): {pm.aave_hf:.2f} {pm.risk_emoji} {pm.risk_label}"
        )
        if pm.liq_price > 0:
            buf = (
                f" · buffer a liq {pm.price_buffer_pct:.1f}%"
                if pm.price_buffer_pct > 0 else ""
            )
            lines.append(
                f"• Liq. price HYPE (maint-LTV {pm.liq_threshold:.2f}): "
                f"${pm.liq_price:,.2f}{buf}"
            )
        else:
            lines.append("• Liq. price HYPE (maint-LTV): n/d")
    else:
        lines.append("• aave-HF: n/d (sin deuda — no hay riesgo de liquidación)")
        lines.append("• Liq. price HYPE: n/d (sin deuda)")

    # ── Cross vs isolated split (R-PM-MARGIN-MODE-FIX parity) ──
    iso_legs = getattr(pm, "isolated_positions", ()) or ()
    if pm.shorts_notional > 0:
        split = ""
        if pm.isolated_shorts_notional > 0:
            split = (
                f" [cross {_money(pm.cross_shorts_notional)} en pool · "
                f"isolated {_money(pm.isolated_shorts_notional)} walled-off]"
            )
        lines.append(
            f"• Hedge (shorts basket): {_money(pm.shorts_notional)} notional"
            f"{split}"
        )
    if iso_legs:
        lines.append(
            "• ISOLATED legs (margin walled-off — NO tocan el pool cross ni el "
            "liq price del HYPE; cada uno con su propio liq price):"
        )
        for leg in iso_legs:
            try:
                dist = (
                    f" · dist liq {leg.distance_to_liq_pct:.1f}%"
                    if leg.distance_to_liq_pct else ""
                )
                lp = f" · liq ${leg.liq_px:,.2f}" if leg.liq_px else ""
                lines.append(
                    f"    – {leg.side} {leg.coin}: ntl "
                    f"{_money(abs(leg.notional_usd))}{lp}{dist}"
                )
            except Exception:  # noqa: BLE001
                continue

    if pm.naked_long:
        lines.append(
            "• 🚨 NAKED-LONG: deuda abierta sin shorts del basket — long "
            "apalancado sin hedge (violación de regla dura). Alertar SIEMPRE."
        )

    # ── Narrative directive (band-keyed, panel-consistent) ──
    lines.append("")
    lines.append(f"DIRECTIVA DE RIESGO PM: {_risk_directive(pm)}")
    lines.append(
        "Recordatorio: borrow utilization es un eje SEPARADO del aave-HF. "
        ">100% = OVER MAX-BORROW (solo bloquea nuevos draws de USDC); 90–100% = "
        "NEAR MAX-BORROW (draws limitados). NUNCA pintes la borrow utilization "
        "con color de liquidación."
    )
    lines.append("═══════ FIN PORTFOLIO MARGIN PRE-CALCULADO ═══════")
    return "\n".join(lines)


def build_pm_llm_block_from_wallets(
    wallets: list[dict[str, Any]] | None,
    market: dict[str, Any] | None = None,
) -> str:
    """Convenience: select the primary PMState and render the LLM block.

    Returns "" when no PM data is available. NEVER raises.
    """
    try:
        pm = select_primary_pm_state(wallets, market)
        return build_pm_llm_block(pm)
    except Exception:  # noqa: BLE001
        return ""
