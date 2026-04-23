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

    # ── Round 13: Costo real borrow on-chain ─────────────────────────────
    # Lee currentVariableBorrowRate de cada reserve via getReserveData().
    # UETH es la deuda real del flywheel post-17 abr; mostramos TODO lo que
    # el fondo tiene borroweado hoy + UETH aunque no esté borroweado (para
    # baseline).
    rates_ok = isinstance(rates, dict) and rates.get("status") == "ok"
    lines.append("COSTO BORROW ON-CHAIN (HyperLend live)")
    if rates_ok:
        rates_map = rates.get("rates") or {}
        # Buscar símbolos: los que el fondo efectivamente borrowea +
        # siempre UETH como referencia del flywheel.
        fund_debt_syms: set[str] = set()
        for r in hl_list:
            if r.get("status") == "ok":
                for d in r["data"].get("debt_assets", []) or []:
                    sym = d.get("symbol")
                    if sym:
                        fund_debt_syms.add(sym)
        # Fuerza UETH para monitoreo aun si actualmente no hay deuda.
        fund_debt_syms.add("UETH")
        any_row = False
        for sym in sorted(fund_debt_syms):
            entry = rates_map.get(sym)
            if not entry:
                # case-insensitive lookup
                for k, v in rates_map.items():
                    if k.lower() == sym.lower():
                        entry = v
                        break
            if not entry:
                lines.append(f"  {sym}: — (no en reserves list)")
                continue
            apr = float(entry.get("apr_borrow") or 0.0)
            apy = float(entry.get("apy_borrow") or 0.0)
            tag = ""
            if apy >= 0.10:
                tag = " 🚨 >10%"
            elif apy >= 0.06:
                tag = " ⚠️ >6% (threshold flywheel)"
            lines.append(f"  {sym} borrow: {apr*100:.2f}% APR / {apy*100:.2f}% APY{tag}")
            any_row = True
        ts = rates.get("fetched_at_iso") or "—"
        lines.append(f"  (última lectura RPC: {ts}, cache 15min)")
        if not any_row:
            lines.append("  — sin reserves para reportar")
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
