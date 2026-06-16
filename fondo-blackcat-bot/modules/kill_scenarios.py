"""Kill scenarios — conditions that would invalidate each trade thesis."""

from __future__ import annotations

import logging
from typing import Any

from auto.fund_constants import FUND_DEFAULT_LEVERAGE
from modules.portfolio import get_spot_price

log = logging.getLogger(__name__)


async def compute_kill_scenarios() -> str:
    """Generate kill scenario analysis for all active positions."""
    lines: list[str] = []
    lines.append("\U0001f480 KILL SCENARIOS \u2014 FONDO BLACK CAT")
    lines.append("\u2500" * 40)
    lines.append("Condiciones que invalidar\u00edan cada tesis.\n")

    # Fetch live data
    hype_px = await get_spot_price("HYPE")
    eth_px = await get_spot_price("ETH")

    # 1. Super Basket Stage 6 (canonical name since 2026-05-07)
    # Leverage label = FUND_DEFAULT_LEVERAGE (referencia documental — BCD opera
    # 5x cross default permanente). El leverage REAL de cada leg se calcula
    # dinámicamente en /reporte y /posiciones desde notional/equity.
    lines.append(
        f"1\ufe0f\u20e3 SUPER BASKET STAGE 6 (SHORT basket {FUND_DEFAULT_LEVERAGE})"
    )
    lines.append("   Kill scenario: Ceasefire + dovish Fed \u2192 risk-on alt squeeze")
    lines.append("   Triggers concretos:")
    lines.append("   \u2022 Ceasefire confirmado Iran/Israel (no solo rumores)")
    lines.append("   \u2022 Fed pivot dovish (rate cut signal o QE)")
    lines.append("   \u2022 Alt season confirmation (BTC.D < 50% + alts +30%)")
    lines.append("   SL: individual por posici\u00f3n (liq price = 100% margin)")
    lines.append("   Acci\u00f3n si kill: cerrar basket completo, no esperar")
    lines.append("")

    # 2. War Trade (DreamCash)
    lines.append("2\ufe0f\u20e3 WAR TRADE (DreamCash 0x171b)")
    lines.append("   Estado: INACTIVA \u2014 sin posiciones")
    lines.append("   Kill scenario: N/A (no hay exposure)")
    lines.append("   Reabrir si: escalada confirmada + oil >$85 + gold >$3500")
    lines.append("")

    # 3. Portfolio Margin nativo (colateral HYPE / deuda USDC-USDH)
    # R-BOT-DEFINITIVE-KILLCLEAN (2026-06-15): el flywheel HyperLend pair-trade
    # (LONG HYPE colateral / SHORT UETH deuda) está MUERTO. El único riesgo de
    # liquidación vivo es el Portfolio Margin nativo: aave-HF, liq price y
    # utilización sobre el colateral HYPE. Fuente ÚNICA: compute_pm_state (la
    # MISMA del panel y del canal real-risk). NO se lee HyperLend.
    hf_text = "n/d"
    liq_text = ""
    has_pm_debt = False
    try:
        from modules.portfolio import fetch_all_wallets
        from modules.pm_context import select_primary_pm_state
        wallets = await fetch_all_wallets()
        pm = select_primary_pm_state(wallets, None)
        if pm is not None and pm.has_data and pm.debt_usd > 1.0 and pm.aave_hf > 0:
            has_pm_debt = True
            hf_text = f"{pm.aave_hf:.3f}"
            if pm.liq_price > 0:
                liq_text = f"   Liq price HYPE (maint): ${pm.liq_price:,.2f}"
    except Exception:  # noqa: BLE001
        log.exception("kill_scenarios: PM state unavailable")

    lines.append("3\ufe0f\u20e3 PORTFOLIO MARGIN (colateral HYPE / deuda USDC-USDH)")
    if has_pm_debt:
        lines.append(f"   aave-HF actual: {hf_text}")
        if liq_text:
            lines.append(liq_text)
    else:
        lines.append("   Sin deuda USDC/USDH abierta — no hay riesgo de liquidaci\u00f3n PM")
    if hype_px:
        lines.append(f"   HYPE: ${hype_px:.2f}")
    if eth_px:
        lines.append(f"   ETH: ${eth_px:.2f}")
    lines.append("   Kill scenario: HYPE crash con deuda USDC abierta")
    lines.append("   Triggers concretos:")
    lines.append("   \u2022 HYPE cae hacia el liq price (colateral cubre menos la deuda)")
    lines.append("   \u2022 aave-HF < 1.10 (zona acci\u00f3n)")
    lines.append(
        "   Acci\u00f3n si kill: cerrar patas GANADORAS del basket a USDC y "
        "repagar \u2014 NUNCA vender HYPE (playbook del fondo)"
    )
    lines.append("")

    # 4. Core DCA (renumerado tras eliminar Trade del Ciclo en R-NOPRELIQ + REMOVE BLOFIN 2026-05-15)
    lines.append("4\ufe0f\u20e3 CORE DCA (kHYPE + PEAR spot)")
    lines.append("   Kill scenario: N/A (spot, sin leverage)")
    lines.append("   Monitored pero sin liq risk")

    return "\n".join(lines)
