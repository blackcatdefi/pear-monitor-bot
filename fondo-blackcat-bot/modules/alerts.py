"""Periodic alert checks (HF, liquidation proximity, HYPE/BTC crashes).

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
from modules.hyperlend import fetch_all_hyperlend
from modules.portfolio import fetch_all_wallets, get_spot_price
from utils.telegram import send_bot_message

log = logging.getLogger(__name__)

STATE_FILE = os.path.join(DATA_DIR, "alert_state.json")


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
        short_addr = wallet_addr[:6] + "…" + wallet_addr[-4:] if wallet_addr else ""
        ident = f"{label} ({short_addr})" if label else short_addr
        wallet_key = wallet_addr[-8:] if wallet_addr else "unknown"
        if hf is not None and not math.isinf(hf):
            if hf < HF_CRITICAL:
                await _emit(bot, f"hf_critical_{wallet_key}", state, f"🚨 HYPERLEND HF CRÍTICO: {hf:.3f} — {ident} — acción inmediata!")
            else:
                _clear(state, f"hf_critical_{wallet_key}")
            if hf < HF_WARN:
                await _emit(bot, f"hf_warn_{wallet_key}", state, f"⚠️ HYPERLEND HF: {hf:.3f} — {ident} — por debajo de {HF_WARN}")
            else:
                _clear(state, f"hf_warn_{wallet_key}")
                _clear(state, f"hf_critical_{wallet_key}")

    # 2. HYPE price
    hype_px = await get_spot_price("HYPE")
    if hype_px is not None:
        if hype_px < HYPE_CRITICAL:
            await _emit(bot, "hype_critical", state, f"🔴 HYPE @ ${hype_px:.2f} — VERIFICAR HF INMEDIATAMENTE!")
        else:
            _clear(state, "hype_critical")
        if hype_px < HYPE_WARN:
            await _emit(bot, "hype_warn", state, f"🚨 HYPE @ ${hype_px:.2f} — impacto directo en colateral HyperLend")
        else:
            _clear(state, "hype_warn")
            _clear(state, "hype_critical")

    # 3. BTC crash
    btc_px = await get_spot_price("BTC")
    if btc_px is not None:
        if btc_px < BTC_WARN:
            await _emit(bot, "btc_warn", state, f"🚨 BTC @ ${btc_px:,.0f} — debajo de ${BTC_WARN:,.0f}, target ZordXBT $46K activo")
        else:
            _clear(state, "btc_warn")

    # 4. Liquidation proximity (per position)
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
            short_addr = d["wallet"][:6] + "…" + d["wallet"][-4:]
            key = f"liq_{d['wallet']}_{p['coin']}"
            if distance < LIQ_PROXIMITY_PCT:
                msg = (
                    f"⚠️ {p['coin']} {p['side']} en {d['label']} ({short_addr}) "
                    f"a {distance*100:.1f}% de liquidación (curr ${current:.4f} / liq ${liq_px:.4f})"
                )
                await _emit(bot, key, state, msg)
            else:
                _clear(state, key)

    _save_state(state)
