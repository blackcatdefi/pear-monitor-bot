"""Periodic alert checks (HF flywheel, HYPE/BTC crashes, BCD DCA, margin stress).

Edge-triggered: keeps last alert state in data/alert_state.json to avoid spam.

R-NOPRELIQ (2026-05-15): basket per-leg pre-liq alerts ELIMINADOS.
BCD pone SL/TP 100% nativo en HL por pata, no necesita pre-liq alerts en basket.
Flywheel HF gates (1.10/1.05/1.02) y MARGIN_STRESS edge 90% wallet-level
permanecen como red de seguridad.
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from config import (
    BTC_WARN,
    DATA_DIR,
    HF_CRITICAL,
    HF_WARN,
    HYPE_CRITICAL,
    HYPE_WARN,
    TELEGRAM_CHAT_ID,
)
from fund_state import BCD_DCA_PLAN
from modules.hyperlend import fetch_all_hyperlend, fetch_reserve_rates
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

    # 1. HyperLend HF (all wallets) — R-SILENT: gated by auto.hf_alert_gate
    # Threshold defaults: 1.10 / 1.05 / 1.02 (warn / critical / preliq).
    # Dedup 2h, delta 0.05; preliq fires every 5min until recovery.
    try:
        from auto import hf_alert_gate as hfg  # noqa: WPS433
    except Exception:  # noqa: BLE001
        hfg = None  # type: ignore[assignment]
    try:
        from auto import silent_mode as _silent  # noqa: WPS433
    except Exception:  # noqa: BLE001
        _silent = None  # type: ignore[assignment]

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

        if hf is None or math.isinf(hf) or math.isnan(hf):
            continue
        hf_r = round(hf, 4)

        if hfg is None:
            # Gate import failed: legacy passthrough (warn under HF_WARN, critical under HF_CRITICAL)
            wallet_key = wallet_addr[-8:] if wallet_addr else "unknown"
            if hf_r < HF_CRITICAL:
                await _emit(
                    bot, f"hf_critical_{wallet_key}", state,
                    f"\U0001f6a8 HYPERLEND HF CRITICAL: {hf_r:.4f} \u2014 {ident} \u2014 immediate action required!"
                )
            else:
                _clear(state, f"hf_critical_{wallet_key}")
            continue

        decision = hfg.decide(wallet_addr, hf_r)
        if not decision.should_emit:
            # If recovered above threshold, clear the dedup state so next drop
            # is a fresh first-cross alert.
            if decision.severity is None:
                hfg.clear_wallet(wallet_addr)
            continue

        # Silent-mode hardening: only critical/preliq escape silent mode.
        if _silent is not None and _silent.is_silent():
            if decision.severity not in {"critical", "preliq"}:
                log.info(
                    "alerts.HF: suppressed (silent_mode ON, severity=%s) %s hf=%.4f",
                    decision.severity, ident, hf_r,
                )
                continue

        if decision.severity == "preliq":
            msg = (
                f"\U0001f6a8\U0001f6a8 HYPERLEND PRE-LIQUIDATION: HF {hf_r:.4f} \u2014 {ident} \u2014 "
                f"immediate action, urgent repay. (Repeats every {hfg.PRELIQ_REPEAT_MIN}min until recovery)"
            )
        elif decision.severity == "critical":
            msg = (
                f"\U0001f6a8 HYPERLEND HF CRITICAL: {hf_r:.4f} \u2014 {ident} \u2014 "
                f"below {hfg.CRITICAL:.2f}, evaluate repay/collateral"
            )
        else:  # warn
            msg = (
                f"\u26a0\ufe0f HYPERLEND HF: {hf_r:.4f} \u2014 {ident} \u2014 "
                f"below {hfg.THRESHOLD:.2f} (warn zone)"
            )
        try:
            if TELEGRAM_CHAT_ID:
                await send_bot_message(bot, TELEGRAM_CHAT_ID, msg)
            log.warning("ALERT hf_%s: %s", decision.severity, msg)
            hfg.record_emit(wallet_addr, hf_r, decision.severity)
        except Exception:  # noqa: BLE001
            log.exception("alerts.HF send failed for %s", ident)

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
                        f"\U0001f6a8 HYPE @ ${hype_px:.2f} \u2014 direct impact on HyperLend collateral")
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
                        "evaluate rotation to stable or immediate partial repay "
                        "(critical threshold 10%)."
                    )
                else:
                    _clear(state, "ueth_borrow_critical")

                if apy >= 0.06:
                    await _emit(
                        bot, "ueth_borrow_warn", state,
                        f"\u26a0\ufe0f [FLYWHEEL] UETH borrow APY = {apy*100:.2f}% — "
                        "above thesis 6% threshold. Pair trade cost "
                        "becomes unsustainable if sustained."
                    )
                else:
                    _clear(state, "ueth_borrow_warn")
                    _clear(state, "ueth_borrow_critical")
    except Exception:  # noqa: BLE001
        log.exception("UETH borrow APY check failed (non-fatal)")

    # 6. (Removido R-NOPRELIQ 2026-05-15) Per-leg basket pre-liquidation alerts.
    # BCD pone SL/TP 100% nativo en HL por pata, no necesita pre-liq alerts en basket.
    # Flywheel HF gates (1.10/1.05/1.02) y MARGIN_STRESS edge 90% wallet-level
    # quedan como red de seguridad de fondo.
    wallets = await fetch_all_wallets()

    # 6b. R-BOT-DEFINITIVE WI-3 (2026-06-10) — margin alerting redesigned.
    # The legacy edge-triggered MARGIN STRESS watchdog (1 fire per breach but
    # re-armed by state churn → 6 identical alerts/night, wrong "buffer to
    # liquidation" copy) is replaced by modules.alerts_margin:
    #   * "Perp margin used vs perp equity" — band transitions (<90/90-100/
    #     100-110/>110) + >5pp worsening, 6h cooldown, SQLite persisted, copy
    #     states >100% only blocks NEW positions (never liquidation language).
    #   * REAL-risk channel — PM aave-HF crossings 1.30/1.20/1.10 + any single
    #     position liq distance <12% / <8% (fed by compute_pm_state + live
    #     position data only).
    try:
        from modules.cron_state import margin_stress_enabled
        if margin_stress_enabled():
            from modules.alerts_margin import run_margin_alerts
            await run_margin_alerts(bot, wallets)
    except Exception:  # noqa: BLE001
        log.exception("margin alerts (WI-3) failed (non-fatal)")

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

    # 7. BCD DCA zone watchdog (Round 13)
    # Para cada asset en BCD_DCA_PLAN chequear si el precio entra en un range
    # con status lógico "pending" (= sin alerta activa en las últimas 24h).
    # Estado por zona en alert_state.json:
    #   dca_<asset>_<idx>_alerted_at:  ISO ts del último emit
    #   dca_<asset>_<idx>_in_zone:     bool — currently inside el range
    # Reset a "pending": si el precio salió de la zona Y pasaron 24h desde
    # alerted_at — la próxima entrada re-emite.
    try:
        await _run_dca_zone_alerts(bot, state)
    except Exception:  # noqa: BLE001
        log.exception("DCA zone alerts failed (non-fatal)")

    _save_state(state)


# ─── Round 13: BCD DCA zone watchdog ───────────────────────────────────────
_DCA_ASSETS_TO_CHECK = ("BTC", "ETH", "HYPE")
_DCA_ALERT_REARM_HOURS = 24


def _dca_alerted_within_window(state: dict[str, Any], key: str) -> bool:
    """Return True if the alerted_at timestamp is within the rearm window."""
    ts_raw = state.get(key)
    if not ts_raw:
        return False
    try:
        ts = datetime.fromisoformat(str(ts_raw))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts) < timedelta(hours=_DCA_ALERT_REARM_HOURS)


async def _run_dca_zone_alerts(bot, state: dict[str, Any]) -> None:
    for asset in _DCA_ASSETS_TO_CHECK:
        plan = BCD_DCA_PLAN.get(asset) or {}
        tranches = plan.get("tranches") or []
        if not tranches:
            continue
        px = await get_spot_price(asset)
        if px is None or px <= 0:
            continue

        for idx, t in enumerate(tranches):
            rng = t.get("range") or []
            if len(rng) != 2:
                continue
            low, high = float(rng[0]), float(rng[1])
            in_zone = low <= px <= high
            alerted_key = f"dca_{asset}_{idx}_alerted_at"
            zone_key = f"dca_{asset}_{idx}_in_zone"

            if in_zone:
                if not _dca_alerted_within_window(state, alerted_key):
                    pct = t.get("pct", 0)
                    msg = (
                        f"\U0001f3af [DCA ALERT] {asset} @ ${px:,.2f} "
                        f"entered zone {pct}% (${low:,.0f}-${high:,.0f}). "
                        f"Evaluate buy."
                    )
                    log.warning("DCA ZONE: %s", msg)
                    if TELEGRAM_CHAT_ID:
                        await send_bot_message(bot, TELEGRAM_CHAT_ID, msg)
                    state[alerted_key] = datetime.now(timezone.utc).isoformat()
                state[zone_key] = True

                # Asset-specific companion alert: ETH entered debt_flip_range
                if asset == "ETH":
                    flip = plan.get("debt_flip_range") or []
                    if len(flip) == 2:
                        flow, fhigh = float(flip[0]), float(flip[1])
                        if flow <= px <= fhigh:
                            flip_key = "dca_ETH_debt_flip_alerted_at"
                            if not _dca_alerted_within_window(state, flip_key):
                                msg = (
                                    f"\U0001f501 [FLYWHEEL] ETH @ ${px:,.2f} entered "
                                    f"debt_flip_range (${flow:,.0f}-${fhigh:,.0f}). "
                                    f"Consider rotating UETH debt to stable "
                                    f"(USDT0/USDC) before the rebound."
                                )
                                log.warning("ETH DEBT FLIP: %s", msg)
                                if TELEGRAM_CHAT_ID:
                                    await send_bot_message(bot, TELEGRAM_CHAT_ID, msg)
                                state[flip_key] = datetime.now(timezone.utc).isoformat()
            else:
                # Fuera de la zona: si ya pasó la ventana de rearm, limpiar
                # alerted_at para que la próxima entrada pueda re-emitir.
                if state.get(zone_key):
                    state[zone_key] = False
                if state.get(alerted_key) and not _dca_alerted_within_window(state, alerted_key):
                    state.pop(alerted_key, None)


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
