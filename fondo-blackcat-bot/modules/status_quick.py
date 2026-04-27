"""Round 17 — /status quick: snapshot del fondo en <3s sin LLM ni X API.

Lee los datos baratos (HL on-chain + market data en cache + last_basket_upnl
desde snapshots) y devuelve un block compacto. Sirve como reemplazo barato
de /reporte para BCD cuando solo quiere ver capital + HF + UPnL rápido.

Sources tocadas:
    - hyperlend.fetch_all_hyperlend()  (cache 30s on second call)
    - market.fetch_market_data()       (cache 60s)
    - portfolio.fetch_all_wallets()    (cached if available)
    - fund_state.BASKET_V5_STATUS, TRADE_DEL_CICLO_STATUS
    - macro_calendar.next_event()      (read-only)
    - errors_log: count last 24h

Latency target: <3s típico.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


def _fmt_usd(v: float | None, decimals: int = 0) -> str:
    if v is None:
        return "—"
    try:
        if decimals == 0:
            return f"${v:,.0f}"
        return f"${v:,.{decimals}f}"
    except Exception:
        return "—"


def _fmt_signed_usd(v: float | None) -> str:
    if v is None:
        return "—"
    try:
        sign = "+" if v >= 0 else "-"
        return f"{sign}${abs(v):,.2f}"
    except Exception:
        return "—"


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "—"
    try:
        sign = "+" if v >= 0 else ""
        return f"{sign}{v:.2f}%"
    except Exception:
        return "—"


async def build_status_block() -> str:
    """Build the full /status text block. Resilient to per-source failures."""
    from modules.hyperlend import fetch_all_hyperlend
    from modules.market import fetch_market_data
    from modules.portfolio import fetch_all_wallets
    from modules.errors_log import recent as recent_errors

    # Concurrent fetches with per-task error swallowing
    async def _safe(coro, label):
        try:
            return await coro
        except Exception as exc:  # noqa: BLE001
            log.warning("/status: %s failed: %s", label, exc)
            return None

    hl_list, market, wallets = await asyncio.gather(
        _safe(fetch_all_hyperlend(), "hyperlend"),
        _safe(fetch_market_data(), "market"),
        _safe(fetch_all_wallets(), "wallets"),
    )

    # ─── Capital (HL + perp account values) ─────────────────────────────────
    hl_collateral_usd = 0.0
    hl_debt_usd = 0.0
    flywheel_hf: float | None = None
    flywheel_collateral_bal: float | None = None
    flywheel_debt_sym: str | None = None
    flywheel_debt_bal: float | None = None
    if isinstance(hl_list, list):
        for r in hl_list:
            if r.get("status") != "ok":
                continue
            d = r["data"]
            hl_collateral_usd += float(d.get("total_collateral_usd") or 0.0)
            hl_debt_usd += float(d.get("total_debt_usd") or 0.0)
            # Pick the wallet with debt as flywheel
            if (d.get("total_debt_usd") or 0) > 0 and flywheel_hf is None:
                flywheel_hf = d.get("health_factor")
                flywheel_collateral_bal = d.get("collateral_balance")
                flywheel_debt_sym = d.get("debt_symbol")
                flywheel_debt_bal = d.get("debt_balance")

    perp_account_value = 0.0
    perp_unrealized = 0.0
    basket_active = False
    basket_positions_count = 0
    if isinstance(wallets, list):
        from fund_state import BASKET_PERP_TOKENS
        for w in wallets:
            if w.get("status") != "ok":
                continue
            d = w.get("data", {})
            perp_account_value += float(d.get("account_value") or 0.0)
            perp_unrealized += float(d.get("unrealized_pnl_total") or 0.0)
            for pos in d.get("positions") or []:
                coin = (pos.get("coin") or "").upper()
                if coin in BASKET_PERP_TOKENS:
                    basket_active = True
                    basket_positions_count += 1

    total_capital = hl_collateral_usd - hl_debt_usd + perp_account_value

    # ─── Market ──────────────────────────────────────────────────────────────
    btc = eth = hype = None
    fg_value = fg_label = None
    if isinstance(market, dict) and market.get("status") == "ok":
        prices = market["data"].get("prices", {})
        btc = (prices.get("bitcoin") or {}).get("usd")
        eth = (prices.get("ethereum") or {}).get("usd")
        hype = (prices.get("hyperliquid") or {}).get("usd")
        fg = market["data"].get("fear_greed") or {}
        fg_value = fg.get("value")
        fg_label = fg.get("classification") or fg.get("label")

    # ─── Fund state from authoritative file ──────────────────────────────────
    try:
        from fund_state import (
            BASKET_V5_STATUS,
            TRADE_DEL_CICLO_STATUS,
            TRADE_DEL_CICLO_PNL_REALIZED,
        )
    except Exception:
        BASKET_V5_STATUS = "?"
        TRADE_DEL_CICLO_STATUS = "?"
        TRADE_DEL_CICLO_PNL_REALIZED = 0.0

    # ─── Active alerts (errors last 1h) ─────────────────────────────────────
    try:
        recent_errs = recent_errors(limit=50)
        # count those in last 1h
        cutoff = datetime.now(timezone.utc).timestamp() - 3600
        active_alerts = 0
        for e in recent_errs:
            ts = e.get("timestamp_utc") or ""
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if dt.timestamp() > cutoff:
                    active_alerts += 1
            except Exception:
                continue
    except Exception:
        active_alerts = 0

    # ─── Next macro event ────────────────────────────────────────────────────
    next_event_text = "—"
    next_event_in = "—"
    try:
        from modules.macro_calendar import next_upcoming_event, format_time_until
        ev = next_upcoming_event()
        if ev is not None:
            next_event_text = ev.name
            next_event_in = format_time_until(ev.timestamp_utc)
    except Exception:
        pass

    # ─── Compose ─────────────────────────────────────────────────────────────
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sep = "─" * 36

    lines: list[str] = []
    lines.append("🐱‍⬛ STATUS — " + now_utc)
    lines.append(sep)
    lines.append(f"💰 Capital total: {_fmt_usd(total_capital)}")
    lines.append(f"   HL collateral: {_fmt_usd(hl_collateral_usd)} | debt: {_fmt_usd(hl_debt_usd)}")
    lines.append(f"   Perp account value: {_fmt_usd(perp_account_value)}")
    lines.append(f"📊 UPnL perp: {_fmt_signed_usd(perp_unrealized)}")
    lines.append("")
    if flywheel_hf is not None:
        try:
            hf_str = f"{flywheel_hf:.3f}"
        except Exception:
            hf_str = "—"
        lines.append(f"🔁 Flywheel HF: {hf_str}")
        if flywheel_collateral_bal is not None:
            lines.append(
                f"   Colateral: {flywheel_collateral_bal:.2f} (kHYPE)"
            )
        if flywheel_debt_bal is not None and flywheel_debt_sym:
            lines.append(
                f"   Deuda: {flywheel_debt_bal:.4f} {flywheel_debt_sym}"
            )
    else:
        lines.append("🔁 Flywheel: sin colateral activo o RPC offline")
    lines.append("")
    lines.append(f"🎯 Basket v5: {BASKET_V5_STATUS}")
    if basket_active:
        lines.append(f"   ↳ Posiciones SHORT activas: {basket_positions_count}")
    lines.append(f"💎 Trade del Ciclo (Blofin): {TRADE_DEL_CICLO_STATUS}")
    if TRADE_DEL_CICLO_STATUS == "CLOSED":
        lines.append(
            f"   PnL realized último ciclo: "
            f"{_fmt_signed_usd(float(TRADE_DEL_CICLO_PNL_REALIZED or 0))}"
        )
    lines.append("")
    lines.append(
        f"🌡 BTC: {_fmt_usd(btc)} | ETH: {_fmt_usd(eth)} | "
        f"HYPE: {_fmt_usd(hype, decimals=2)}"
    )
    if fg_value is not None:
        lines.append(f"😨 F&G: {fg_value} ({fg_label or '—'})")
    lines.append("")
    lines.append(f"⚠️ Errores última hora: {active_alerts}")
    lines.append(f"🔮 Próximo catalyst: {next_event_text}")
    if next_event_in != "—":
        lines.append(f"   ETA: {next_event_in}")
    lines.append("")
    lines.append("ℹ️ /reporte para análisis LLM completo (30-90s)")
    return "\n".join(lines)
