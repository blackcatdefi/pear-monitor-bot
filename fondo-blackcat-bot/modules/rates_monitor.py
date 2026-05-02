"""Round 17 — Edge-triggered rate alerts (UETH borrow APY, HF flywheel).

Llamado por scheduler cada 30 min. Por cada threshold cruzado, alerta MAX 1×
por día. Se persiste en data/rates_alert_state.json.

Thresholds:
    - UETH borrow APY: warn 6%, crit 10%
    - HF flywheel: warn 1.20, crit 1.10, emerg 1.05
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from config import DATA_DIR, TELEGRAM_CHAT_ID

log = logging.getLogger(__name__)

_STATE_FILE = os.path.join(DATA_DIR, "rates_alert_state.json")


def _load_state() -> dict:
    if not os.path.isfile(_STATE_FILE):
        return {}
    try:
        with open(_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        with open(_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        log.exception("rates_alert_state save failed")


async def scheduled_check(bot) -> int:
    """Returns number of alerts sent in this cycle."""
    if os.getenv("RATES_MONITOR_ENABLED", "true").strip().lower() == "false":
        return 0
    if not TELEGRAM_CHAT_ID:
        return 0

    from utils.telegram import send_bot_message
    from modules.hyperlend import fetch_reserve_rates, fetch_all_hyperlend

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state = _load_state()
    fired_today = set(state.get(today, []))
    sent = 0

    # 1. UETH borrow APY
    try:
        payload = await fetch_reserve_rates()
        if payload.get("status") == "ok":
            apy = None
            for sym, v in (payload.get("rates") or {}).items():
                if (sym or "").upper() in ("UETH", "WETH", "ETH"):
                    apy = float(v.get("apy_borrow") or 0.0) * 100
                    break
            if apy is not None:
                key_crit = "ueth_apy_crit_10"
                key_warn = "ueth_apy_warn_6"
                if apy > 10 and key_crit not in fired_today:
                    msg = (
                        f"🚨 UETH borrow APY = {apy:.2f}% > 10%\n"
                        "Critical threshold — flywheel cost unsustainable.\n"
                        "Consider repay / debt flip to stable."
                    )
                    await send_bot_message(bot, TELEGRAM_CHAT_ID, msg)
                    fired_today.add(key_crit)
                    sent += 1
                elif apy > 6 and key_warn not in fired_today and key_crit not in fired_today:
                    msg = (
                        f"⚠️ UETH borrow APY = {apy:.2f}% > 6%\n"
                        "Thesis threshold — monitor next few hours."
                    )
                    await send_bot_message(bot, TELEGRAM_CHAT_ID, msg)
                    fired_today.add(key_warn)
                    sent += 1
    except Exception:
        log.exception("rates_monitor UETH check failed")

    # 2. HF flywheel
    try:
        hl = await fetch_all_hyperlend()
        hf_min = None
        if isinstance(hl, list):
            for r in hl:
                if r.get("status") != "ok":
                    continue
                v = r["data"].get("health_factor")
                if v is None:
                    continue
                f = float(v)
                if hf_min is None or f < hf_min:
                    hf_min = f
        if hf_min is not None:
            key_emerg = "hf_emerg_105"
            key_crit = "hf_crit_110"
            key_warn = "hf_warn_120"
            if hf_min < 1.05 and key_emerg not in fired_today:
                msg = (
                    f"🚨🚨 HF EMERGENCY: {hf_min:.3f} < 1.05\n"
                    "LIQUIDATION IMMINENT.\n"
                    "Repay debt NOW or add collateral."
                )
                await send_bot_message(bot, TELEGRAM_CHAT_ID, msg)
                fired_today.add(key_emerg)
                sent += 1
            elif hf_min < 1.10 and key_crit not in fired_today:
                msg = (
                    f"🚨 HF CRITICAL: {hf_min:.3f} < 1.10\n"
                    "Operational action required."
                )
                await send_bot_message(bot, TELEGRAM_CHAT_ID, msg)
                fired_today.add(key_crit)
                sent += 1
            elif hf_min < 1.20 and key_warn not in fired_today:
                msg = (
                    f"⚠️ HF in monitor zone: {hf_min:.3f} < 1.20\n"
                    "Watch evolution."
                )
                await send_bot_message(bot, TELEGRAM_CHAT_ID, msg)
                fired_today.add(key_warn)
                sent += 1
    except Exception:
        log.exception("rates_monitor HF check failed")

    state[today] = list(fired_today)
    keys = sorted(state.keys())
    if len(keys) > 7:
        for k in keys[:-7]:
            state.pop(k, None)
    _save_state(state)

    if sent:
        log.info("rates_monitor dispatched %d alert(s)", sent)
    return sent
