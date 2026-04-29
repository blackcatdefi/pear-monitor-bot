"""Periodic alert checks (HF, liquidation proximity, HYPE/BTC crashes, Trade del Ciclo).

Edge-triggered: keeps last alert state in data/alert_state.json to avoid spam.
Round 3 changes (2026-04-19):
  - HF alert uses STRICT `<` comparator (exact 1.20 does NOT alert).
  - HF display uses 4-decimal precision so rounding artefacts (1.19999 → 1.200)
    no longer confuse the user.
  - `_emit` is called with the full (bot, key, state, message) signature everywhere.
  - Added Trade del Ciclo BTC trigger alerts (DCA entries + kill zone + TP zones).
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

# Trade del Ciclo thresholds (BTC price in USD)
CYCLE_DCA_ADD_1 = 70_000.0   # +$500 margin trigger
CYCLE_DCA_ADD_2 = 63_000.0   # +$750 margin trigger
CYCLE_DCA_ADD_3 = 55_000.0   # +$1000 margin trigger
CYCLE_LIQ_ZONE = 50_000.0    # critical evaluate trigger
CYCLE_TP_PARTIAL = 130_000.0 # evaluate 30% close
CYCLE_TP_MAIN = 150_000.0    # evaluate 50-100% close


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
    """Edge-triggered emit: only fires once per threshold crossing."""
    if state.get(key):  # already alerted, skip
        return
    log.warning("ALERT %s: %s", key, message)
    if TELEGRAM_CHAT_ID:
        try:
            await send_bot_message(bot, TELEGRAM_CHAT_ID, message)
        except Exception as exc:  # noqa: BLE001
            log.warning("send_bot_message failed for %s: %s", key, exc)
    state[key] = True


def _clear(state: dict[str, Any], key: str) -> None:
    if key in state:
        state.pop(key)


async def run_alert_cycle(bot) -> None:  # noqa: C901
    state = _load_state()

    # 1. HyperLend HF (all wallets) — STRICT < comparator, 4-decimal display
    hl_list = await fetch_all_hyperlend()
    for hl in hl_list:
        if hl.get("status") != "ok":
            continue
        hld = hl["data"]
        hf = hld.get("health_factor")
        label = hld.get("label", "")
        wallet_addr = hld.get("wallet", "")
        short_addr = (wallet_addr[:6] + "…" + wallet_addr[-4:]) if wallet_addr else ""
        ident = f"{label} ({short_addr})" if label else short_addr
        wallet_key = wallet_addr[-8:] if wallet_addr else "unknown"

        if hf is None or math.isinf(hf):
            continue

        # Round to 4 decimals to kill float noise. Strict `<`: exact 1.20 never alerts.
        hf_r = round(hf, 4)

        # Emergency critical (< HF_CRITICAL, default 1.10)
        crit_key = f"hf_critical_{wallet_key}"
        if hf_r < HF_CRITICAL:
            await _emit(
                bot, crit_key, state,
                f"🚨 HYPERLEND HF CRÍTICO: {hf_r:.4f} — {ident} — por debajo de {HF_CRITICAL:.2f} — acción inmediata!",
            )
        else:
            _clear(state, crit_key)

        # Warning (< HF_WARN, default 1.20)
        warn_key = f"hf_warn_{wallet_key}"
        if hf_r < HF_WARN:
            await _emit(
                bot, warn_key, state,
                f"⚠️ HYPERLEND HF: {hf_r:.4f} — {ident} — por debajo de {HF_WARN:.2f}",
            )
        else:
            _clear(state, warn_key)

    # 2. HYPE price — strict <
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

    # 3. BTC crash (generic warn from config)
    btc_px = await get_spot_price("BTC")
    if btc_px is not None:
        if btc_px < BTC_WARN:
            await _emit(
                bot, "btc_warn", state,
                f"🚨 BTC @ ${btc_px:,.0f} — debajo de ${BTC_WARN:,.0f}",
            )
        else:
            _clear(state, "btc_warn")

        # ── Trade del Ciclo — DCA / kill / TP triggers (edge-triggered) ──
        # Triggered when BTC price crosses each level; reset when price recovers above level.
        # We alert ONCE per crossing (edge-triggered via state keys).
        cycle_levels = [
            (CYCLE_DCA_ADD_1, "cycle_dca_add1", f"🔔 TRADE DEL CICLO: ADD 1 trigger — BTC @ ${btc_px:,.0f} tocó ${CYCLE_DCA_ADD_1:,.0f}. Agregar $500 margin en Blofin."),
            (CYCLE_DCA_ADD_2, "cycle_dca_add2", f"🔔 TRADE DEL CICLO: ADD 2 trigger — BTC @ ${btc_px:,.0f} tocó ${CYCLE_DCA_ADD_2:,.0f}. Agregar $750 margin."),
            (CYCLE_DCA_ADD_3, "cycle_dca_add3", f"🔔 TRADE DEL CICLO: ADD 3 trigger — BTC @ ${btc_px:,.0f} tocó ${CYCLE_DCA_ADD_3:,.0f}. Agregar $1000 margin."),
            (CYCLE_LIQ_ZONE, "cycle_liq_zone", f"⚠️⚠️ TRADE DEL CICLO: ZONA CRÍTICA — BTC @ ${btc_px:,.0f} ≤ ${CYCLE_LIQ_ZONE:,.0f}. Evaluar salvar posición (liq target $45-50K)."),
        ]
        for level_px, key, msg in cycle_levels:
            if btc_px < level_px:
                await _emit(bot, key, state, msg)
            else:
                _clear(state, key)

        # TP zones (triggers when BTC *crosses above*)
        tp_levels = [
            (CYCLE_TP_PARTIAL, "cycle_tp_partial", f"🎯 TRADE DEL CICLO: zona TP parcial — BTC @ ${btc_px:,.0f} ≥ ${CYCLE_TP_PARTIAL:,.0f}. Evaluar cierre 30%."),
            (CYCLE_TP_MAIN, "cycle_tp_main", f"🎯🎯 TRADE DEL CICLO: zona TP principal — BTC @ ${btc_px:,.0f} ≥ ${CYCLE_TP_MAIN:,.0f}. Evaluar cierre 50-100%."),
        ]
        for level_px, key, msg in tp_levels:
            if btc_px >= level_px:
                await _emit(bot, key, state, msg)
            else:
                _clear(state, key)

    # 4. Liquidation proximity (per position on Hyperliquid perps)
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
