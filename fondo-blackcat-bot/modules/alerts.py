"""Periodic alert checks (HF flywheel, HYPE/BTC crashes, margin stress).

Edge-triggered: keeps last alert state in data/alert_state.json to avoid spam.

R-NOPRELIQ (2026-05-15): basket per-leg pre-liq alerts ELIMINADOS.
BCD pone SL/TP 100% nativo en HL por pata, no necesita pre-liq alerts en basket.
Flywheel HF gates (1.10/1.05/1.02) y MARGIN_STRESS edge 90% wallet-level
permanecen como red de seguridad.

R-SIGNAL-DIET (2026-07-20): [DCA ALERT] price-zone pushes ELIMINADOS por
completo (BTC/ETH/HYPE). BCD ve esos niveles en sus charts; el bot empujaba
ruido que ahogaba la única alerta que importa (riesgo de liquidación PM).
``BCD_DCA_PLAN`` sigue vivo como dato interno (/dca on-demand + LLM prompt) —
CERO pushes de Telegram derivados de él.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from config import (
    BTC_WARN,
    DATA_DIR,
    HYPE_CRITICAL,
    HYPE_WARN,
    TELEGRAM_CHAT_ID,
)
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

    # 1. (Removido R-BOT-DEFINITIVE-KILLCLEAN 2026-06-15) HyperLend HF watchdog.
    # El fondo NO usa HyperLend (protocolo Aave-fork muerto). El único riesgo de
    # liquidación vivo es el aave-HF del Portfolio Margin nativo sobre el
    # colateral HYPE, y ese canal ya lo cubre modules.alerts_margin (REAL-RISK:
    # aave-HF 1.30/1.20/1.10 + liq distance por pata, alimentado SOLO por
    # compute_pm_state). Leer HyperLend acá era una fuente muerta duplicada.

    # 2. HYPE price
    hype_px = await get_spot_price("HYPE")
    if hype_px is not None:
        if hype_px < HYPE_CRITICAL:
            await _emit(bot, "hype_critical", state,
                        f"\U0001f534 HYPE @ ${hype_px:.2f} \u2014 CHECK HF IMMEDIATELY!")
        else:
            _clear(state, "hype_critical")

        if hype_px < HYPE_WARN:
            await _emit(bot, "hype_warn", state,
                        f"\U0001f6a8 HYPE @ ${hype_px:.2f} \u2014 direct impact on Portfolio Margin HYPE collateral")
        else:
            _clear(state, "hype_warn")
            _clear(state, "hype_critical")

    # 3. BTC crash (legacy)
    btc_px = await get_spot_price("BTC")
    if btc_px is not None:
        if btc_px < BTC_WARN:
            await _emit(bot, "btc_warn", state,
                        f"\U0001f6a8 BTC @ ${btc_px:,.0f} \u2014 below ${BTC_WARN:,.0f}")
        else:
            _clear(state, "btc_warn")

    # 4. (Removido R-NOPRELIQ 2026-05-15) Trade del Ciclo DCA alerts —
    # Blofin trade vehicle eliminado del fondo, sin alertas asociadas.

    # 5b. (Removido R-BOT-DEFINITIVE-KILLCLEAN 2026-06-15) UETH borrow APY
    # watchdog. El flywheel HyperLend pair-trade (LONG HYPE colateral / SHORT
    # UETH deuda) ESTÁ MUERTO: ninguna posición viva mapea a él. Esta alerta
    # disparaba "[FLYWHEEL] UETH borrow APY = X%" con sugerencias de rotación a
    # stable / repay parcial que CONTRADICEN el mandato actual (prohibido
    # repagar deuda). Eliminada junto con la fuente fetch_reserve_rates.

    # 6. (Removido R-NOPRELIQ 2026-05-15) Per-leg basket pre-liquidation alerts.
    # BCD pone SL/TP 100% nativo en HL por pata, no necesita pre-liq alerts en basket.
    # Flywheel HF gates (1.10/1.05/1.02) y MARGIN_STRESS edge 90% wallet-level
    # quedan como red de seguridad de fondo.
    wallets = await fetch_all_wallets()

    # 6b. R-NOISE-CUT (2026-06-16) — margin alerting is now REAL-RISK ONLY.
    # The MARGIN STRESS channel (perp cross margin used vs cross equity) is
    # REMOVED from paging: under the fund's unified Portfolio Margin the perp
    # cross sub-account rests at ~100% utilization by construction (thin perp
    # equity; HYPE spot collateral cross-margins everything), so it carried no
    # actionable risk and fired every few hours with nothing to act on. Its one
    # real datum (≥100% blocks opening NEW perp legs) moved to the /reporte PM
    # panel as an informational line — never a push.
    #
    # ``run_margin_alerts`` now contains ONLY the real-risk channel — PM aave-HF
    # crossings 1.30/1.20/1.10 + any single position liq distance <12% / <8%,
    # fed by compute_pm_state + live position data. It runs UNCONDITIONALLY
    # (safety): it is no longer gated by the MARGIN_STRESS_ALERT_ENABLED flag,
    # which only ever governed the now-removed noise channel.
    try:
        import time as _time
        from modules.alerts_margin import run_margin_alerts
        _t0 = _time.monotonic()
        _sent = await run_margin_alerts(bot, wallets)
        # R-RISK-VALIDATOR-HOTFIX: liveness proof for the REAL-risk channel
        # (PM aave-HF bands 1.30/1.20/1.10 + per-position liq distance
        # <12%/<8%) — one INFO line per successful execution so health is
        # verifiable in Railway logs.
        log.info(
            "real_risk_channel OK — duration=%.2fs alerts_sent=%d",
            _time.monotonic() - _t0,
            int(_sent or 0),
        )
    except Exception:  # noqa: BLE001
        log.exception("real-risk margin alerts failed (non-fatal)")

    # 6b-bis. R-BOT-DEFINITIVE WI-5/WI-6 — SL reachability + trailing rule.
    try:
        from modules.sl_validator import run_sl_reachability_alerts
        await run_sl_reachability_alerts(bot, wallets)
    except Exception:  # noqa: BLE001
        log.exception("SL reachability alerts failed (non-fatal)")
    try:
        from modules.trailing_monitor import run_trailing_alerts
        await run_trailing_alerts(bot, wallets)
    except Exception:  # noqa: BLE001
        log.exception("trailing rule alerts failed (non-fatal)")

    # 7. (Removido R-SIGNAL-DIET 2026-07-20) BCD DCA zone watchdog (Round 13).
    # Los [DCA ALERT] de zonas de precio (BTC/ETH/HYPE) empujaban ruido nightly
    # que BCD ya ve en sus charts. Eliminado el push scheduler COMPLETO — el
    # plan DCA sigue disponible on-demand vía /dca y en el prompt del LLM.

    _save_state(state)


# ─── R-ONDEMAND: Margin stress watchdog ────────────────────────────────────


def margin_stress_ratio(account_value: float, total_margin_used: float) -> float | None:
    """Pure helper used by the alert path and by tests.

    Returns ``total_margin_used / account_value`` as a float in [0, +inf), or
    ``None`` when the wallet has no perp equity (empty wallet, idle wallet,
    or pre-fund-ready). Returning None instead of 0 lets the caller skip
    the wallet without generating false-positive 0% alerts.
    """
    try:
        eq = float(account_value)
        used = float(total_margin_used)
    except (TypeError, ValueError):
        return None
    if eq <= 0.0:
        # No perp equity → nothing to stress over. Includes the common
        # "empty wallet" case where Hyperliquid returns accountValue=0.
        return None
    return used / eq


async def _run_margin_stress_alerts(
    bot,
    state: dict[str, Any],
    wallets: list[dict[str, Any]],
    *,
    threshold_pct: float,
) -> None:
    """Edge-triggered margin-stress alert per wallet.

    Fires once when ratio crosses ``threshold_pct`` and clears state when it
    drops back below. State key: ``margin_stress_<last8>``. ``threshold_pct``
    is expressed in percent (e.g. 90.0 means used/equity ≥ 0.90).
    """
    threshold = max(0.0, min(1.0, threshold_pct / 100.0))
    for w in wallets or []:
        if w.get("status") != "ok":
            continue
        d = w.get("data") or {}
        wallet_addr = d.get("wallet", "") or ""
        label = d.get("label", "") or ""
        eq = d.get("account_value", 0.0) or 0.0
        used = d.get("total_margin_used", 0.0) or 0.0
        ratio = margin_stress_ratio(eq, used)
        if ratio is None:
            # Idle/empty wallet — nothing to alert on; ensure state is clean.
            if wallet_addr:
                _clear(state, f"margin_stress_{wallet_addr[-8:]}")
            continue
        short_addr = wallet_addr[:6] + "\u2026" + wallet_addr[-4:] if wallet_addr else ""
        ident = f"{label} ({short_addr})" if label else short_addr
        key = f"margin_stress_{wallet_addr[-8:]}" if wallet_addr else "margin_stress_unknown"
        if ratio >= threshold:
            # R-BOT-DEFINITIVE WI-3: honest copy — this metric is perp margin
            # used vs perp equity (utilization). Above 100% it only blocks NEW
            # positions; it is NOT liquidation proximity. (Legacy path kept for
            # compatibility; production routes via modules.alerts_margin.)
            msg = (
                f"\U0001f6a8 MARGIN STRESS \u2014 {ident} \u2014 "
                f"Perp margin used vs perp equity = {ratio*100:.1f}% "
                f"(threshold {threshold_pct:.0f}%). "
                f"margin_used=${used:,.0f} \u00b7 account_value=${eq:,.0f}. "
                "Por encima de 100% solo bloquea ABRIR posiciones nuevas \u2014 "
                "NO es proximidad de liquidaci\u00f3n."
            )
            await _emit(bot, key, state, msg)
        else:
            _clear(state, key)
