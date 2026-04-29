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
    btc_px = await get_spot_price("BTC")
    hype_px = await get_spot_price("HYPE")
    eth_px = await get_spot_price("ETH")

    # 1. Alt Short Bleed
    lines.append("1\ufe0f\u20e3 ALT SHORT BLEED (SHORT basket 3x)")
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

    # 4. Trade del Ciclo
    lines.append("4\ufe0f\u20e3 TRADE DEL CICLO (LONG BTC 3x)")
    if btc_px:
        lines.append(f"   BTC: ${btc_px:,.0f}")
    lines.append("   Kill scenario: Bear market confirmado, BTC < $50K sostenido")
    lines.append("   Triggers concretos:")
    lines.append("   \u2022 Cycle Top Model AiPear \u226519/30 signals")
    lines.append("   \u2022 BTC < $50K por >1 semana (zona cr\u00edtica)")
    lines.append("   \u2022 BTC < $45K \u2192 liquidaci\u00f3n mec\u00e1nica")
    lines.append("   NO es kill scenario:")
    lines.append("   \u2022 Pullbacks intraday/semanales")
    lines.append("   \u2022 Titulares geopol\u00edticos temporales")
    lines.append("   \u2022 Drawdowns <30% desde ATH")
    lines.append("   DCA plan activo:")
    lines.append("   \u2022 $70K \u2192 Add 1 ($500)")
    lines.append("   \u2022 $63K \u2192 Add 2 ($750)")
    lines.append("   \u2022 $55K \u2192 Add 3 ($1,000)")
    if btc_px:
        if btc_px > 70_000:
            lines.append(f"   Status: \u2705 Sano \u2014 ${btc_px:,.0f} por encima de todos los DCA levels")
        elif btc_px > 63_000:
            lines.append(f"   Status: \u26a0\ufe0f DCA Add 1 activado \u2014 BTC @ ${btc_px:,.0f}")
        elif btc_px > 55_000:
            lines.append(f"   Status: \u26a0\ufe0f DCA Add 2 activado \u2014 BTC @ ${btc_px:,.0f}")
        elif btc_px > 50_000:
            lines.append(f"   Status: \U0001f534 DCA Add 3 activado \u2014 cerca de zona cr\u00edtica")
        else:
            lines.append(f"   Status: \U0001f480 ZONA CR\u00cdTICA \u2014 BTC @ ${btc_px:,.0f}")
    lines.append("   Acci\u00f3n si kill: solo liquidaci\u00f3n mec\u00e1nica (SL = liq price)")
    lines.append("")

    # 5. Core DCA
    lines.append("5\ufe0f\u20e3 CORE DCA (kHYPE + PEAR spot)")
    lines.append("   Kill scenario: N/A (spot, sin leverage)")
    lines.append("   Monitored pero sin liq risk")

    return "\n".join(lines)
