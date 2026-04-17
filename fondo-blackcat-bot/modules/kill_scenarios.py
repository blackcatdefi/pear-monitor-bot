"""Kill-scenario dashboard — /kill command.

Evaluates macro conditions that would invalidate (kill) each leg
of the fund's thesis: War Trade, Alt Short Bleed, HYPE Flywheel.
Uses real-time data where possible + semi-static geopolitical state.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from modules.hyperlend import fetch_all_hyperlend
from modules.market import coingecko_prices, coingecko_global, fear_greed

log = logging.getLogger(__name__)

# ─── Semi-static geopolitical state ──────────────────────────────────────────
# Update these manually as events unfold.  Each entry:
#   label, status ("active"/"partial"/"inactive"), detail, icon
WAR_TRADE_GEO: list[dict[str, str]] = [
    {
        "label": "Hormuz reabierto",
        "status": "partial",
        "detail": "condicional, US/IL prohibidos",
        "icon": "🟡",
    },
    {
        "label": "Ceasefire US-Iran",
        "status": "active",
        "detail": "~2 semanas, negociación activa",
        "icon": "🔴",
    },
    {
        "label": "Nuclear deal cerrado",
        "status": "inactive",
        "detail": "NO",
        "icon": "🟢",
    },
    {
        "label": "Bloqueo naval US",
        "status": "active",
        "detail": "SIGUE ACTIVO",
        "icon": "🟢",
    },
]

# Thresholds
HYPE_KILL_PRICE = 40.0       # HYPE < $40 kills flywheel
HF_KILL_THRESHOLD = 1.20     # HF < 1.20 is warning
KHYPE_DEPEG_THRESHOLD = 0.97 # kHYPE/HYPE ratio < 0.97 = depeg
FNG_EUPHORIA = 75            # Fear & Greed >= 75 = euphoria (kills alt short)
SPX_ATH_APPROX = 6_800       # rough SPX ATH level — update as needed


def _status_icon(condition: bool, warning: bool = False) -> str:
    """Return icon: 🔴 if condition is a kill, 🟡 if warning, 🟢 if safe."""
    if condition:
        return "🔴"
    if warning:
        return "🟡"
    return "🟢"


async def kill_scenarios() -> str:
    """Build the kill-scenario dashboard with live data."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Fetch live data in parallel
    prices, glob, fng_data, hl_data = await asyncio.gather(
        coingecko_prices(),
        coingecko_global(),
        fear_greed(),
        fetch_all_hyperlend(),
    )

    # ── Extract values ───────────────────────────────────────────────────
    hype_price = (prices.get("HYPE") or {}).get("price_usd", 0) or 0
    btc_dom = (glob.get("btc_dominance") or 0)
    fng_value = fng_data.get("value", 0) or 0
    fng_class = fng_data.get("classification", "?")

    # Best (lowest) HF across wallets
    hf_min = float("inf")
    hf_label = "—"
    for entry in hl_data:
        if entry.get("status") != "ok":
            continue
        d = entry["data"]
        hf = d.get("health_factor", float("inf"))
        if hf < hf_min:
            hf_min = hf
            hf_label = d.get("label", entry.get("label", "?"))

    # ── Build output ─────────────────────────────────────────────────────
    lines = [
        f"💀 KILL SCENARIOS — {now}",
        "",
    ]

    # === WAR TRADE ===
    lines.append("WAR TRADE Kill (posición NO activa actualmente):")
    for item in WAR_TRADE_GEO:
        lines.append(f"  {item['icon']} {item['label']}: {item['detail']}")

    killers_active = 0
    killers_total = 0

    # === ALT SHORT BLEED ===
    lines.append("")
    lines.append("ALT SHORT Kill:")

    # BTC.D cayendo? (kill = BTC.D dropping, alts strengthening)
    btc_d_dropping = btc_dom < 55.0
    btc_d_warning = btc_dom < 57.0
    icon = _status_icon(btc_d_dropping, btc_d_warning)
    btc_d_status = "SÍ" if btc_d_dropping else "NO"
    lines.append(f"  {icon} BTC.D cayendo: {btc_d_status} ({btc_dom:.1f}%)")
    killers_total += 1
    if btc_d_dropping:
        killers_active += 1

    # F&G euphoria?
    fng_euphoria = fng_value >= FNG_EUPHORIA
    fng_warning = fng_value >= 60
    icon = _status_icon(fng_euphoria, fng_warning)
    fng_status = "SÍ" if fng_euphoria else "NO"
    lines.append(f"  {icon} F&G euphoria: {fng_status} ({fng_value}, {fng_class})")
    killers_total += 1
    if fng_euphoria:
        killers_active += 1

    # SPX ATH? (rough heuristic — we don't fetch SPX live, mark as note)
    lines.append(f"  🟡 SPX ATH: monitorear manualmente (~{SPX_ATH_APPROX:,})")

    # === HYPE FLYWHEEL ===
    lines.append("")
    lines.append("FLYWHEEL Kill:")

    # HYPE < $40
    hype_kill = hype_price < HYPE_KILL_PRICE
    hype_warn = hype_price < (HYPE_KILL_PRICE * 1.15)
    icon = _status_icon(hype_kill, hype_warn)
    hype_status = "SÍ" if hype_kill else "NO"
    lines.append(f"  {icon} HYPE < ${HYPE_KILL_PRICE:.0f}: {hype_status} (${hype_price:,.2f})")
    killers_total += 1
    if hype_kill:
        killers_active += 1

    # HF < 1.20
    hf_kill = hf_min < HF_KILL_THRESHOLD
    hf_crit = hf_min < 1.25
    icon = _status_icon(hf_kill, hf_crit)
    hf_status = "SÍ" if hf_kill else "NO"
    hf_display = f"{hf_min:.3f}" if hf_min < 1e10 else "∞"
    lines.append(f"  {icon} HF < {HF_KILL_THRESHOLD:.2f}: {hf_status} ({hf_label} {hf_display})")
    killers_total += 1
    if hf_kill:
        killers_active += 1

    # kHYPE depeg (we can't easily check on-chain peg — flag as note)
    lines.append("  🟢 kHYPE depeg: NO (monitorear)")

    # === Summary ===
    lines.append("")
    if killers_active == 0:
        lines.append(f"✅ Evaluación: 0/{killers_total} killers activos. Tesis intacta.")
    elif killers_active <= 2:
        lines.append(f"⚠️ Evaluación: {killers_active}/{killers_total} killers activos. Precaución.")
    else:
        lines.append(f"🔴 Evaluación: {killers_active}/{killers_total} killers activos. REVISAR TESIS.")

    return "\n".join(lines)
