"""Round 17 — Kill triggers monitor para basket v5 + flywheel.

Reglas embebidas en código (zero-config) — son los kill scenarios que BCD
decidió manualmente. Si una condición se cumple, alerta Telegram con
sugerencia ("alert_only" | "suggest_close"). NUNCA cierra automáticamente
posiciones — la decisión final es siempre humana.

Triggers actuales:
  1. BTC > $82K sostenido 4h (invalida bear trap)
  2. BTC en zona DCA $63-65K (zona verde multipropósito — suggest_close)
  3. HF flywheel < 1.10 (zona crítica → suggest_close)
  4. Basket UPnL < -$2,000 (drawdown extremo → alert_only)
  5. Funding HL negativo + Hormuz signal (geopolitical) — placeholder

Rate limit: cada trigger dispara MÁX 1× por día (key = trigger_id+UTC date).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from config import DATA_DIR, TELEGRAM_CHAT_ID

log = logging.getLogger(__name__)

_KILL_STATE_FILE = os.path.join(DATA_DIR, "kill_trigger_state.json")
_BTC_HISTORY_FILE = os.path.join(DATA_DIR, "btc_price_history.json")


# ─── State ───────────────────────────────────────────────────────────────────


def _load_state() -> dict:
    if not os.path.isfile(_KILL_STATE_FILE):
        return {}
    try:
        with open(_KILL_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        with open(_KILL_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception:
        log.exception("kill_state save failed")


def _btc_history_load() -> list[tuple[str, float]]:
    """Returns list of (iso_ts, price). Used for the "BTC > $82K sustained 4h" check."""
    if not os.path.isfile(_BTC_HISTORY_FILE):
        return []
    try:
        with open(_BTC_HISTORY_FILE) as f:
            data = json.load(f)
        return [(it["ts"], float(it["px"])) for it in data]
    except Exception:
        return []


def _btc_history_save(items: list[tuple[str, float]]) -> None:
    # Keep only last 12h of samples (max 144 at 5min intervals)
    cutoff = datetime.now(timezone.utc).timestamp() - 12 * 3600
    keep: list[dict] = []
    for ts, px in items:
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        if t >= cutoff:
            keep.append({"ts": ts, "px": px})
    try:
        with open(_BTC_HISTORY_FILE, "w") as f:
            json.dump(keep, f)
    except Exception:
        log.exception("btc_history save failed")


def _record_btc(price: float) -> None:
    items = _btc_history_load()
    items.append((datetime.now(timezone.utc).isoformat(), float(price)))
    _btc_history_save(items)


def _btc_above_for_hours(threshold: float, hours: float) -> bool:
    """True if every recorded sample in the last `hours` was >= threshold."""
    items = _btc_history_load()
    if not items:
        return False
    now_t = datetime.now(timezone.utc).timestamp()
    window_start = now_t - hours * 3600
    samples_in_window = []
    for ts, px in items:
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        if t >= window_start:
            samples_in_window.append(px)
    if len(samples_in_window) < max(3, int(hours * 60 / 30)):
        return False  # need enough coverage
    return all(p >= threshold for p in samples_in_window)


# ─── Trigger evaluators ──────────────────────────────────────────────────────


@dataclass
class TriggerResult:
    trigger_id: str
    name: str
    fired: bool
    distance_text: str  # "lejos" | "cerca" | "ACTIVO"
    detail: str
    action: str  # "alert_only" | "suggest_close"


async def _evaluate_btc_above_82k() -> TriggerResult:
    from modules.portfolio import get_spot_price
    px = await get_spot_price("BTC")
    fired = False
    detail = ""
    distance = "lejos"
    if px:
        _record_btc(px)
        if _btc_above_for_hours(82000, 4):
            fired = True
            distance = "ACTIVO"
            detail = f"BTC ${px:,.0f}, sostenido > $82K últimas 4h"
        elif px > 80000:
            distance = "cerca"
            detail = f"BTC ${px:,.0f} (${82000 - px:,.0f} de $82K)"
        else:
            detail = f"BTC ${px:,.0f}"
    else:
        detail = "BTC price unavailable"
    return TriggerResult(
        trigger_id="btc_above_82k_4h",
        name="BTC > $82K sostenido 4h (invalida bear trap)",
        fired=fired,
        distance_text=distance,
        detail=detail,
        action="alert_only",
    )


async def _evaluate_btc_dca_zone() -> TriggerResult:
    from modules.portfolio import get_spot_price
    px = await get_spot_price("BTC")
    fired = False
    distance = "lejos"
    detail = ""
    if px:
        if 63000 <= px <= 65000:
            fired = True
            distance = "ACTIVO"
            detail = f"BTC ${px:,.0f} en zona DCA $63-65K (zona verde multipropósito)"
        elif 62000 <= px < 63000 or 65000 < px <= 66000:
            distance = "cerca"
            detail = f"BTC ${px:,.0f} cerca de zona $63-65K"
        else:
            detail = f"BTC ${px:,.0f}"
    else:
        detail = "BTC price unavailable"
    return TriggerResult(
        trigger_id="btc_dca_63_65",
        name="BTC en zona DCA $63-65K (zona verde)",
        fired=fired,
        distance_text=distance,
        detail=detail,
        action="alert_only",
    )


async def _evaluate_hf_flywheel() -> TriggerResult:
    from modules.hyperlend import fetch_all_hyperlend
    hl = await fetch_all_hyperlend()
    hf_min: float | None = None
    if isinstance(hl, list):
        for r in hl:
            if r.get("status") != "ok":
                continue
            hf = r["data"].get("health_factor")
            if hf is not None and (hf_min is None or hf < hf_min):
                hf_min = float(hf)
    fired = False
    distance = "lejos"
    detail = ""
    if hf_min is not None:
        if hf_min < 1.10:
            fired = True
            distance = "ACTIVO"
            detail = f"HF flywheel = {hf_min:.3f} < 1.10 — ZONA CRÍTICA"
        elif hf_min < 1.20:
            distance = "cerca"
            detail = f"HF flywheel = {hf_min:.3f} (zona monitoreo)"
        else:
            detail = f"HF flywheel = {hf_min:.3f} (saludable)"
    else:
        detail = "HF data unavailable"
    return TriggerResult(
        trigger_id="hf_flywheel_below_110",
        name="HF flywheel < 1.10 (zona crítica)",
        fired=fired,
        distance_text=distance,
        detail=detail,
        action="suggest_close",
    )


async def _evaluate_basket_drawdown() -> TriggerResult:
    from modules.portfolio import fetch_all_wallets
    try:
        from fund_state import BASKET_PERP_TOKENS
    except Exception:
        BASKET_PERP_TOKENS = []  # type: ignore
    wallets = await fetch_all_wallets()
    basket_upnl = 0.0
    has_basket = False
    if isinstance(wallets, list):
        for w in wallets:
            if w.get("status") != "ok":
                continue
            for pos in w.get("data", {}).get("positions") or []:
                coin = (pos.get("coin") or "").upper()
                if coin not in BASKET_PERP_TOKENS:
                    continue
                try:
                    upnl = float(pos.get("unrealized_pnl") or 0.0)
                except Exception:
                    upnl = 0.0
                basket_upnl += upnl
                has_basket = True
    fired = False
    distance = "lejos"
    if not has_basket:
        detail = "Basket sin posiciones activas"
    elif basket_upnl < -2000:
        fired = True
        distance = "ACTIVO"
        detail = f"Basket UPnL = -${abs(basket_upnl):,.2f} < -$2,000 (drawdown extremo)"
    elif basket_upnl < -1000:
        distance = "cerca"
        detail = f"Basket UPnL = -${abs(basket_upnl):,.2f}"
    else:
        sign = "+" if basket_upnl >= 0 else "-"
        detail = f"Basket UPnL = {sign}${abs(basket_upnl):,.2f}"
    return TriggerResult(
        trigger_id="basket_drawdown_2k",
        name="Basket UPnL < -$2,000 (drawdown extremo)",
        fired=fired,
        distance_text=distance,
        detail=detail,
        action="alert_only",
    )


async def _evaluate_ueth_borrow_apy() -> TriggerResult:
    from modules.hyperlend import fetch_reserve_rates
    payload = await fetch_reserve_rates()
    apy: float | None = None
    if payload.get("status") == "ok":
        rates_map = payload.get("rates") or {}
        # rates_map keyed by symbol; accept UETH or eth_chain entries
        for sym, v in rates_map.items():
            sym_u = (sym or "").upper()
            if sym_u in ("UETH", "WETH", "ETH"):
                apy = float(v.get("apy_borrow") or 0.0) * 100
                break
    fired = False
    distance = "lejos"
    if apy is None:
        detail = "UETH APY data unavailable"
    elif apy > 10:
        fired = True
        distance = "ACTIVO"
        detail = f"UETH borrow APY = {apy:.2f}% > 10% (insostenible para flywheel)"
    elif apy > 6:
        distance = "cerca"
        detail = f"UETH borrow APY = {apy:.2f}% > 6% (zona warning)"
    else:
        detail = f"UETH borrow APY = {apy:.2f}%"
    return TriggerResult(
        trigger_id="ueth_apy_above_10",
        name="UETH borrow APY > 10% (flywheel insostenible)",
        fired=fired,
        distance_text=distance,
        detail=detail,
        action="alert_only",
    )


_TRIGGERS: list[Callable[[], Awaitable[TriggerResult]]] = [
    _evaluate_btc_above_82k,
    _evaluate_btc_dca_zone,
    _evaluate_hf_flywheel,
    _evaluate_basket_drawdown,
    _evaluate_ueth_borrow_apy,
]


# ─── Public API ──────────────────────────────────────────────────────────────


async def evaluate_all() -> list[TriggerResult]:
    results: list[TriggerResult] = []
    for fn in _TRIGGERS:
        try:
            r = await fn()
            results.append(r)
        except Exception as exc:  # noqa: BLE001
            log.warning("kill trigger %s failed: %s", fn.__name__, exc)
    return results


def format_kill_status(results: list[TriggerResult]) -> str:
    lines = ["🎯 KILL TRIGGERS — basket v5 + flywheel", "─" * 40]
    for r in results:
        if r.fired:
            tag = "🚨 ACTIVO"
        elif r.distance_text == "cerca":
            tag = "⚠️ CERCA"
        else:
            tag = "✅ lejos"
        lines.append(f"{tag} {r.name}")
        lines.append(f"   {r.detail}")
        lines.append(f"   Acción: {r.action}")
        lines.append("")
    lines.append("ℹ️ Si /kill_status muestra ACTIVO, ejecutar /kill para detalle.")
    lines.append("Auto-close DESACTIVADO siempre — la decisión final es humana.")
    return "\n".join(lines)


async def scheduled_check(bot) -> int:
    """Corre cada 5min. Si nuevo trigger ACTIVO (no alertado hoy), envía alerta."""
    if os.getenv("KILL_TRIGGERS_ENABLED", "true").strip().lower() == "false":
        return 0
    if not TELEGRAM_CHAT_ID:
        return 0

    from utils.telegram import send_bot_message

    state = _load_state()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fired_today = set(state.get(today, []))
    sent = 0

    results = await evaluate_all()
    for r in results:
        if not r.fired:
            continue
        if r.trigger_id in fired_today:
            continue
        msg = (
            f"🚨 KILL TRIGGER ACTIVO\n"
            f"{r.name}\n\n"
            f"{r.detail}\n\n"
            f"Acción sugerida: {r.action}\n"
            f"Ejecutar /kill_status para todos los triggers,\n"
            f"o /kill para detalle de cierre."
        )
        try:
            await send_bot_message(bot, TELEGRAM_CHAT_ID, msg)
            fired_today.add(r.trigger_id)
            sent += 1
        except Exception:
            log.exception("kill scheduled alert failed: %s", r.trigger_id)

    state[today] = list(fired_today)
    # purge old days (keep last 7)
    keys = sorted(state.keys())
    if len(keys) > 7:
        for k in keys[:-7]:
            state.pop(k, None)
    _save_state(state)

    if sent:
        log.info("basket_killer dispatched %d alert(s)", sent)
    return sent
