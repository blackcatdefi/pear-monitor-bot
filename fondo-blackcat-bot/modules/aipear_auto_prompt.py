"""Round 18.3.3 — AiPear auto-prompt generator post-basket-close.

When a basket is fully closed (or a major rebalance happens), generate a ready
to-paste prompt for the next basket version (v(N+1)) that BCD can take to AiPear
or any LLM-based portfolio designer.

Reads:
  * fund_state.BASKET_V5_STATUS, BASKET_V5_PLAN
  * portfolio_snapshot for capital
  * intel_memory for recent regime tagging

Kill switch: ``AIPEAR_AUTOPROMPT_ENABLED=false``.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


def _is_enabled() -> bool:
    return os.getenv("AIPEAR_AUTOPROMPT_ENABLED", "true").strip().lower() != "false"


def _bump_version_label(current: str | None) -> str:
    """`basket_v5` → `basket_v6`. Falls back to `basket_vN+1` heuristic."""
    if not current:
        return "basket_v_next"
    cur = current.strip().lower()
    # Try to find vN substring
    import re
    m = re.search(r"v(\d+)", cur)
    if not m:
        return f"{cur}_next"
    n = int(m.group(1))
    return cur.replace(f"v{n}", f"v{n + 1}")


async def _capital_summary() -> dict[str, Any]:
    out: dict[str, Any] = {"capital_total": None, "btc": None, "eth": None, "hype": None,
                           "fear_greed": None, "regime_hint": "neutral"}
    try:
        from modules.portfolio_snapshot import build_portfolio_snapshot
        snap = await build_portfolio_snapshot()
        out["capital_total"] = snap.capital_total
        out["btc"] = snap.market.btc
        out["eth"] = snap.market.eth
        out["hype"] = snap.market.hype
        out["fear_greed"] = snap.market.fear_greed_value
        if snap.market.fear_greed_value is not None:
            fg = snap.market.fear_greed_value
            if fg >= 70:
                out["regime_hint"] = "greed (watch FOMO)"
            elif fg <= 30:
                out["regime_hint"] = "fear (accumulation opportunity)"
            else:
                out["regime_hint"] = "neutral"
    except Exception:  # noqa: BLE001
        log.exception("aipear_auto_prompt: snapshot failed")
    return out


def _recent_intel_themes(hours: int = 48, limit: int = 8) -> list[str]:
    """Pull short headline-y items from intel_memory if available."""
    try:
        from modules.intel_memory import _conn as imem_conn
        from datetime import timedelta
        cutoff_iso = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        c = imem_conn()
        rows = c.execute(
            "SELECT raw_text FROM intel_memory WHERE timestamp_utc>=? "
            "ORDER BY timestamp_utc DESC LIMIT ?",
            (cutoff_iso, int(limit)),
        ).fetchall()
        c.close()
        out = []
        for (txt,) in rows:
            t = (txt or "").strip().splitlines()[0][:160]
            if t:
                out.append(t)
        return out
    except Exception:
        return []


async def generate_aipear_prompt_post_basket(basket_close: dict[str, Any] | None = None) -> str:
    """basket_close is best-effort: dict with keys label/exit_pnl/winners/losers."""
    label = "basket_v?"
    exit_pnl = None
    winners: list[str] = []
    losers: list[str] = []
    if basket_close:
        label = basket_close.get("label") or label
        exit_pnl = basket_close.get("exit_pnl")
        winners = basket_close.get("winners") or []
        losers = basket_close.get("losers") or []

    next_label = _bump_version_label(label)
    cap = await _capital_summary()
    intel = _recent_intel_themes()

    fmt_pnl = f"${exit_pnl:,.2f}" if isinstance(exit_pnl, (int, float)) else "—"
    fmt_cap = f"${cap['capital_total']:,.0f}" if cap.get("capital_total") else "—"
    fmt_btc = f"${cap['btc']:,.0f}" if cap.get("btc") else "—"
    fmt_eth = f"${cap['eth']:,.0f}" if cap.get("eth") else "—"
    fmt_hype = f"${cap['hype']:,.2f}" if cap.get("hype") else "—"
    fmt_fg = str(cap.get("fear_greed") or "—")

    lines = [
        f"🤖 AIPEAR PROMPT — {next_label.upper()}",
        "─" * 36,
        f"Previous basket: {label} (Realized PnL: {fmt_pnl})",
        f"Available capital: {fmt_cap}",
        f"Market: BTC {fmt_btc} · ETH {fmt_eth} · HYPE {fmt_hype} · FG {fmt_fg} ({cap['regime_hint']})",
        "",
        "Winners in {label}:".format(label=label),
    ]
    lines.append("  • " + (", ".join(winners) if winners else "—"))
    lines.append("Losers:")
    lines.append("  • " + (", ".join(losers) if losers else "—"))
    lines.append("")
    lines.append("Recent intel (last 48h):")
    if intel:
        for it in intel[:6]:
            lines.append(f"  • {it}")
    else:
        lines.append("  • (no cached intel)")
    lines.append("")
    lines.append("─" * 36)
    lines.append("PROMPT FOR AIPEAR (copy-paste):")
    lines.append("─" * 36)
    lines.append(
        f"Design {next_label}: short basket of 4-6 alts vs USDC on HyperLiquid. "
        f"Capital: {fmt_cap}. Regime: {cap['regime_hint']}. "
        f"Learned from previous basket: rotate out of losers ({', '.join(losers) or 'n/a'}), "
        f"weight exposure toward bias confirmed by recent intel. "
        "Output: list (asset, side, weight%, leverage, entry zone, SL pct, trailing pct activation pct), "
        "2-3 line thesis per name, macro and micro kill triggers, "
        "conditions to add/scale-down. Target beta -0.8 to -1.2 vs BTC."
    )
    lines.append("")
    lines.append("ℹ️ Paste into Aipear/Claude/SuperGrok. Upload as BASKET_V5_PLAN when ready.")
    return "\n".join(lines)


async def maybe_send_post_basket_prompt(bot, basket_close: dict[str, Any] | None) -> bool:
    if not _is_enabled():
        return False
    try:
        from config import TELEGRAM_CHAT_ID
        if not TELEGRAM_CHAT_ID or bot is None:
            return False
        text = await generate_aipear_prompt_post_basket(basket_close)
        from utils.telegram import send_bot_message
        await send_bot_message(bot, TELEGRAM_CHAT_ID, text)
        return True
    except Exception:  # noqa: BLE001
        log.exception("aipear_auto_prompt: send failed")
        return False
