"""Periodic alert checks (HF, liquidation proximity, HYPE/BTC crashes, Trade del Ciclo DCA).

Edge-triggered: keeps last alert state in data/alert_state.json to avoid spam.
"""
from __future__ import annotations

import json
import logging
import math
import os
from typing import Any

from config import (
    BTC_WARN,
    DATA_DIR,
    HF_CRITICAL,
    HF_WARN,
    HYPE_CRITICAL,
    HYPE_WARN,
    LIQ_PROXIMITY_PCT,
    TELEGRAM_CHAT_ID,
)
from modules.hyperlend import fetch_all_hyperlend, fetch_reserve_rates
from modules.portfolio import fetch_all_wallets, get_spot_price
from utils.telegram import send_bot_message

log = logging.getLogger(__name__)

STATE_FILE = os.path.join(DATA_DIR, "alert_state.json")

# Trade del Ciclo DCA levels
CYCLE_DCA_LEVELS = [
    {"price": 70_000, "label": "DCA Add 1", "margin": "$500"},
    {"price": 63_000, "label": "DCA Add 2", "margin": "$750"},
    {"price": 55_000, "label": "DCA Add 3", "margin": "$1,000"},
]
CYCLE_CRITICAL = 50_000  # Near liquidation zone
CYCLE_TP_ZONE = 150_000  # Take profit zone


def _load_state() -> dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not save alert state: %s", exc)


async def _emit(bot, key: str, state: dict[str, Any], message: str) -> None:
    if state.get(key):  # already alerted
        return
    log.warning("ALERT %s: %s", key, message)
    if TELEGRAM_CHAT_ID:
        await send_bot_message(bot, TELEGRAM_CHAT_ID, message)
    state[key] = True


def _clear(state: dict[str, Any], key: str) -> None:
    if key in state:
        state.pop(key)


