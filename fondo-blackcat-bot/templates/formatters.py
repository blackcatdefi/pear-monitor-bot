"""Formatters for quick replies (no Claude needed)."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any


def _fmt_usd(v: float | None) -> str:
    if v is None:
        return "â"
    if math.isinf(v):
        return "â"
    sign = "-" if v < 0 else ""
    v = abs(v)
    if v >= 1_000_000:
        return f"{sign}${v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"{sign}${v/1_000:.1f}K"
    return f"{sign}${v:.2f}"


def _fmt_hf(v: float | None) -> str:
    if v is None:
        return "â"
    if math.isinf(v):
        return "â (sin deuda)"
    return f"{v:.3f}"


def _estimate_spot_usd(spot_balances: list[dict[str, Any]]) -> float:
    """Estimate USD value of spot token balances."""
    total = 0.0
    for sb in spot_balances:
        coin = (sb.get("coin") or "").upper()
        amount = sb.get("total", 0) or 0
        if coin in ("USDC", "USDH", "USDT", "DAI"):
            total += amount
        else:
            # Use entry_ntl (cost basis) as rough USD estimate
            entry_ntl = sb.get("entry_ntl", 0) or 0
            if entry_ntl > 0:
                total += entry_ntl
    return total


def format_quick_positions(wallets: list[dict[str, Any]],
                           hyperlend: list[dict[str, Any]] | dict[str, Any],
                           bounce_tech: list[dict[str, Any]] | None = None,
                           recent_fills: list[dict[str, Any]] | None = None) -> str:
    lines: list[str] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"ð Snapshot Fondo Black Cat â {now}")
    lines.append("")

    # ââ Build HyperLend collateral map: wallet_addr (lower) â data ââ
    hl_list = hyperlend if isinstance(hyperlend, list) else [hyperlend]
    hl_by_wallet: dict[str, dict[str, float]] = {}
    for hl in hl_list:
        if hl.get("status") == "ok":
            h = hl["data"]
            addr = (h.get("wallet") or "").lower()
            coll = h.get("total_collateral_usd", 0.0) or 0.0
            debt = h.get("total_debt_usd", 0.0) or 0.0
            if addr:
                hl_by_wallet[addr] = {
                    "collateral_usd": coll,
                    "debt_usd": debt,
                    "net_usd": coll - debt,
                }

    # ââ Compute total capital per wallet (perp + spot + HyperLend collateral) ââ
    for w in wallets:
        if w.get("status") != "ok":
            continue
        d = w["data"]
        wallet_addr = (d.get("wallet") or "").lower()
        perp_eq = d.get("account_value") or 0.0
        spot_usd = _estimate_spot_usd(d.get("spot_balances") or [])
        hl_data = hl_by_wallet.get(wallet_addr, {})
        hl_coll = hl_data.get("collateral_usd", 0.0)
        hl_debt = hl_data.get("debt_usd", 0.0)
        total_capital = perp_eq + spot_usd + hl_coll
        d["_total_capital"] = total_capital
        d["_perp_equity"] = perp_eq
        d["_spot_usd"] = spot_usd
        d["_hl_collateral_usd"] = hl_coll
        d["_hl_debt_usd"] = hl_debt

    # Sort wallets by total capital descending (dynamic ordering)
    wallets = sorted(wallets,
                     key=lambda w: (w.get("data", {}).get("_total_capital") or 0)
                     if w.get("status") == "ok" else 0,
                     reverse=True)

    # Dynamic wallet labels based on capital rank
    RANK_LABELS = ["PRINCIPAL", "SECUNDARIA"]

    lines.append("PORTFOLIO CONSOLIDADO")

    total_fund_capital = 0.0
    total_upnl = 0.0
    all_spot: list[dict[str, Any]] = []
    cycle_positions: list[dict[str, Any]] = []
    wallet_rank = 0

    for w in wallets:
        if w.get("status") != "ok":
            label = w.get("label", "?")
            short = (w.get("wallet") or "")[:6] + "â¦"
            error_msg = w.get("error", "error")
            if w.get("stale"):
                lines.append(f"  â¢ {label} ({short}): â ï¸ {error_msg} (usando cache)")
            else:
                lines.append(f"  â¢ {label} ({short}): â {error_msg}")
            continue
        d = w["data"]
        short = d["wallet"][:6] + "â¦" + d["wallet"][-4:]
        tc = d.get("_total_capital") or 0.0
        perp_eq = d.get("_perp_equity") or 0.0
        spot_usd = d.get("_spot_usd") or 0.0
        hl_coll = d.get("_hl_collateral_usd") or 0.0
        hl_debt = d.get("_hl_debt_usd") or 0.0
        upnl_val = d.get("unrealized_pnl_total") or 0.0
        total_fund_capital += tc
        total_upnl += upnl_val

        # Dynamic label: PRINCIPAL / SECUNDARIA / original label
        if wallet_rank < len(RANK_LABELS) and tc > 0.01:
            display_label = f"ð° {RANK_LABELS[wallet_rank]}"
        else:
            display_label = d["label"]
        wallet_rank += 1

        positions = d.get("positions") or []
        if positions:
            pos_summary = ", ".join(f"{p['side']} {p['coin']}" for p in positions[:5])
        else:
            pos_summary = "sin posiciones perp"

        lines.append(f"  â¢ {display_label} {short}")
        lines.append(f"    Capital Total: {_fmt_usd(tc)}")

        # Breakdown only if multiple components have value
        parts: list[str] = []
        if perp_eq > 0.01:
            parts.append(f"Perp {_fmt_usd(perp_eq)}")
        if spot_usd > 0.01:
            parts.append(f"Spot {_fmt_usd(spot_usd)}")
        if hl_coll > 0.01:
            parts.append(f"HL Coll {_fmt_usd(hl_coll)}")
        if hl_debt > 0.01:
            parts.append(f"HL Debt -{_fmt_usd(hl_debt)}")
        if len(parts) > 0:
            lines.append(f"    ({' | '.join(parts)})")
        if upnl_val != 0:
            lines.append(f"    UPnL: {_fmt_usd(upnl_val)}")
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

    lines.append(f"  TOTAL FONDO: Capital {_fmt_usd(total_fund_capital)} | UPnL {_fmt_usd(total_upnl)}")

    # ââ Trade del Ciclo (BTC LONG 3x) â always visible ââ
    lines.append("")
    lines.append("TRADE DEL CICLO (BTC LONG 3x)")
    if cycle_positions:
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
                    lines.append(f"    â ï¸ DCA triggered: {', '.join(triggered)}")
                if pending:
                    lines.append(f"    Pending DCA: {', '.join(pending)}")
                if btc_current >= 150_000:
                    lines.append("    ð¯ TP ZONE â evaluar cierre parcial")
    else:
        lines.append("  Status: PENDIENTE â sin posiciÃ³n abierta")
        lines.append("  DCA plan activo: $70K / $63K / $55K")

    # ââ Spot token balances (kHYPE, PEAR, etc.) ââ
    if all_spot:
        lines.append("")
        lines.append("SPOT TOKENS")
        by_coin: dict[str, list[dict[str, Any]]] = {}
        for sb in all_spot:
            coin = sb.get("coin", "?")
            by_coin.setdefault(coin, []).append(sb)

        for coin, entries in sorted(by_coin.items()):
            total_amount = sum(e.get("total", 0) for e in entries)
            total_entry = sum(e.get("entry_ntl", 0) for e in entries)

            if coin == "USDC":
                cost_basis_display = f"${total_amount:,.2f}"
            else:
                cost_basis_display = _fmt_usd(total_entry)

            if len(entries) == 1:
                wallet_label = entries[0].get("_wallet_label", "")
                lines.append(f"  â¢ {coin}: {total_amount:.4f} (cost basis: {cost_basis_display}) [{wallet_label}]")
            else:
                lines.append(f"  â¢ {coin}: {total_amount:.4f} total (cost basis: {cost_basis_display})")
                for e in entries:
                    lines.append(f"      {e.get('_wallet_label','?')}: {e.get('total',0):.4f}")

    # HyperLend section â detailed view with HF, collateral breakdown, debt
    lines.append("")
    lines.append("HYPERLEND")

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
            label = h.get("label") or hl.get("label") or "â"
            wallet_short = (h.get("wallet") or "")[:6]
            if wallet_short:
                wallet_short = wallet_short + "â¦" + (h.get("wallet") or "")[-4:]
            header = f"  [{label}]" + (f" {wallet_short}" if wallet_short else "")
            lines.append(header)
            lines.append(f"    HF: {_fmt_hf(h.get('health_factor'))}")

            coll_sym = h.get("collateral_symbol")
            coll_bal = h.get("collateral_balance") or 0.0
            if coll_sym and coll_bal:
                lines.append(
                    f"    Colateral: {coll_bal:.4f} {coll_sym} ({_fmt_usd(h.get('total_collateral_usd'))})"
                )
            else:
                lines.append(f"    Colateral: {_fmt_usd(h.get('total_collateral_usd'))}")

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
            lines.append(f"  â {hl.get('error','error')}")

    # ââ Bounce Tech leveraged tokens ââ
    if bounce_tech is not None:
        bt_positions = []
        for bw in bounce_tech:
            if bw.get("status") != "ok":
                continue
            for p in bw.get("positions", []):
                bt_positions.append(p)

        lines.append("")
        lines.append("BOUNCE TECH (Leveraged Tokens)")
        if bt_positions:
            bt_total = 0.0
            for p in bt_positions:
                direction = "ð¢ LONG" if p.get("is_long") else "ð´ SHORT"
                asset = p.get("asset", "?")
                lev = p.get("leverage", "?")
                val = p.get("value_usd", 0.0)
                bt_total += val
                lines.append(f"  {direction} {asset} {lev} â {_fmt_usd(val)}")
            lines.append(f"  Total BT: {_fmt_usd(bt_total)}")
        else:
            lines.append("  INACTIVA â sin posiciones abiertas")

    # ââ Trades cerrados Ãºltimas 24h ââ
    if recent_fills:
        lines.append("")
        lines.append("TRADES CERRADOS (24h)")
        total_pnl = 0.0
        total_fees = 0.0
        for f in recent_fills:
            coin = f.get("coin", "?")
            side = f.get("side", "?").upper()
            sz = f.get("sz", 0)
            px = f.get("px", 0)
            pnl = f.get("closedPnl", 0)
            fee = f.get("fee", 0)
            direction = f.get("dir", "")
            label = f.get("_wallet_label", "")
            total_pnl += pnl
            total_fees += fee
            icon = "ð¢" if pnl >= 0 else "ð´"
            ts = f.get("time")
            time_str = ""
            if ts:
                from datetime import datetime as _dt, timezone as _tz
                time_str = _dt.fromtimestamp(ts / 1000, tz=_tz.utc).strftime("%H:%M")
            lines.append(f"  {icon} {side} {coin} {sz:.4f} @ ${px:,.2f} | PnL: {_fmt_usd(pnl)} | {time_str} [{label}]")
        lines.append(f"  TOTAL PnL: {_fmt_usd(total_pnl)} | Fees: {_fmt_usd(total_fees)} | Net: {_fmt_usd(total_pnl - total_fees)}")

    return "\n".join(lines)


def format_hf(hyperlend: list[dict[str, Any]] | dict[str, Any]) -> str:
    """Format HF for /hf command â supports both list and single dict."""
    hl_list = hyperlend if isinstance(hyperlend, list) else [hyperlend]

    hl_list = sorted(hl_list,
                     key=lambda hl: (hl.get("data", {}).get("total_collateral_usd") or 0)
                     if hl.get("status") == "ok" else 0,
                     reverse=True)

    parts: list[str] = []
    for hl in hl_list:
        if hl.get("status") != "ok":
            parts.append(f"â HyperLend: {hl.get('error','error')}")
            continue
        h = hl["data"]
        coll = h.get("total_collateral_usd", 0.0) or 0.0
        if coll < 0.01:
            continue

        hf = h.get("health_factor")
        icon = "ð¢"
        if hf is not None and not math.isinf(hf):
            if hf < 1.10:
                icon = "ð¨"
            elif hf < 1.20:
                icon = "â ï¸"

        label = h.get("label") or hl.get("label") or "â"
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

    return "\n".join(parts) if parts else "â Sin posiciones HyperLend activas"


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
        "Genera el reporte siguiendo el formato del system prompt. "
        "Sin relleno, nÃºmeros especÃ­ficos, conclusiones accionables."
    )
