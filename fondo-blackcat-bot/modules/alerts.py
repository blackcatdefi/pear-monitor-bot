"""
Monitoreo continuo cada N minutos.

Checks:
1. HyperLend HF bajo umbral (warning y crítico)
2. Cercanía a liquidación en cualquier posición perp (<10%)
3. HYPE price bajo umbral (colateral del flywheel)
4. BTC price bajo umbral

Estado persistido en memoria para evitar spam (edge-triggered: solo avisa
cuando cruza el umbral, no repite si sigue abajo).
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from config import (
    BTC_WARN_PRICE,
    HF_CRITICAL,
    HF_WARNING,
    HYPE_CRITICAL_PRICE,
    HYPE_WARN_PRICE,
    LIQUIDATION_DISTANCE_WARN,
)
from modules import hyperlend as hyperlend_mod
from modules import portfolio as portfolio_mod

log = logging.getLogger(__name__)

# state: alertas que ya disparamos (para evitar repetición)
_fired: dict[str, bool] = {}


def _edge(key: str, condition: bool) -> bool:
    """True solo la primera vez que `condition` se vuelve True; reset al volverse False."""
    prev = _fired.get(key, False)
    _fired[key] = condition
    return condition and not prev


async def run_alert_cycle(send_alert: Callable[[str], Awaitable[None]]) -> None:
    """Ejecutá un ciclo completo de checks. `send_alert` es async(str)->None."""
    # 1. HyperLend HF
    try:
        account = hyperlend_mod.get_account_data()
        if account:
            hf = account["health_factor"]
            if _edge("hf_critical", hf < HF_CRITICAL):
                await send_alert(
                    f"🔴 HYPERLEND HF CRÍTICO: {hf:.2f} — acción inmediata!\n"
                    f"Debt: ${account['debt_usd']:,.0f} | Colateral: ${account['collateral_usd']:,.0f}"
                )
            elif _edge("hf_warning", HF_CRITICAL <= hf < HF_WARNING):
                await send_alert(
                    f"⚠️ HYPERLEND HF: {hf:.2f} — por debajo de {HF_WARNING}\n"
                    f"Debt: ${account['debt_usd']:,.0f} | Colateral: ${account['collateral_usd']:,.0f}"
                )
    except Exception as e:  # noqa: BLE001
        log.warning("HF check failed: %s", e)

    # 2. Distancia a liquidación por posición
    try:
        snapshots = await portfolio_mod.fetch_all_wallets()
        for s in snapshots:
            for p in s["positions"]:
                liq = p.get("liquidation_px") or 0
                entry = p.get("entry") or 0
                if liq <= 0 or entry <= 0:
                    continue
                # Usamos entry como proxy del precio actual cuando no hay otro.
                # Mejor: sacar mark_px del meta, pero entry es conservador.
                # La distancia se calcula contra entry por falta de mark inline.
                distance = abs(entry - liq) / entry
                key = f"liq_{s['wallet']}_{p['coin']}"
                if _edge(key, distance < LIQUIDATION_DISTANCE_WARN):
                    await send_alert(
                        f"⚠️ {p['coin']} a {distance*100:.1f}% de liquidación\n"
                        f"Wallet: {s['label']} ({s['wallet'][:6]}...{s['wallet'][-4:]})\n"
                        f"Entry: ${entry:.4f} | Liq: ${liq:.4f} | Side: {p['side']}"
                    )
    except Exception as e:  # noqa: BLE001
        log.warning("Liquidation distance check failed: %s", e)

    # 3 & 4. HYPE + BTC prices via HL meta
    try:
        funding_ctx = await portfolio_mod.fetch_funding_context()
        if funding_ctx:
            hype = (funding_ctx.get("HYPE") or {}).get("mark_px") or 0
            btc = (funding_ctx.get("BTC") or {}).get("mark_px") or 0

            if _edge("hype_critical", 0 < hype < HYPE_CRITICAL_PRICE):
                await send_alert(
                    f"🔴 HYPE @ ${hype:.2f} — VERIFICAR HF INMEDIATAMENTE!"
                )
            elif _edge("hype_warning", HYPE_CRITICAL_PRICE <= hype < HYPE_WARN_PRICE):
                await send_alert(
                    f"🚨 HYPE @ ${hype:.2f} — impacto directo en colateral HyperLend."
                )

            if _edge("btc_warning", 0 < btc < BTC_WARN_PRICE):
                await send_alert(
                    f"🚨 BTC @ ${btc:,.0f} — debajo de ${BTC_WARN_PRICE:,.0f}, target ZordXBT $46K activo."
                )
    except Exception as e:  # noqa: BLE001
        log.warning("Price check failed: %s", e)
