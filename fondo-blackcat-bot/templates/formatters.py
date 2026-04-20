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


def format_quick_positions(wallets: list[dict[str, Any]],
                           hyperlend: list[dict[str, Any]] | dict[str, Any],
                           bounce_tech: list[dict[str, Any]] | None = None) -> str:
    lines: list[str] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"📊 Snapshot Fondo Black Cat — {now}")
    lines.append("")
    lines.append("PORTFOLIO HYPERLIQUID")

    # Sort wallets by equity descending
    wallets = sorted(wallets,
                     key=lambda w: (w.get("data", {}).get("account_value") or 0)
                     if w.get("status") == "ok" else 0,
                     reverse=True)

    total_eq = 0.0
    total_upnl = 0.0
    all_spot: list[dict[str, Any]] = []
    cycle_positions: list[dict[str, Any]] = []  # Trade del Ciclo BTC LONGs

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

        # Collect spot balances
        spot = d.get("spot_balances") or []
        for sb in spot:
            sb["_wallet_label"] = d["label"]
        all_spot.extend(spot)

        # Detect Trade del Ciclo BTC LONG positions
        for p in positions:
            if p.get("coin") == "BTC" and p.get("side") == "LONG":
                p["_wallet_label"] = d["label"]
                p["_wallet_short"] = short
                cycle_positions.append(p)

    lines.append(f"  TOTAL: Equity {_fmt_usd(total_eq)} | UPnL {_fmt_usd(total_upnl)}")

    # ── Trade del Ciclo (BTC LONG 3x) ──
    if cycle_positions:
        lines.append("")
        lines.append("TRADE DEL CICLO (BTC LONG 3x)")
        for cp in cycle_positions:
            entry = cp.get("entry_px", 0)
            mark = cp.get("mark_px") or cp.get("position_value", 0)
            upnl_c = cp.get("unrealized_pnl", 0)
            size = cp.get("size", 0)
            margin = cp.get("margin_used", 0)
            liq = cp.get("liq_px", 0)
            leverage = cp.get("leverage", "?")
            wallet_label = cp.get("_wallet_label", "?")
            wallet_short = cp.get("_wallet_short", "")

            lines.append(f"  [{wallet_label}] {wallet_short}")
            lines.append(f"    Entry: ${entry:,.0f} | Mark: ${mark:,.0f} | Leverage: {leverage}x")
            lines.append(f"    Size: {size:.4f} BTC | Margin: {_fmt_usd(margin)}")
            lines.append(f"    UPnL: {_fmt_usd(upnl_c)}")
            if liq:
                lines.append(f"    Liq: ${liq:,.0f}")

            # DCA status
            if mark:
                btc_current = mark
            elif entry:
                btc_current = entry
            else:
                btc_current = 0

            if btc_current > 0:
                dca_levels = [
                    (70_000, "DCA Add 1", "$500"),
                    (63_000, "DCA Add 2", "$750"),
                    (55_000, "DCA Add 3", "$1,000"),
                ]
                pending = [f"${l[0]:,} ({l[1]})" for l in dca_levels if btc_current > l[0]]
                triggered = [f"${l[0]:,} ({l[1]})" for l in dca_levels if btc_current <= l[0]]
                if triggered:
                    lines.append(f"    ⚠️ DCA triggered: {', '.join(triggered)}")
                if pending:
                    lines.append(f"    Pending DCA: {', '.join(pending)}")
                if btc_current >= 150_000:
                    lines.append("    🎯 TP ZONE — evaluar cierre parcial")

    # ── Spot token balances (kHYPE, PEAR, etc.) ──
    if all_spot:
        lines.append("")
        lines.append("SPOT TOKENS")
        # Group by coin
        by_coin: dict[str, list[dict[str, Any]]] = {}
        for sb in all_spot:
            coin = sb.get("coin", "?")
            by_coin.setdefault(coin, []).append(sb)

        for coin, entries in sorted(by_coin.items()):
            total_amount = sum(e.get("total", 0) for e in entries)
            total_entry = sum(e.get("entry_ntl", 0) for e in entries)
            if len(entries) == 1:
                wallet_label = entries[0].get("_wallet_label", "")
                lines.append(f"  • {coin}: {total_amount:.4f} (cost basis: {_fmt_usd(total_entry)}) [{wallet_label}]")
            else:
                lines.append(f"  • {coin}: {total_amount:.4f} total (cost basis: {_fmt_usd(total_entry)})")
                for e in entries:
                    lines.append(f"      {e.get('_wallet_label','?')}: {e.get('total',0):.4f}")

    # HyperLend section — supports both list (new) and dict (legacy) format
    lines.append("")
    lines.append("HYPERLEND")
    hl_list = hyperlend if isinstance(hyperlend, list) else [hyperlend]

    # Sort HyperLend by collateral descending
    hl_list = sorted(hl_list,
                     key=lambda hl: (hl.get("data", {}).get("total_collateral_usd") or 0)
                     if hl.get("status") == "ok" else 0,
                     reverse=True)

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

            # Colateral — show asset symbol + balance when available
            coll_sym = h.get("collateral_symbol")
            coll_bal = h.get("collateral_balance") or 0.0
            if coll_sym and coll_bal:
                lines.append(
                    f"    Colateral: {coll_bal:.4f} {coll_sym} ({_fmt_usd(h.get('total_collateral_usd'))})"
                )
            else:
                lines.append(f"    Colateral: {_fmt_usd(h.get('total_collateral_usd'))}")

            # Borrowed — show actual asset symbol (UETH, USDH, ...) + balance
            debt_sym = h.get("debt_symbol")
            debt_bal = h.get("debt_balance") or 0.0
            if debt_sym and debt_bal:
                lines.append(
                    f"    Borrowed: {debt_bal:.4f} {debt_sym} ({_fmt_usd(h.get('total_debt_usd'))})"
                )
            else:
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

    # Sort HyperLend by collateral descending
    hl_list = sorted(hl_list,
                     key=lambda hl: (hl.get("data", {}).get("total_collateral_usd") or 0)
                     if hl.get("status") == "ok" else 0,
                     reverse=True)

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
        coll_sym = h.get("collateral_symbol")
        coll_bal = h.get("collateral_balance") or 0.0
        debt_sym = h.get("debt_symbol")
        debt_bal = h.get("debt_balance") or 0.0

        coll_str = (
            f"{coll_bal:.4f} {coll_sym} ({_fmt_usd(h.get('total_collateral_usd'))})"
            if coll_sym and coll_bal
            else _fmt_usd(h.get("total_collateral_usd"))
        )

        debt_str = (
            f"{debt_bal:.4f} {debt_sym} ({_fmt_usd(h.get('total_debt_usd'))})"
            if debt_sym and debt_bal
            else _fmt_usd(h.get("total_debt_usd"))
        )

        parts.append(
            f"{icon} [{label}] HF: {_fmt_hf(hf)}\n"
            f"  Colateral: {coll_str}\n"
            f"  Borrowed: {debt_str}\n"
            f"  Available: {_fmt_usd(h.get('available_borrows_usd'))}"
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

    # Extract bounce_tech from telegram_intel if not passed separately
    bt = bounce_tech
    if not bt and isinstance(telegram_intel, dict) and "bounce_tech" in telegram_intel:
        bt = telegram_intel.pop("bounce_tech", None)

    blob = {
        "timestamp_utc": now,
        "portfolio": portfolio or [],
        "hyperlend": hyperlend or {},
        "market": market or {},
        "unlocks": unlocks or {},
        "telegram_intel": telegram_intel or {},
        "bounce_tech": bt or [],
    }
    pretty = json.dumps(blob, ensure_ascii=False, indent=2, default=str)
    return (
        "DATA CRUDA (timestamp UTC " + now + "):\n\n"
        "```json\n" + pretty + "\n```\n\n"
        "Generá el reporte siguiendo el formato del system prompt. "
        "Sin relleno, números específicos, conclusiones accionables."
    )

