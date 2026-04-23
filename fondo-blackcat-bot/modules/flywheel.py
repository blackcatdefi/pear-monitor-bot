"""Flywheel pair-trade summary (/flywheel command).

The HyperLend flywheel rotated its debt from USDH → UETH on 2026-04-17.  That
makes it an implicit PAIR TRADE:
    LONG HYPE  (via kHYPE collateral)
    SHORT debt (via UETH/… borrowed)

This module summarises:
  • per-wallet LONG HYPE exposure (USD)
  • per-wallet SHORT debt-asset exposure (USD)
  • net exposure
  • HF per wallet
  • daily borrow cost (approximate — uses avg borrow rate when available)
  • HYPE/ETH ratio (current only — no 30d history without a dedicated service)
  • Bounce Tech SHORT complement (5x or any SHORT position)
  • Total SHORT ETH exposure across all sources
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from modules.bounce_tech import fetch_bounce_tech
from modules.hyperlend import fetch_all_hyperlend, fetch_reserve_rates, symbol_to_ticker
from modules.market import coingecko_prices

log = logging.getLogger(__name__)


def _fmt_usd(v: float) -> str:
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


def _price(prices: dict[str, Any], ticker: str) -> float:
    entry = prices.get(ticker) or {}
    px = entry.get("price_usd") if isinstance(entry, dict) else None
    return float(px) if px else 0.0


async def compute_flywheel() -> str:
    hl_list, bt_list, prices, rates = await asyncio.gather(
        fetch_all_hyperlend(),
        fetch_bounce_tech(),
        coingecko_prices(),
        fetch_reserve_rates(),
    )

    hype_price = _price(prices, "HYPE")
    eth_price = _price(prices, "ETH")

    lines: list[str] = []
    lines.append("🔁 FLYWHEEL PAIR TRADE")
    lines.append("─" * 40)
    if hype_price and eth_price:
        ratio = hype_price / eth_price
        lines.append(
            f"HYPE = ${hype_price:,.2f} | ETH = ${eth_price:,.2f} | HYPE/ETH = {ratio:.5f}"
        )
    elif hype_price:
        lines.append(f"HYPE = ${hype_price:,.2f}")
    else:
        lines.append("⚠ sin precio HYPE/ETH (CoinGecko offline)")
    lines.append("")

    total_long_hype_usd = 0.0
    total_short_debt_usd = 0.0
    total_short_eth_usd = 0.0  # separate bucket for ETH-ticker debts
    any_flywheel = False

    for r in hl_list:
        if r.get("status") != "ok":
            continue
        d = r["data"]
        coll_sym = d.get("collateral_symbol")
        coll_bal = d.get("collateral_balance") or 0.0
        debt_sym = d.get("debt_symbol")
        debt_bal = d.get("debt_balance") or 0.0
        coll_usd = d.get("total_collateral_usd") or 0.0
        debt_usd = d.get("total_debt_usd") or 0.0
        hf = d.get("health_factor")
        label = d.get("label", "—")
        wallet = d.get("wallet") or ""
        wshort = (wallet[:6] + "…" + wallet[-4:]) if wallet else ""

        # Only count wallets with actual flywheel structure (collateral + debt).
        if coll_usd <= 0.01:
            continue
        any_flywheel = True

        coll_ticker = symbol_to_ticker(coll_sym)
        debt_ticker = symbol_to_ticker(debt_sym)
        net = coll_usd - debt_usd

        lines.append(f"[{label}] {wshort}")
        if coll_sym and coll_bal:
            lines.append(
                f"  LONG {coll_ticker}:  {coll_bal:.4f} {coll_sym} = {_fmt_usd(coll_usd)}"
            )
            if coll_ticker == "HYPE":
                total_long_hype_usd += coll_usd
        else:
            lines.append(f"  Colateral: {_fmt_usd(coll_usd)}")

        if debt_sym and debt_bal:
            direction = "SHORT" if debt_ticker not in ("USD",) else "DEBT"
            lines.append(
                f"  {direction} {debt_ticker}: {debt_bal:.4f} {debt_sym} = {_fmt_usd(debt_usd)}"
            )
            total_short_debt_usd += debt_usd
            if debt_ticker == "ETH":
                total_short_eth_usd += debt_usd
        else:
            lines.append(f"  Borrowed: {_fmt_usd(debt_usd)}")

        lines.append(f"  Net exposure: {_fmt_usd(net)}")
        hf_str = "∞" if (hf is None or math.isinf(hf)) else f"{hf:.3f}"
        lines.append(f"  HF: {hf_str}")
        lines.append("")

    if not any_flywheel:
        lines.append("— Sin posiciones flywheel activas.")
        return "\n".join(lines)

    # ── Bounce Tech SHORT complements ──
    bt_short_eth = 0.0
    bt_positions_out: list[str] = []
    for bw in bt_list:
        if bw.get("status") != "ok":
            continue
        for p in bw.get("positions", []):
            asset = (p.get("asset") or "").upper()
            is_long = bool(p.get("is_long"))
            val = float(p.get("value_usd") or 0.0)
            direction = "LONG" if is_long else "SHORT"
            bt_positions_out.append(
                f"  {direction} {asset} {p.get('leverage','?')} — {_fmt_usd(val)}"
            )
            if not is_long and asset == "ETH":
                bt_short_eth += val

    if bt_positions_out:
        lines.append("BOUNCE TECH complements")
        lines.extend(bt_positions_out)
        lines.append("")

    # ── Consolidated totals ──
    total_short_eth_all = total_short_eth_usd + bt_short_eth
    net_all = total_long_hype_usd - total_short_debt_usd - bt_short_eth

    lines.append("CONSOLIDADO")
    lines.append(f"  Total LONG HYPE:  {_fmt_usd(total_long_hype_usd)}")
    lines.append(f"  Total SHORT debt (HL): {_fmt_usd(total_short_debt_usd)}")
    if bt_short_eth > 0:
        lines.append(f"  Total SHORT ETH (Bounce Tech): {_fmt_usd(bt_short_eth)}")
    lines.append(f"  Total SHORT ETH (all sources): {_fmt_usd(total_short_eth_all)}")
    lines.append(f"  Net flywheel exposure: {_fmt_usd(net_all)}")
    lines.append("")

    # ── Round 14: Costo borrow on-chain — TODAS las stables + UETH ───────
    # Round 13 mostraba solo las monedas que el fondo tenía como deuda.
    # Round 14 extiende a todos los stables borrow-ables (UETH + USDH +
    # USDC + USDT0 + USDe) para que BCD pueda comparar APYs y decidir
    # rotación de deuda cuando algún spread se dispara (ej. UETH >10%).
    TARGET_SYMBOLS = ["UETH", "USDH", "USDC", "USDT0", "USDe"]
    SYMBOL_ALIASES: dict[str, list[str]] = {
        "UETH":  ["UETH"],
        "USDH":  ["USDH", "USDhl", "USDh"],
        "USDC":  ["USDC", "USDC.e"],
        "USDT0": ["USDT0", "USD₮0", "USDt0", "USDT"],
        "USDe":  ["USDe", "USDE", "sUSDe"],
    }

    def _lookup_rate(rmap: dict[str, Any], target: str):
        aliases = SYMBOL_ALIASES.get(target, [target])
        for alias in aliases:
            v = rmap.get(alias)
            if v:
                return alias, v
        lowered = {a.lower() for a in aliases}
        for k, v in rmap.items():
            if k.lower() in lowered:
                return k, v
        return None, None

    rates_ok = isinstance(rates, dict) and rates.get("status") == "ok"
    lines.append("COSTO BORROW ON-CHAIN (HyperLend live — ordenado por APY)")
    if rates_ok:
        rates_map = rates.get("rates") or {}

        # Symbols the fund actually has as debt (for rotation suggestion).
        fund_debt_syms: set[str] = set()
        for r in hl_list:
            if r.get("status") == "ok":
                for d in r["data"].get("debt_assets", []) or []:
                    sym = d.get("symbol")
                    if sym:
                        fund_debt_syms.add(sym)

        # Gather APY for each target, note missing ones.
        found: list[dict[str, Any]] = []
        missing: list[str] = []
        for tgt in TARGET_SYMBOLS:
            resolved_sym, entry = _lookup_rate(rates_map, tgt)
            if not entry:
                missing.append(tgt)
                continue
            apr = float(entry.get("apr_borrow") or 0.0)
            apy = float(entry.get("apy_borrow") or 0.0)
            found.append({
                "target": tgt,
                "resolved": resolved_sym or tgt,
                "apr": apr,
                "apy": apy,
            })

        # Sort ascending by APY (cheapest first).
        found.sort(key=lambda x: x["apy"])

        if found:
            for i, row in enumerate(found):
                apr_pct = row["apr"] * 100
                apy_pct = row["apy"] * 100
                apy = row["apy"]
                # Icon = status tier
                if apy >= 0.10:
                    icon = "🚨"
                elif apy >= 0.06:
                    icon = "⚠️"
                else:
                    icon = "✅"
                # Suffix = descriptor
                if i == 0:
                    suffix = "  (🟢 más barato)"
                elif apy >= 0.10:
                    suffix = "  (>10%)"
                elif apy >= 0.06:
                    suffix = "  (>6%)"
                else:
                    suffix = ""
                name = f"{row['target']}:"
                lines.append(
                    f"  {icon} {name:<7}{apr_pct:5.2f}% APR / {apy_pct:5.2f}% APY{suffix}"
                )
        else:
            lines.append("  — ninguno de los target stables presente en el pool")

        for m in missing:
            lines.append(f"  ⚪ {m}: no disponible en pool")

        ts = rates.get("fetched_at_iso") or "—"
        lines.append(f"  (última lectura RPC: {ts}, cache 15min)")
        lines.append("")

        # ── Rotation suggestion ─────────────────────────────────────────
        # Solo sugerimos si (a) el fondo tiene UETH como deuda y (b) hay
        # alguna stable >=3 puntos porcentuales más barata. Calculamos
        # ahorro mensual sobre la deuda UETH actual en USD.
        if found and "UETH" in fund_debt_syms:
            ueth_row = next((r for r in found if r["target"] == "UETH"), None)
            if ueth_row:
                alt_rows = [r for r in found if r["target"] != "UETH"]
                if alt_rows:
                    best_alt = alt_rows[0]  # already ASC sorted
                    spread = ueth_row["apy"] - best_alt["apy"]
                    if spread >= 0.03:
                        ueth_debt_usd = 0.0
                        for r in hl_list:
                            if r.get("status") == "ok":
                                for d in r["data"].get("debt_assets", []) or []:
                                    if (d.get("symbol") or "").upper() == "UETH":
                                        if eth_price:
                                            ueth_debt_usd += (d.get("balance") or 0.0) * eth_price
                        monthly_savings = ueth_debt_usd * spread / 12 if ueth_debt_usd else 0.0
                        save_str = (
                            f"~${monthly_savings:,.0f}/mes"
                            if monthly_savings >= 1
                            else "ahorro marginal"
                        )
                        lines.append(
                            f"💡 Sugerencia: {best_alt['target']} es "
                            f"{spread*100:.2f}% más barato que UETH. Si la tesis "
                            f"direccional ETH-short no aplica o HF está alto, "
                            f"considerar flip parcial UETH→{best_alt['target']} "
                            f"para reducir carry cost {save_str}."
                        )
                        lines.append("")
    else:
        err = rates.get("error") if isinstance(rates, dict) else "n/a"
        lines.append(f"  ⚠️ Lectura RPC falló: {err}")
        lines.append("")

    lines.append(
        "Notas: el flywheel HL gana si HYPE outperforma al asset borrowed. "
        "Si la deuda es ETH-denominada, es un pair trade implícito LONG HYPE / SHORT ETH. "
        "Alerta automática dispara si UETH borrow APY > 10%."
    )
    return "\n".join(lines)
