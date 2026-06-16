"""Kill triggers monitor para el fondo (post-migración a Portfolio Margin).

Reglas embebidas en código (zero-config) — son los kill scenarios que BCD
decidió manualmente. Si una condición se cumple, alerta Telegram con
sugerencia ("alert_only" | "suggest_close"). NUNCA cierra automáticamente
posiciones — la decisión final es siempre humana.

Triggers actuales (cada uno con input LIVE — nunca valores fabricados):
  1. BTC > $82K sostenido 4h (invalida bear trap) — live spot price.
  2. BTC en zona DCA $63-65K (zona verde multipropósito) — live spot price.
  3. PM aave-HF < 1.10 (zona crítica → suggest_close) — fuente ÚNICA
     ``modules.portfolio_margin.compute_pm_state`` sobre el colateral HYPE
     del Portfolio Margin nativo (HL Earn), MISMA que el panel y el canal
     real-risk. NO lee HyperLend (protocolo muerto, el fondo no lo usa).
  4. Basket UPnL < -$2,000 (drawdown extremo → alert_only) — live perps.

R-BOT-DEFINITIVE-KILLCLEAN (2026-06-15): eliminado el trigger
``ueth_apy_above_10`` (UETH borrow APY del flywheel HyperLend pair-trade).
Ese concepto está MUERTO — no mapea a ninguna posición viva y disparaba ruido
("KILL TRIGGER ACTIVE / UETH borrow APY = 26.21%") contra el mandato vigente.

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
    distance = "far"
    if px:
        _record_btc(px)
        if _btc_above_for_hours(82000, 4):
            fired = True
            distance = "ACTIVE"
            detail = f"BTC ${px:,.0f}, sustained > $82K last 4h"
        elif px > 80000:
            distance = "near"
            detail = f"BTC ${px:,.0f} (${82000 - px:,.0f} from $82K)"
        else:
            detail = f"BTC ${px:,.0f}"
    else:
        detail = "BTC price unavailable"
    return TriggerResult(
        trigger_id="btc_above_82k_4h",
        name="BTC > $82K sustained 4h (invalidates bear trap)",
        fired=fired,
        distance_text=distance,
        detail=detail,
        action="alert_only",
    )


async def _evaluate_btc_dca_zone() -> TriggerResult:
    from modules.portfolio import get_spot_price
    px = await get_spot_price("BTC")
    fired = False
    distance = "far"
    detail = ""
    if px:
        if 63000 <= px <= 65000:
            fired = True
            distance = "ACTIVE"
            detail = f"BTC ${px:,.0f} in DCA zone $63-65K (multi-purpose green zone)"
        elif 62000 <= px < 63000 or 65000 < px <= 66000:
            distance = "near"
            detail = f"BTC ${px:,.0f} near zone $63-65K"
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


async def _evaluate_pm_hf() -> TriggerResult:
    """PM aave-HF < 1.10 on the primary HYPE-collateral Portfolio Margin.

    Single source of truth: ``compute_pm_state`` over live HyperLiquid wallet
    data (the SAME math the DESTACADO panel and the real-risk alert channel
    use). NEVER reads HyperLend. If PM data is unavailable or there is no debt
    (no liquidation risk), the trigger reports n/d and NEVER fires on a
    fabricated or stale value.
    """
    aave_hf: float | None = None
    has_debt = False
    try:
        from modules.portfolio import fetch_all_wallets
        from modules.pm_context import select_primary_pm_state
        wallets = await fetch_all_wallets()
        pm = select_primary_pm_state(wallets, None)
        if pm is not None and pm.has_data:
            has_debt = pm.debt_usd > 1.0
            if has_debt and pm.aave_hf > 0:
                aave_hf = float(pm.aave_hf)
    except Exception as exc:  # noqa: BLE001
        log.warning("pm_hf trigger: PM state unavailable: %s", exc)

    fired = False
    distance = "far"
    if not has_debt:
        # No USDC/USDH borrowed → no liquidation risk → nothing to fire on.
        detail = "PM aave-HF n/d (sin deuda — no hay riesgo de liquidación)"
    elif aave_hf is None:
        detail = "PM aave-HF n/d (dato no disponible)"
    elif aave_hf < 1.10:
        fired = True
        distance = "ACTIVE"
        detail = f"PM aave-HF = {aave_hf:.3f} < 1.10 — ZONA CRÍTICA"
    elif aave_hf < 1.20:
        distance = "near"
        detail = f"PM aave-HF = {aave_hf:.3f} (zona observación)"
    else:
        detail = f"PM aave-HF = {aave_hf:.3f} (saludable)"
    return TriggerResult(
        trigger_id="pm_hf_below_110",
        name="PM aave-HF < 1.10 (colateral HYPE — zona crítica)",
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
    distance = "far"
    if not has_basket:
        detail = "Basket has no active positions"
    elif basket_upnl < -2000:
        fired = True
        distance = "ACTIVE"
        detail = f"Basket UPnL = -${abs(basket_upnl):,.2f} < -$2,000 (extreme drawdown)"
    elif basket_upnl < -1000:
        distance = "near"
        detail = f"Basket UPnL = -${abs(basket_upnl):,.2f}"
    else:
        sign = "+" if basket_upnl >= 0 else "-"
        detail = f"Basket UPnL = {sign}${abs(basket_upnl):,.2f}"
    return TriggerResult(
        trigger_id="basket_drawdown_2k",
        name="Basket UPnL < -$2,000 (extreme drawdown)",
        fired=fired,
        distance_text=distance,
        detail=detail,
        action="alert_only",
    )


_TRIGGERS: list[Callable[[], Awaitable[TriggerResult]]] = [
    _evaluate_btc_above_82k,
    _evaluate_btc_dca_zone,
    _evaluate_pm_hf,
    _evaluate_basket_drawdown,
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
    lines = ["🎯 KILL TRIGGERS — basket + Portfolio Margin", "─" * 40]
    for r in results:
        if r.fired:
            tag = "🚨 ACTIVE"
        elif r.distance_text in ("near", "cerca"):
            tag = "⚠️ NEAR"
        else:
            tag = "✅ far"
        lines.append(f"{tag} {r.name}")
        lines.append(f"   {r.detail}")
        lines.append(f"   Action: {r.action}")
        lines.append("")
    lines.append("ℹ️ If /kill_status shows ACTIVE, run /kill for close details.")
    lines.append("Auto-close DISABLED always — final decision is always human.")
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
            f"🚨 KILL TRIGGER ACTIVE\n"
            f"{r.name}\n\n"
            f"{r.detail}\n\n"
            f"Suggested action: {r.action}\n"
            f"Run /kill_status for all triggers,\n"
            f"or /kill for close details."
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
