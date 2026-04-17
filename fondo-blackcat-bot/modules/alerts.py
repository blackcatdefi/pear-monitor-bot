"""Periodic alert checks (HF, liquidation proximity, HYPE/BTC crashes).

Edge-triggered: keeps last alert state in data/alert_state.json to avoid spam.
HF alerts use escalated thresholds with cooldown + delta logic.
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

# ── Edge-triggered HF alert state (in-memory, persists via alert_state.json) ──
# Structure: { wallet_key: { "last_hf": float, "last_ts": float, "last_level": str } }
_HF_ALERT_KEY = "hf_edge_state"

# Escalated thresholds: (threshold, level_name, emoji)
_HF_THRESHOLDS = [
    (1.05, "emergency", "\U0001f480"),    # 💀
    (1.10, "critical", "\U0001f534\U0001f534"),  # 🔴🔴
    (1.15, "strong", "\U0001f534"),        # 🔴
    (1.20, "warning", "\u26a0\ufe0f"),     # ⚠️
]

_HF_DELTA = 0.02          # re-alert only if HF changed by this much
_HF_COOLDOWN = 7200       # 2 hours in seconds


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

7��2FVb�V֗B�&�B��W��7G"�7FFS�F�7E�7G"�����W76vS�7G"������S���b7FFR�vWB��W���2�&VG��W'FV@�&WGW&���r�v&��r�$�U%BW3�W2"��W���W76vR���bDT�Tu$��4�E��C��v�B6V�E�&�E��W76vR�&�B�DT�Tu$��4�E��B��W76vR��7FFU��W���G'VP���7��2FVb�V֗E�f�&6R�&�B��W76vS�7G"������S��""%6V�B�W'BV�6��F�F����ǒ�W6VB'�VFvR�G&�vvW&VB�b��"" ���r�v&��r�$�U%B�b�TDtS�W2"��W76vR���bDT�Tu$��4�E��C��v�B6V�E�&�E��W76vR�&�B�DT�Tu$��4�E��B��W76vR����FVb�6�V"�7FFS�F�7E�7G"�����W��7G"������S���b�W���7FFS��7FFR����W�����FVb�vWE��e��WfV�c�f��B���GW�U�7G"�7G%�����S��""%&WGW&���WfV����R�V�����f�"F�Rv�'7BF�&W6���B'&V6�VB��"���R�"" �f�"F�&W6���B��WfV��V�������e�D�$U4���E3���b�b�F�&W6���C��&WGW&���WfV��V������&WGW&����P����幌�����}�����}��}�������а�݅����}������Ȱ���聙���а���������Ȱ��хє聑��Ф����9����(������������ɥ���ɕ��!������聙�ɕ́���ѡɕ͡�����ɽ�ͥ����ݥѠ�������ݸ������ф����(������܀�ѥ���ѥ����(������}�хє聑��Ѐ��хє�͕ё���ձС}!}1IQ}-d�����(�����ɕ؀􁡙}�хє���С݅����}��䰁���(�����ɕ�}�����ɕع��Р�����}����(�����ɕ�}�̀��ɕع��Р�����}�̈����(�����ɕ�}��ٕ����ɕع��Р�����}��ٕ�������((������ٕ�}������}���}��}��ٕ�����(�������ɕ��}��ٕ��􁱕ٕ�}����l�t������ٕ�}�������͔���(����������􁱕ٕ�}����l�t������ٕ�}�������͔���((���������Ё��ٕ�}�����(����������!��́����ѡ䀠��ĸ�����P�����ȁ�хє�ͼ����Ё�ɽ�́�ɥ����́�ɕ͠(�����������݅����}��䁥����}�хє�(������������������}�хѕm݅����}���t(��������ɕ��ɸ((�������ѕɵ�������ݔ�͡�ձ����ɔ(����͡�ձ�}��ɔ����͔(����ɕ�ͽ��􀈈((��������ɕ�}����́9����(�������������Ёѥ����ɽ�ͥ�������܁��ѡɕ͡���(��������͡�ձ�}��ɔ��Q�Ք(��������ɕ�ͽ���ѡɕ͡�����ɽ�͕��(������������ɕ��}��ٕ�����ɕ�}��ٕ��(����������͍���ѕ��Ѽ���ݽ�͔���ٕ����ȁ����͍���ѕ��(��������͡�ձ�}��ɔ��Q�Ք(��������ɕ�ͽ��􁘉��ٕ������������ɕ�}��ٕ���I���ɕ��}��ٕ��(�����������̡������ɕ�}������}!}1Q�(����������!���ٕ��ͥ��������ѱ�ͥ�������Ё�����(��������͡�ձ�}��ɔ��Q�Ք(��������ɕ�ͽ��􁘉���ф�텉̡������ɕ�}����͙����}!}1Q�(������������܀���ɕ�}�̤���}!}
==1=]8�(����������
�����ݸ�����ɕ���ɔ�����Ё�Ёͅ�����ٕ�(��������͡�ձ�}��ɔ��Q�Ք(��������ɕ�ͽ��􀉍�����ݸ�����ɕ��((�������͡�ձ�}��ɔ�(����������ٕ�}���������ɕ��}��ٕ������Ƞ�(���������͜�􁘉핵����!eAI19�!����ٕ�}�����������͙�P��������m�ɕ�ͽ��t�(���������݅�Ё}����}��ɍ����а��͜�(����������}�хѕm݅����}���t���(�����������������}���聡��(�����������������}�̈聹�ܰ(�����������������}��ٕ��聍��ɕ��}��ٕ��(���������((async def run_alert_cycle(bot) -> None:  # noqa: C901
    state = _load_state()

    # 1. HyperLend HF (all wallets) — edge-triggered
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
            await _check_hf_edge(bot, wallet_key, hf, ident, state)

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
