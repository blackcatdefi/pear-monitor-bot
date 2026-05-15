"""Kill scenarios — conditions that would invalidate each trade thesis."""

from __future__ import annotations

import logging
import math
from typing import Any

from modules.hyperlend import fetch_all_hyperlend
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
    lines.append("1\ufe0f\u20e3 SUPER BASKET STAGE 6 (SHORT basket 3x)")
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

    # 3. HyperLend Flywheel
    hf_text = "?"
    hl_list = await fetch_all_hyperlend()
    for hl in hl_list:
        if hl.get("status") == "ok":
            hf = hl["data"].get("health_factor")
            if hf is not None:
                hf_text = f"{hf:.3f}" if not math.isinf(hf) else "\u221e"
                break

    lines.append("3\ufe0f\u20e3 HYPERLEND FLYWHEEL (LONG HYPE / SHORT ETH)")
    lines.append(f"   HF actual: {hf_text}")
    if hype_px:
        lines.append(f"   HYPE: ${hype_px:.2f}")
    if eth_px:
        lines.append(f"   ETH: ${eth_px:.2f}")
    lines.append("   Kill scenario: HYPE crash + ETH pump simult\u00e1neo")
    lines.append("   Triggers concretos:")
    lines.append("   \u2022 HYPE < $30 (colateral colapsa)")
    lines.append("   \u2022 ETH > $4000 mientras HYPE cae (deuda sube)")
    lines.append("   \u2022 HF < 1.10 sin capacidad de repay")
    lines.append("   Acci\u00f3n si kill: repay deuda UETH parcial, no cerrar todo")
    lines.append("")

    # 4. Core DCA (renumerado tras eliminar Trade del Ciclo en R-NOPRELIQ + REMOVE BLOFIN 2026-05-15)
    lines.append("4\ufe0f\u20e3 CORE DCA (kHYPE + PEAR spot)")
    lines.append("   Kill scenario: N/A (spot, sin leverage)")
    lines.append("   Monitored pero sin liq risk")

    return "\n".join(lines)
