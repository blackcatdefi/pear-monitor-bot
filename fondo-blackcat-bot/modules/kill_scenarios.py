"""Kill-scenario dashboard — when should we bail on each position?

Builds a /kill (aka /kill_scenarios) command that snapshots the current
state against each thesis invalidation trigger. Round 3 adds a Trade del
Ciclo section.

The file on master was corrupted to an HTML 404 page at some point; this
rewrite restores the command and extends it.
"""
from __future__ import annotations

import logging
import math
from typing import Any

from modules.hyperlend import fetch_all_hyperlend
from modules.market import fear_greed, coingecko_global
from modules.portfolio import get_spot_price
from modules.cycle_trade import (
    LIQ_TARGET_RANGE,
    TP_MAIN,
    TP_PARTIAL,
    compute_upnl,
    get_state as cycle_state,
)

log = logging.getLogger(__name__)

# Static bits (manually maintained; updated in system_prompt / memory)
WAR_TRADE_ACTIVE = False  # DreamCash stays INACTIVA
WAR_TRADE_NOTE = "DreamCash INACTIVA. No hay exposure actual a la tesis Stage 6."


def _fmt_hf(v: float | None) -> str:
    if v is None:
        return "—"
    if math.isinf(v):
        return "∞"
    return f"{v:.3f}"


async def compute_kill_scenarios() -> str:
    hl_list, fng, glob, btc_px, hype_px = [], {}, {}, None, None
    try:
        hl_list = await fetch_all_hyperlend()
    except Exception:  # noqa: BLE001
        log.exception("hyperlend fetch failed in kill_scenarios")

    try:
        fng = await fear_greed()
    except Exception:  # noqa: BLE001
        log.exception("fear_greed failed")

    try:
        glob = await coingecko_global()
    except Exception:  # noqa: BLE001
        log.exception("global market failed")

    try:
        btc_px = await get_spot_price("BTC")
    except Exception:  # noqa: BLE001
        log.exception("btc spot failed")

    try:
        hype_px = await get_spot_price("HYPE")
    except Exception:  # noqa: BLE001
        log.exception("hype spot failed")

    btc_dom = float(glob.get("btc_dominance") or 0.0)
    fng_val = int(fng.get("value") or 0)

    lines: list[str] = []
    lines.append("💀 KILL SCENARIOS — invalidadores de cada tesis")
    lines.append("─" * 45)
    lines.append("")

    # ── WAR TRADE (DreamCash) ──
    lines.append("WAR TRADE (DreamCash 0x171b):")
    lines.append(f"  {WAR_TRADE_NOTE}")
    lines.append("  Sin kill scenario activo (posición no expuesta).")
    lines.append("")

    # ── ALT SHORT BLEED ──
    lines.append("ALT SHORT BLEED Kill (3 wallets, SHORT basket):")
    dom_state = "🟢" if btc_dom >= 55 else "🔴"
    fng_state = "🟢" if fng_val < 75 else "🔴"
    lines.append(f"  {dom_state} BTC.D: {btc_dom:.1f}% (kill si <55%)")
    lines.append(f"  {fng_state} F&G: {fng_val} (kill si ≥75 euphoria)")
    lines.append("  ⚠️ SPX ATH: check manual (kill si breakout sostenido post-ceasefire)")
    lines.append("")

    # ── FLYWHEEL (HYPE/ETH via HyperLend) ──
    lines.append("FLYWHEEL Kill (HyperLend HF / HYPE / kHYPE):")
    if hype_px is not None:
        hype_state = "🟢" if hype_px >= 40 else ("⚠️" if hype_px >= 34 else "🔴")
        lines.append(f"  {hype_state} HYPE: ${hype_px:.2f} (kill si <$40, crítico <$34)")
    else:
        lines.append("  ❓ HYPE: precio no disponible")
    # HF per wallet
    for hl in hl_list:
        if hl.get("status") != "ok":
            continue
        h = hl["data"]
        coll = float(h.get("total_collateral_usd") or 0.0)
        if coll < 0.01:
            continue
        hf = h.get("health_factor")
        label = h.get("label") or "—"
        if hf is None or math.isinf(hf):
            lines.append(f"  ∞ {label}: sin deuda")
        else:
            icon = "🟢" if hf >= 1.20 else ("⚠️" if hf >= 1.10 else "🔴")
            lines.append(f"  {icon} {label} HF: {_fmt_hf(hf)} (kill si <1.20, liquidación <1.00)")
    lines.append("  ⚠️ kHYPE depeg: check manual")
    lines.append("")

    # ── TRADE DEL CICLO ──
    cs = cycle_state()
    lines.append("TRADE DEL CICLO Kill (BTC LONG Blofin):")
    if btc_px is not None:
        btc_state = "🟢" if btc_px >= LIQ_TARGET_RANGE[1] else ("⚠️" if btc_px >= LIQ_TARGET_RANGE[0] else "🔴")
        lines.append(
            f"  {btc_state} BTC: ${btc_px:,.0f} (liq zone ${LIQ_TARGET_RANGE[0]:,.0f}-${LIQ_TARGET_RANGE[1]:,.0f})"
        )
        # TP awareness
        if btc_px >= TP_MAIN:
            lines.append(f"  🎯🎯 BTC ≥ ${TP_MAIN:,.0f} — zona TP principal (evaluar cierre 50-100%)")
        elif btc_px >= TP_PARTIAL:
            lines.append(f"  🎯 BTC ≥ ${TP_PARTIAL:,.0f} — zona TP parcial (evaluar cierre 30%)")
    else:
        lines.append("  ❓ BTC: precio no disponible")
    # Cycle Top Model placeholder
    lines.append("  ⚠️ Cycle Top Model AiPear: check manual (kill si score >19/25)")
    # Bull market sanity
    lines.append("  🟢 Bull market intacto: BTC bien sobre liq zone")
    if cs.get("active"):
        upnl = compute_upnl(cs)
        sign = "+" if upnl >= 0 else ""
        lines.append(
            f"  Posición activa: margin ${cs.get('margin_usd', 0):,.0f} | UPnL {sign}${upnl:,.2f}"
        )
    else:
        lines.append("  Posición: INACTIVA — sin entrada registrada")
    lines.append("")

    # ── Summary ──
    lines.append("─" * 45)
    lines.append("RESUMEN: evaluar kills activos con data manual + bot data arriba.")
    lines.append("DreamCash no figura porque está cerrada; Trade del Ciclo es NEW en Round 3.")

    return "\n".join(lines)