async def run_alert_cycle(bot) -> None:  # noqa: C901
    state = _load_state()

    # 1. HyperLend HF (all wallets)
    hl_list = await fetch_all_hyperlend()
    for hl in hl_list:
        if hl.get("status") != "ok":
            continue
        hld = hl["data"]
        hf = hld.get("health_factor")
        label = hld.get("label", "")
        wallet_addr = hld.get("wallet", "")
        short_addr = wallet_addr[:6] + "\u2026" + wallet_addr[-4:] if wallet_addr else ""
        ident = f"{label} ({short_addr})" if label else short_addr
        wallet_key = wallet_addr[-8:] if wallet_addr else "unknown"

        if hf is not None and not math.isinf(hf):
            # Round to 4 decimals to avoid float noise
            hf_r = round(hf, 4)
            if hf_r < HF_CRITICAL:  # strict <, exact threshold does NOT alert
                await _emit(
                    bot, f"hf_critical_{wallet_key}", state,
                    f"\U0001f6a8 HYPERLEND HF CR\u00cdTICO: {hf_r:.4f} \u2014 {ident} \u2014 acci\u00f3n inmediata!"
                )
            else:
                _clear(state, f"hf_critical_{wallet_key}")

            if hf_r < HF_WARN:  # strict <, exact 1.20 does NOT alert
                await _emit(
                    bot, f"hf_warn_{wallet_key}", state,
                    f"\u26a0\ufe0f HYPERLEND HF: {hf_r:.4f} \u2014 {ident} \u2014 por debajo de {HF_WARN:.2f}"
                )
            else:
                _clear(state, f"hf_warn_{wallet_key}")
                _clear(state, f"hf_critical_{wallet_key}")

    # 2. HYPE price
    hype_px = await get_spot_price("HYPE")
    if hype_px is not None:
        if hype_px < HYPE_CRITICAL:
            await _emit(bot, "hype_critical", state,
                        f"\U0001f534 HYPE @ ${hype_px:.2f} \u2014 VERIFICAR HF INMEDIATAMENTE!")
        else:
            _clear(state, "hype_critical")

        if hype_px < HYPE_WARN:
            await _emit(bot, "hype_warn", state,
                        f"\U0001f6a8 HYPE @ ${hype_px:.2f} \u2014 impacto directo en colateral HyperLend")
        else:
            _clear(state, "hype_warn")
            _clear(state, "hype_critical")

    # 3. BTC crash (legacy)
    btc_px = await get_spot_price("BTC")
    if btc_px is not None:
        if btc_px < BTC_WARN:
            await _emit(bot, "btc_warn", state,
                        f"\U0001f6a8 BTC @ ${btc_px:,.0f} \u2014 debajo de ${BTC_WARN:,.0f}")
        else:
            _clear(state, "btc_warn")

    # 4. Trade del Ciclo — BTC DCA dip alerts
    if btc_px is not None:
        for level in CYCLE_DCA_LEVELS:
            key = f"cycle_dca_{level['price']}"
            if btc_px <= level["price"]:
                await _emit(
                    bot, key, state,
                    f"\U0001f4c9 Dip Alert Trade del Ciclo: BTC @ ${btc_px:,.0f} \u2014 "
                    f"activar {level['label']} ({level['margin']} margin)"
                )
            else:
                _clear(state, key)

        # Critical zone (near liquidation)
        if btc_px <= CYCLE_CRITICAL:
            await _emit(
                bot, "cycle_critical", state,
                f"\u26a0\ufe0f ZONA CR\u00cdTICA Trade del Ciclo: BTC @ ${btc_px:,.0f} \u2014 "
                f"HF del trade cerca de liquidaci\u00f3n!"
            )
        else:
            _clear(state, "cycle_critical")

        # TP zone
        if btc_px >= CYCLE_TP_ZONE:
            await _emit(
                bot, "cycle_tp", state,
                f"\U0001f3af TP Zone Trade del Ciclo: BTC @ ${btc_px:,.0f} \u2014 "
                f"evaluar cierre parcial"
            )
        else:
            _clear(state, "cycle_tp")

    # 5b. UETH borrow APY watchdog (Round 13)
    # El flywheel es insostenible si UETH borrow APY > 6%. A 10% empieza a
    # devorar profit: alerta crítica. Lectura on-chain via getReserveData().
    try:
        rates = await fetch_reserve_rates()
        if rates.get("status") == "ok":
            entry = (rates.get("rates") or {}).get("UETH")
            if not entry:
                for k, v in (rates.get("rates") or {}).items():
                    if k.lower() == "ueth":
                        entry = v
                        break
            if entry:
                apy = float(entry.get("apy_borrow") or 0.0)
                if apy >= 0.10:
                    await _emit(
                        bot, "ueth_borrow_critical", state,
                        f"\U0001f6a8 [FLYWHEEL] UETH borrow APY = {apy*100:.2f}% — "
                        "evaluar rotaci\u00f3n a stable o repay parcial inmediato "
                        "(threshold cr\u00edtico 10%)."
                    )
                else:
                    _clear(state, "ueth_borrow_critical")

                if apy >= 0.06:
                    await _emit(
                        bot, "ueth_borrow_warn", state,
                        f"\u26a0\ufe0f [FLYWHEEL] UETH borrow APY = {apy*100:.2f}% — "
                        "sobre threshold 6% de la tesis. Costo del pair trade "
                        "se hace insostenible si se mantiene."
                    )
                else:
                    _clear(state, "ueth_borrow_warn")
                    _clear(state, "ueth_borrow_critical")
    except Exception:  # noqa: BLE001
        log.exception("UETH borrow APY check failed (non-fatal)")

    # 6. Liquidation proximity (per position)
    wallets = await fetch_all_wallets()
    for w in wallets:
        if w.get("status") != "ok":
            continue
        d = w["data"]
        for p in d.get("positions") or []:
            liq_px = p.get("liq_px")
            entry = p.get("entry_px")
            if not liq_px or not entry or entry == 0:
                continue
            current = await get_spot_price(p["coin"]) or entry
            if current == 0:
                continue
            distance = abs(current - liq_px) / current
            short_addr = d["wallet"][:6] + "\u2026" + d["wallet"][-4:]
            key = f"liq_{d['wallet']}_{p['coin']}"
            if distance < LIQ_PROXIMITY_PCT:
                msg = (
                    f"\u26a0\ufe0f {p['coin']} {p['side']} en {d['label']} ({short_addr}) "
                    f"a {distance*100:.1f}% de liquidaci\u00f3n (curr ${current:.4f} / liq ${liq_px:.4f})"
                )
                await _emit(bot, key, state, msg)
            else:
                _clear(state, key)

    _save_state(state)
