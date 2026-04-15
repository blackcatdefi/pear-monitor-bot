"""Periodic alert checks (HF, liquidations, HYPE price, BTC price).

Called every ALERT_INTERVAL_MINUTES by the APScheduler in bot.py.
Dedupes alerts so we don't spam the same condition every cycle.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from config import (
    BTC_PRICE_WARN,
    FUND_WALLETS,
    HF_CRITICAL,
    HF_WARN,
    HYPE_PRICE_CRITICAL,
    HYPE_PRICE_WARN,
    LIQUIDATION_PROXIMITY_WARN,
)
from modules import hyperlend, portfolio

log = logging.getLogger(__name__)

# Simple in-memory dedup: key -> last_fired_ts
_DEDUP: dict[str, float] = {}
DEDUP_WINDOW = 30 * 60  # 30 min: don't re-fire the same alert within 30min


def _should_fire(key: str) -> bool:
    now = time.time()
    last = _DEDUP.get(key, 0)
    if now - last < DEDUP_WINDOW:
        return False
    _DEDUP[key] = now
    return True


async def run_checks(send: Callable[[str], Awaitable[Any]]) -> None:
    """Run all alert checks. `send` is an async function that posts to Telegram."""
    try:
        # 1. HyperLend HF
        hl = await hyperlend.get_account_data()
        hf = hl.get("hf")
        if hf is not None and hf != float("inf"):
            if hf < HF_CRITICAL and _should_fire(f"hf:crit"):
                await send(f"🚨 HYPERLEND HF CRÍTICO: {hf:.3f} — acción inmediata! Liquidación en HF<1.0.")
            elif hf < HF_WARN and _should_fire(f"hf:warn"):
                await send(f"⚠️ HYPERLEND HF: {hf:.3f} — por debajo de {HF_WARN:.2f}. Monitoreando colateral kHYPE.")
    except Exception as e:  # noqa: BLE001
        log.warning("HF check failed: %s", e)

    try:
        snapshot = await portfolio.fetch_all_wallets()
        # 2. Liquidation proximity
        for w in snapshot["wallets"]:
            for p in w.get("positions") or []:
                liq = p.get("liq_px")
                mark = p.get("mark")
                if not liq or not mark or liq <= 0 or mark <= 0:
                    continue
                distance = abs(mark - liq) / mark
                if distance < LIQUIDATION_PROXIMITY_WARN:
                    key = f"liq:{w['wallet']}:{p['coin']}"
                    if _should_fire(key):
                        await send(
                            f"⚠️ {p['side']} {p['coin']} a {distance*100:.1f}% de liquidación "
                            f"({w['label']}: {w['wallet'][:6]}…) — mark ${mark:.4f} vs liq ${liq:.4f}"
                        )

        # 3. HYPE price monitoring
        hype = (snapshot.get("mids") or {}).get("HYPE")
        try:
            hype_f = float(hype) if hype is not None else None
        except (TypeError, ValueError):
            hype_f = None
        if hype_f is not None:
            if hype_f < HYPE_PRICE_CRITICAL and _should_fire("hype:crit"):
                await send(f"🔴 HYPE @ ${hype_f:.2f} — VERIFICAR HF INMEDIATAMENTE! Colateral del flywheel en riesgo.")
            elif hype_f < HYPE_PRICE_WARN and _should_fire("hype:warn"):
                await send(f"🚨 HYPE @ ${hype_f:.2f} — debajo de ${HYPE_PRICE_WARN:.0f}. Impacto directo en colateral HyperLend.")

        # 4. BTC crash monitoring
        btc = (snapshot.get("mids") or {}).get("BTC")
        try:
            btc_f = float(btc) if btc is not None else None
        except (TypeError, ValueError):
            btc_f = None
        if btc_f is not None and btc_f < BTC_PRICE_WARN and _should_fire("btc:warn"):
            await send(f"🚨 BTC @ ${btc_f:,.0f} — debajo de ${BTC_PRICE_WARN:,.0f}. Target ZordXBT $46K activo.")
    except Exception as e:  # noqa: BLE001
        log.warning("portfolio checks failed: %s", e)
