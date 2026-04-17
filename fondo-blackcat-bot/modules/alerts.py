"""Periodic alert checks (HF, liquidation proximity, HYPE/BTC crashes).

Edge-triggered HF alerts: only fire when crossing thresholds or after
significant value change / 2-hour passive reminder.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
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

# ── HF edge-triggered thresholds ──
HF_THRESHOLDS = [
    (1.05, "EMERGENCY", "🆘"),
    (1.10, "CRITICAL", "🚨"),
    (1.15, "STRONG", "🔴"),
    (1.20, "WARNING", "⚠️"),
]

# State for edge-triggered HF alerts:
# {wallet_addr: {"value": float, "timestamp": float, "threshold_crossed": float}}
_last_hf_alerts: dict[str, dict] = {}

HF_CHANGE_THRESHOLD = 0.02   # re-alert if HF changed by this much
HF_PASSIVE_REMINDER_S = 7200  # 2 hours passive reminder


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


def _get_hf_threshold_level(hf: float) -> tuple[float, str, str] | None:
    """Return the highest (most severe) threshold that HF is below."""
    for threshold, label, emoji in HF_THRESHOLDS:
        if hf < threshold:
            return (threshold, label, emoji)
    return None


def _should_alert_hf(wallet_addr: str, hf: float) -> tuple[bool, str]:
    """Edge-triggered HF alert logic.

    Returns (should_alert, reason).
    Alert ONLY when:
    1. HF crosses a threshold for the first time (from above to below)
    2. HF changed by more than 0.02 since last alert
    3. More than 2 hours since last alert (passive reminder)
    """
    now = time.time()
    last = _last_hf_alerts.get(wallet_addr)
    current_threshold = _get_hf_threshold_level(hf)

    if current_threshold is None:
        # HF is healthy (above all thresholds) — clear state
        if wallet_addr in _last_hf_alerts:
            del _last_hf_alerts[wallet_addr]
        return False, ""

    threshold_val, threshold_label, _ = current_threshold

    if last is None:
        # First time crossing ANY threshold — alert
        return True, f"threshold {threshold_label} crossed (HF < {threshold_val})"

    last_value = last.get("value", 0)
    last_time = last.get("timestamp", 0)
    last_threshold = last.get("threshold_crossed", 999)

    # 1. New (more severe) threshold crossed
    if threshold_val < last_threshold:
        return True, f"NEW threshold {threshold_label} crossed (HF < {threshold_val})"

    # 2. HF changed significantly since last alert
    hf_delta = abs(hf - last_value)
    if hf_delta >= HF_CHANGE_THRESHOLD:
        direction = "↓" if hf < last_value else "↑"
        return True, f"HF moved {direction} by {hf_delta:.3f} since last alert"

    # 3. Passive reminder after 2 hours
    elapsed = now - last_time
    if elapsed >= HF_PASSIVE_REMINDER_S:
        hours = elapsed / 3600
        return True, f"passive reminder ({hours:.1f}h since last alert)"

    return False, ""


def _record_hf_alert(wallet_addr: str, hf: float) -> None:
    """Record that an HF alert was sent."""
    threshold = _get_hf_threshold_level(hf)
    _last_hf_alerts[wallet_addr] = {
        "value": hf,
        "timestamp": time.time(),
        "threshold_crossed": threshold[0] if threshold else 999,
    }


async def run_alert_cycle(bot) -> None:  # noqa: C901
    state = _load_state()

    # 1. HyperLend HF (all wallets) — EDGE-TRIGGERED
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

        if hf is not None and not math.isinf(hf):
            should_alert, reason = _should_alert_hf(wallet_addr, hf)

            if should_alert:
                threshold = _get_hf_threshold_level(hf)
                if threshold:
                    _, level_label, emoji = threshold
                    msg = (
                        f"{emoji} HYPERLEND HF {level_label}: {hf:.3f} — {ident}\n"
                        f"Razón: {reason}"
                    )
                    log.warning("HF ALERT %s: %s (reason: %s)", ident, hf, reason)
                    if TELEGRAM_CHAT_ID:
                        await send_bot_message(bot, TELEGRAM_CHAT_ID, msg)
                    _record_hf_alert(wallet_addr, hf)
            elif hf >= 1.20:
                # HF recovered above all thresholds — clear state
                if wallet_addr in _last_hf_alerts:
                    log.info("HF recovered for %s: %.3f", ident, hf)
                    del _last_hf_alerts[wallet_addr]

    # 2. HYPE price
    hype_px = await get_spot_price("HYPE")
    if hype_px is not None:
        if hype_px < HYPE_CRITICAL:
            await _emit(bot, "hype_critical", state,
                        f"🔴 HYPE @ ${hype_px:.2f} — VERIFICAR HF INMEDIATAMENTE!")
        else:
            _clear(state, "hype_critical")

        if hype_px < HYPE_WARN:
            await _emit(bot, "hype_warn", state,
                        f"🚨 HYPE @ ${hype_px:.2f} — impacto directo en colateral HyperLend")
        else:
            _clear(state, "hype_warn")
            _clear(state, "hype_critical")

    # 3. BTC crash
    btc_px = await get_spot_price("BTC")
    if btc_px is not None:
        if btc_px < BTC_WARN:
            await _emit(bot, "btc_warn", state,
                        f"🚨 BTC @ ${btc_px:,.0f} — debajo de ${BTC_WARN:,.0f}, target ZordXBT $46K activo")
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
