"""Formatters for quick replies (no Claude needed)."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any


def _fmt_usd(v: float | None) -> str:
    if v is None:
        return "—"
    if math.isinf(v):
        return "∞"
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1_000_000:
        return f"{sign}${v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"{sign}${v/1_000:.1f}K"
    return f"{sign}${v:.2f}"


def _fmt_hf(v: float | None) -> str:
    if v is None:
        return "—"
    if math.isinf(v):
        return "∞ (sin deuda)"
    return f"{v:.3f}"


def format_quick_positions(wallets: list[dict[str, Any]], hyperlend: list[dict[str, Any]] | dict[str, Any], bounce_tech: list[dict[str, Any]] | None = None) -> str:
    lines: list[str] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"📊 Snapshot Fondo Black Cat — {now}")
    lines.append("")
    lines.append("PORTFOLIO HYPERLIQUID")
    total_eq = 0.0
    total_upnl = 0.0
    for w in wallets:
        if w.get("status") != "ok":
            label = w.get("label", "?")
            short = (w.get("wallet") or "")[:6] + "…"
            lines.append(f"  • {label} ({short}): ❌ {w.get('error','error')}")
            continue
        d = w["data"]
        short = d["wallet"][:6] + "…" + d["wallet"][-4:]
        eq_val = d.get("account_value") or 0.0
        upnl_val = d.get("unrealized_pnl_total") or 0.0
        total_eq += eq_val
        total_upnl += upnl_val
        eq = _fmt_usd(eq_val)
        upnl = _fmt_usd(upnl_val)
        ntl = _fmt_usd(d.get("total_ntl_pos"))
        positions = d.get("positions") or []
        if positions:
            pos_summary = ", ".join(f"{p['side']} {p['coin']}" for p in positions[:5])
        else:
            pos_summary = "sin posiciones perp"
        lines.append(f"  • {d['label']} {short}")
        lines.append(f"    Equity: {eq} | UPnL: {upnl} | Notional: {ntl}")
        lines.append(f"    {pos_summary}")
    lines.append(f"  TOTAL: Equity {_fmt_usd(total_eq)} | UPnL {_fmt_usd(total_upnl)}")

    # HyperLend section — supports both list (new) and dict (legacy) format
    lines.append("")
    lines.append("HYPERLEND")
    hl_list = hyperlend if isinstance(hyperlend, list) else [hyperlend]
    for hl in hl_list:
        if hl.get("status") == "ok":
            h = hl["data"]
            coll = h.get("total_collateral_usd", 0.0) or 0.0
            if coll < 0.01:
                continue
            label = h.get("label") or hl.get("label") or "—"
            wallet_short = (h.get("wallet") or "")[:6]
            if wallet_short:
                wallet_short = wallet_short + "…" + (h.get("wallet") or "")[-4:]
            header = f"  [{label}]" + (f" {wallet_short}" if wallet_short else "")
            lines.append(header)
            lines.append(f"    HF: {_fmt_hf(h.get('health_factor'))}")
            lines.append(f"    Colateral: {_fmt_usd(h.get('total_collateral_usd'))}")
            lines.append(f"    Borrowed: {_fmt_usd(h.get('total_debt_usd'))}")
            lines.append(f"    Available borrow: {_fmt_usd(h.get('available_borrows_usd'))}")
            lines.append(f"    LTV: {(h.get('ltv') or 0)*100:.1f}% | LiqThr: {(h.get('current_liquidation_threshold') or 0)*100:.1f}%")
        else:
            lines.append(f"  ❌ {hl.get('error','error')}")

    # ── Bounce Tech leveraged tokens ──
    if bounce_tech:
        bt_positions = []
        for bw in bounce_tech:
            if bw.get("status") != "ok":
                continue
            for p in bw.get("positions", []):
                bt_positions.append(p)
        if bt_positions:
            lines.append("")
            lines.append("BOUNCE TECH (Leveraged Tokens)")
            bt_total = 0.0
            for p in bt_positions:
                direction = "🟢 LONG" if p.get("is_long") else "🔴 SHORT"
                asset = p.get("asset", "?")
                lev = p.get("leverage", "?")
                val = p.get("value_usd", 0.0)
                bt_total += val
                lines.append(f"  {direction} {asset} {lev} — {_fmt_usd(val)}")
            lines.append(f"  Total BT: {_fmt_usd(bt_total)}")

    return "\n".join(lines)


def format_hf(hyperlend: list[dict[str, Any]] | dict[str, Any]) -> str:
    """Format HF for /hf command — supports both list and single dict."""
    hl_list = hyperlend if isinstance(hyperlend, list) else [hyperlend]
    parts: list[str] = []
    for hl in hl_list:
        if hl.get("status") != "ok":
            parts.append(f"❌ HyperLend: {hl.get('error','error')}")
            continue
        h = hl["data"]
        coll = h.get("total_collateral_usd", 0.0) or 0.0
        if coll < 0.01:
            continue
        hf = h.get("health_factor")
        icon = "🟢"
        if hf is not None and not math.isinf(hf):
            if hf < 1.10:
                icon = "🚨"
            elif hf < 1.20:
                icon = "⚠️"
        label = h.get("label") or hl.get("label") or "—"
        parts.append(
            f"{icon} [{label}] HF: {_fmt_hf(hf)}\n"
            f"  Colateral: {_fmt_usd(h.get('total_collateral_usd'))} | "
            f"Borrowed: {_fmt_usd(h.get('total_debt_usd'))} | "
            f"Available: {_fmt_usd(h.get('available_borrows_usd'))}"
        )
    return "\n".join(parts) if parts else "— Sin posiciones HyperLend activas"


def compile_raw_data(
    portfolio: list[dict[str, Any]] | None,
    hyperlend: list[dict[str, Any]] | dict[str, Any] | None,
    market: dict[str, Any] | None,
    unlocks: dict[str, Any] | None,
    telegram_intel: dict[str, Any] | None,
    bounce_tech: list[dict[str, Any]] | None = None,
) -> str:
    """Build the user message that we feed to Claude with all raw data."""
    import json

    now = datetime.now(timezone.utc).isoformat()
    blob = {
        "timestamp_utc": now,
        "portfolio": portfolio or [],
        "hyperlend": hyperlend or {},
        "market": market or {},
        "unlocks": unlocks or {},
        "telegram_intel": telegram_intel or {},
        "bounce_tech": bounce_tech or [],
    }
    pretty = json.dumps(blob, ensure_ascii=False, indent=2, default=str)
    return (
        "DATA CRUDA (timestamp UTC " + now + "):\n\n"
        "```json\n" + pretty + "\n```\n\n"
        "Generá el reporte siguiendo el formato del system prompt. "
        "Sin relleno, números específicos, conclusiones accionables."
    )
