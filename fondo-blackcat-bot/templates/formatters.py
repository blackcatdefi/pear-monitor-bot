"""Formatters for quick replies (no Claude needed)."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from fund_state import (
    ALT_SHORT_BLEED_WALLETS,
    BASKET_STATUS,
    BLOFIN_BALANCE_AVAILABLE,
    TRADE_DEL_CICLO_BLOFIN_BALANCE_USD,
    TRADE_DEL_CICLO_LAST_CLOSE,
    TRADE_DEL_CICLO_LAST_ENTRY,
    TRADE_DEL_CICLO_LAST_UPDATE,
    TRADE_DEL_CICLO_LEVERAGE,
    TRADE_DEL_CICLO_PLATFORM,
    TRADE_DEL_CICLO_PNL_REALIZED,
    TRADE_DEL_CICLO_STATUS,
    classify_fill,
)


def _is_alt_short_wallet(wallet_addr: str) -> bool:
    addr = (wallet_addr or "").lower()
    return any(prefix.lower() in addr for prefix in ALT_SHORT_BLEED_WALLETS)


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


def _estimate_spot_usd(spot_balances: list[dict[str, Any]],
                       perp_account_value: float = 0.0) -> float:
    """Estimate USD value of spot tokens, conditionally excluding USDC.

    ================================================================
    CRITICAL: HYPERLIQUID UNIFIED ACCOUNT — DO NOT REMOVE
    ================================================================
    HyperLiquid unified spot+perp into a single account. The USDC
    that backs an open basket appears in BOTH:
      * clearinghouseState.marginSummary.accountValue  (perp equity)
      * spotClearinghouseState.balances[USDC].total    (spot USDC)

    Per BCD's directive (2026-04-28):
      - IF wallet has an ACTIVE perp position (perp_account_value > 0.01):
        skip USDC from spot — it's already inside accountValue.
      - IF wallet has NO active perp: count USDC from spot — it's free
        capital sitting idle (basket just closed, awaiting next trade).

    Bug history: wallet 0xc7ae was reported as $11.6K (Perp $5.8K +
    Spot $5.8K) when its real capital was $5.8K — a 2x duplication
    of basket margin under Unified Account.
    ================================================================
    """
    has_active_perp = perp_account_value > 0.01
    total = 0.0
    for sb in spot_balances:
        coin = (sb.get("coin") or "").upper()
        amount = sb.get("total", 0) or 0
        # CRITICAL: only skip USDC when an active perp exists (then it's
        # already in accountValue under Unified Account).
        if coin == "USDC":
            if has_active_perp:
                continue
            total += amount
            continue
        if coin in ("USDH", "USDT", "USDT0", "DAI"):
            total += amount
        else:
            # Use entry_ntl (cost basis) as rough USD estimate
            entry_ntl = sb.get("entry_ntl", 0) or 0
            if entry_ntl > 0:
                total += entry_ntl
    return total


def _current_usd_value(coin: str, amount: float, entry_ntl: float,
                       prices: dict[str, Any] | None) -> float:
    """Best-effort current USD valuation of a spot balance.

    Order of preference:
      1. Stablecoins (USDC/USDH/USDT0/DAI/USDT) → amount 1:1.
      2. Current price from market.prices[COIN].price_usd when available.
      3. Entry notional (cost basis) as last-resort proxy.
    """
    c = (coin or "").upper()
    if c in {"USDC", "USDH", "USDT", "USDT0", "DAI"}:
        return float(amount or 0)
    if prices:
        # market dict shape: {prices: {BTC: {price_usd, ...}}}
        # Handle kHYPE → use HYPE price as proxy (kHYPE pegs loosely to HYPE)
        lookup = c.removeprefix("K") if c.startswith("K") else c
        entry = (prices.get(lookup) or prices.get(c) or {})
        px = entry.get("price_usd")
        if px and amount:
            return float(amount) * float(px)
    return float(entry_ntl or 0)


def _fmt_cycle_upnl_block(lines_out: list[str], market: dict[str, Any] | None) -> None:
    """Append Trade del Ciclo block — handles OPEN (UPnL estimate) and CLOSED (realized)."""
    status = (TRADE_DEL_CICLO_STATUS or "OPEN").upper()
    lines_out.append("")
    lines_out.append(
        f"TRADE DEL CICLO (BTC LONG {TRADE_DEL_CICLO_LEVERAGE}x — {TRADE_DEL_CICLO_PLATFORM.upper()}) · {status}"
    )
    lines_out.append("  ⚠️ Blofin no expone API pública — el bot NO lee esta posición en tiempo real.")

    if status == "CLOSED":
        lines_out.append(f"  ✅ CERRADO: {TRADE_DEL_CICLO_LAST_CLOSE}")
        lines_out.append(
            f"  PnL realizado: {_fmt_usd(TRADE_DEL_CICLO_PNL_REALIZED)} "
            f"(desde entry ${TRADE_DEL_CICLO_LAST_ENTRY:,.2f})"
        )
        lines_out.append(
            f"  Balance Blofin disponible: {_fmt_usd(BLOFIN_BALANCE_AVAILABLE)} USDT "
            "(copy-trading descopiado, esperando nueva entrada)"
        )
        lines_out.append(
            "  Próxima entrada: pendiente orden manual BCD. "
            "Edit fund_state.py → STATUS=OPEN + nuevo LAST_ENTRY al reabrir."
        )
        return

    # STATUS == OPEN → render UPnL estimate
    lines_out.append(f"  Último entry confirmado por BCD: ${TRADE_DEL_CICLO_LAST_ENTRY:,.2f}")
    lines_out.append(f"  Balance Blofin (manual + copy-trading): {_fmt_usd(TRADE_DEL_CICLO_BLOFIN_BALANCE_USD)}")
    lines_out.append(f"  Última lectura manual: {TRADE_DEL_CICLO_LAST_UPDATE}")

    btc_price: float | None = None
    if isinstance(market, dict):
        prices = (market.get("data") or {}).get("prices") or market.get("prices") or {}
        btc_entry = prices.get("BTC") or {}
        p = btc_entry.get("price_usd")
        if p:
            try:
                btc_price = float(p)
            except (TypeError, ValueError):
                btc_price = None

    if btc_price:
        pct_move = (btc_price - TRADE_DEL_CICLO_LAST_ENTRY) / TRADE_DEL_CICLO_LAST_ENTRY
        pct_pnl = pct_move * TRADE_DEL_CICLO_LEVERAGE
        assumed_margin = TRADE_DEL_CICLO_BLOFIN_BALANCE_USD * 0.5
        est_pnl_usd = assumed_margin * pct_pnl
        lines_out.append(
            f"  BTC actual: ${btc_price:,.2f} | Movimiento subyacente: {pct_move*100:+.2f}%"
        )
        lines_out.append(
            f"  PnL estimado ({TRADE_DEL_CICLO_LEVERAGE}x sobre ~${assumed_margin:,.0f} margen): "
            f"{pct_pnl*100:+.2f}% → {_fmt_usd(est_pnl_usd)}"
        )
        lines_out.append(
            "  ⚠️ Estimación. No confirmado por API Blofin — BCD debe confirmar balance real."
        )
    else:
        lines_out.append("  UPnL: no calculable (BTC price feed no disponible).")

    lines_out.append("  DCA plan: $70K Add 1 ($500) / $63K Add 2 ($750) / $55K Add 3 ($1,000)")
    lines_out.append("  SL individual = liq price (único SL). TP manual en zona $130K–$150K.")


def format_quick_positions(wallets: list[dict[str, Any]],
                           hyperlend: list[dict[str, Any]] | dict[str, Any],
                           bounce_tech: list[dict[str, Any]] | None = None,
                           recent_fills: list[dict[str, Any]] | None = None,
                           market: dict[str, Any] | None = None) -> str:
    lines: list[str] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"📊 Snapshot Fondo Black Cat — {now}")
    lines.append("")

    # ─── R-DASH: NET CAPITAL banner (single-source-of-truth) ────────────
    # Compute totals from the same wallet+HL data so the dashboard and
    # /reporte agree on the headline number. UPnL is NOT added separately
    # (already inside perp accountValue under Hyperliquid Unified Account).
    try:
        _hl_list = hyperlend if isinstance(hyperlend, list) else [hyperlend]
        _hl_coll_total = 0.0
        _hl_debt_total = 0.0
        for _hl in _hl_list:
            if isinstance(_hl, dict) and _hl.get("status") == "ok":
                _hd = _hl.get("data") or {}
                try:
                    _hl_coll_total += float(_hd.get("total_collateral_usd") or 0.0)
                except (TypeError, ValueError):
                    pass
                try:
                    _hl_debt_total += float(_hd.get("total_debt_usd") or 0.0)
                except (TypeError, ValueError):
                    pass

        _perp_total = 0.0
        _spot_non_usdc_total = 0.0
        _upnl_total = 0.0
        for _w in wallets:
            if not isinstance(_w, dict) or _w.get("status") != "ok":
                continue
            _d = _w.get("data") or {}
            try:
                _pe = float(_d.get("account_value") or 0.0)
            except (TypeError, ValueError):
                _pe = 0.0
            _perp_total += _pe
            try:
                _spot_non_usdc_total += float(
                    _estimate_spot_usd(_d.get("spot_balances") or [], _pe)
                )
            except Exception:  # noqa: BLE001
                pass
            try:
                _upnl_total += float(_d.get("unrealized_pnl_total") or 0.0)
            except (TypeError, ValueError):
                pass

        from auto.capital_calc import compute_net_capital, format_net_capital_telegram
        _net = compute_net_capital({
            "hl_collateral_total": _hl_coll_total,
            "hl_debt_total": _hl_debt_total,
            "perp_equity_total": _perp_total,
            "spot_usd_total": _spot_non_usdc_total,
            "upnl_perp_total": _upnl_total,
        })
        lines.append(format_net_capital_telegram(_net))
        lines.append("")
    except Exception:  # noqa: BLE001
        # Never break the formatter — capital banner is best-effort.
        pass

    # ── Build HyperLend collateral map: wallet_addr (lower) → data ──
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

    # ── Compute total capital per wallet (perp + spot + HyperLend collateral) ──
    for w in wallets:
        if w.get("status") != "ok":
            continue
        d = w["data"]
        wallet_addr = (d.get("wallet") or "").lower()
        perp_eq = d.get("account_value") or 0.0
        spot_usd = _estimate_spot_usd(d.get("spot_balances") or [], perp_eq)
        hl_data = hl_by_wallet.get(wallet_addr, {})
        hl_coll = hl_data.get("collateral_usd", 0.0)
        hl_debt = hl_data.get("debt_usd", 0.0)
        total_capital = perp_eq + spot_usd + hl_coll
        d["_total_capital"] = total_capital
        d["_perp_equity"] = perp_eq
        d["_spot_usd"] = spot_usd
        d["_hl_collateral_usd"] = hl_coll
        d["_hl_debt_usd"] = hl_debt
        d["_margin_used"] = d.get("total_margin_used") or 0.0
        d["_withdrawable"] = d.get("withdrawable") or 0.0
        d["_total_ntl_pos"] = d.get("total_ntl_pos") or 0.0

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
            short = (w.get("wallet") or "")[:6] + "…"
            error_msg = w.get("error", "error")
            if w.get("stale"):
                lines.append(f"  • {label} ({short}): ⚠️ {error_msg} (usando cache)")
            else:
                lines.append(f"  • {label} ({short}): ❌ {error_msg}")
            continue
        d = w["data"]
        short = d["wallet"][:6] + "…" + d["wallet"][-4:]
        tc = d.get("_total_capital") or 0.0
        perp_eq = d.get("_perp_equity") or 0.0
        spot_usd = d.get("_spot_usd") or 0.0
        hl_coll = d.get("_hl_collateral_usd") or 0.0
        hl_debt = d.get("_hl_debt_usd") or 0.0
        margin_used = d.get("_margin_used") or 0.0
        withdrawable = d.get("_withdrawable") or 0.0
        ntl_pos = d.get("_total_ntl_pos") or 0.0
        upnl_val = d.get("unrealized_pnl_total") or 0.0
        total_fund_capital += tc
        total_upnl += upnl_val

        # Dynamic label: PRINCIPAL / SECUNDARIA / original label
        if wallet_rank < len(RANK_LABELS) and tc > 0.01:
            display_label = f"💰 {RANK_LABELS[wallet_rank]}"
        else:
            display_label = d["label"]
        wallet_rank += 1

        positions = d.get("positions") or []
        if positions:
            pos_summary = ", ".join(f"{p['side']} {p['coin']}" for p in positions[:5])
        elif _is_alt_short_wallet(d.get("wallet", "")) and not BASKET_STATUS.get("active"):
            # Wallet históricamente del basket Alt Short Bleed, hoy IDLE.
            last = BASKET_STATUS.get("last_basket", "?")
            net = BASKET_STATUS.get("last_basket_result_net_usd", 0.0)
            nxt = BASKET_STATUS.get("next_basket", "pending")
            pos_summary = (
                f"IDLE (basket {last} cerrado NET {_fmt_usd(net)}, {nxt}). "
                "Dust residual — no posición activa."
            )
        else:
            pos_summary = "sin posiciones perp"

        lines.append(f"  • {display_label} {short}")
        lines.append(f"    Capital Total: {_fmt_usd(tc)}")

        # Breakdown — Account Value already includes any USDC sitting in spot
        # under HyperLiquid Unified Account, so "Spot" here only ever shows
        # NON-USDC tokens (HYPE, kHYPE, etc.). See portfolio_snapshot.py for
        # the full unified-account note.
        parts: list[str] = []
        if perp_eq > 0.01:
            parts.append(f"Account Value {_fmt_usd(perp_eq)}")
        if spot_usd > 0.01:
            parts.append(f"Spot non-USDC {_fmt_usd(spot_usd)}")
        if hl_coll > 0.01:
            parts.append(f"HL Coll {_fmt_usd(hl_coll)}")
        if hl_debt > 0.01:
            parts.append(f"HL Debt -{_fmt_usd(hl_debt)}")
        if len(parts) > 0:
            lines.append(f"    ({' | '.join(parts)})")
        if upnl_val != 0:
            lines.append(f"    UPnL: {_fmt_usd(upnl_val)}")
        # Show margin / withdrawable / leverage when there's an active perp position.
        if ntl_pos > 50 or margin_used > 50:
            lev = (ntl_pos / perp_eq) if perp_eq > 0.01 else 0.0
            lines.append(
                f"    Margin used: {_fmt_usd(margin_used)} | "
                f"Withdrawable: {_fmt_usd(withdrawable)} | "
                f"Notional: {_fmt_usd(ntl_pos)} (~{lev:.2f}x)"
            )
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

    # ── Trade del Ciclo (BTC LONG) — vive en Blofin, NO en Hyperliquid ──
    _fmt_cycle_upnl_block(lines, market)

    # ── Spot token balances (kHYPE, PEAR, etc.) con DUST threshold ──
    if all_spot:
        # Current-price map for USD valuation (kHYPE/HYPE/PEAR/etc.)
        price_map: dict[str, Any] = {}
        if isinstance(market, dict):
            price_map = (market.get("data") or {}).get("prices") or market.get("prices") or {}

        by_coin: dict[str, list[dict[str, Any]]] = {}
        for sb in all_spot:
            coin = sb.get("coin", "?")
            by_coin.setdefault(coin, []).append(sb)

        # Compute per-coin total USD and split into "real" vs "dust" (<$50)
        DUST_THRESHOLD_USD = 50.0
        real_coins: list[tuple[str, list[dict[str, Any]], float, float]] = []  # (coin, entries, amt, usd)
        dust_coins: list[tuple[str, float, float]] = []  # (coin, amount, usd_value)

        for coin, entries in by_coin.items():
            total_amount = sum(e.get("total", 0) for e in entries)
            total_entry_ntl = sum(e.get("entry_ntl", 0) for e in entries)
            # Current USD valuation (sum per-wallet to use each entry_ntl correctly)
            total_usd_now = 0.0
            for e in entries:
                total_usd_now += _current_usd_value(
                    coin,
                    e.get("total", 0) or 0,
                    e.get("entry_ntl", 0) or 0,
                    price_map,
                )
            if total_usd_now >= DUST_THRESHOLD_USD:
                real_coins.append((coin, entries, total_amount, total_usd_now))
            else:
                dust_coins.append((coin, total_amount, total_usd_now))

        if real_coins or dust_coins:
            lines.append("")
            lines.append("SPOT TOKENS")

        # Render real positions (per-wallet breakdown when multiple wallets hold)
        for coin, entries, total_amount, total_usd_now in sorted(real_coins, key=lambda x: -x[3]):
            total_entry = sum(e.get("entry_ntl", 0) for e in entries)
            if coin == "USDC":
                cost_basis_display = f"${total_amount:,.2f}"
            else:
                cost_basis_display = _fmt_usd(total_entry)

            # Unified Account note: when a wallet listed has an active perp,
            # its USDC shown here is already inside Account Value. The
            # capital math above already handles the dedup per-wallet, so
            # the SPOT TOKENS section is purely informational. We only flag
            # USDC entries when ANY of the holding wallets has an active
            # perp — those are the ones a reader could mistakenly re-add.
            usdc_note = ""
            if coin == "USDC":
                # Check per-wallet via the wallets list scoped above
                holding_addrs_with_perp = [
                    w for w in wallets
                    if w.get("status") == "ok"
                    and (w.get("data", {}).get("_perp_equity") or 0.0) > 0.01
                    and any(
                        (e.get("_wallet_label") == w.get("data", {}).get("label"))
                        for e in entries
                    )
                ]
                if holding_addrs_with_perp:
                    usdc_note = (
                        "  ⚠️ part of this USDC is in Account Value of an active "
                        "perp wallet (Unified Account) — see per-wallet breakdown above"
                    )

            if len(entries) == 1:
                wallet_label = entries[0].get("_wallet_label", "")
                lines.append(
                    f"  • {coin}: {total_amount:.4f} · {_fmt_usd(total_usd_now)} now "
                    f"(cost basis {cost_basis_display}) [{wallet_label}]"
                    f"{usdc_note}"
                )
            else:
                lines.append(
                    f"  • {coin}: {total_amount:.4f} total · {_fmt_usd(total_usd_now)} now "
                    f"(cost basis {cost_basis_display})"
                    f"{usdc_note}"
                )
                for e in entries:
                    amt = e.get("total", 0) or 0
                    lines.append(f"      {e.get('_wallet_label','?')}: {amt:.4f}")

        # Render dust in compact single-line block
        if dust_coins:
            dust_total = sum(u for _, _, u in dust_coins)
            dust_parts = []
            for coin, amount, usd in sorted(dust_coins, key=lambda x: -x[2]):
                dust_parts.append(f"{coin} {amount:.4f} ({_fmt_usd(usd)})")
            lines.append(
                f"  SPOT DUST (<${DUST_THRESHOLD_USD:.0f} c/u, residual post-trading, {_fmt_usd(dust_total)} total):"
            )
            # Wrap into chunks of 4 per line
            for i in range(0, len(dust_parts), 4):
                chunk = " | ".join(dust_parts[i:i+4])
                lines.append(f"    {chunk}")

    # HyperLend section — detailed view with HF, collateral breakdown, debt
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
            label = h.get("label") or hl.get("label") or "—"
            wallet_short = (h.get("wallet") or "")[:6]
            if wallet_short:
                wallet_short = wallet_short + "…" + (h.get("wallet") or "")[-4:]
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
            lines.append(f"  ❌ {hl.get('error','error')}")

    # ── Bounce Tech leveraged tokens ──
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
                direction = "🟢 LONG" if p.get("is_long") else "🔴 SHORT"
                asset = p.get("asset", "?")
                lev = p.get("leverage", "?")
                val = p.get("value_usd", 0.0)
                bt_total += val
                lines.append(f"  {direction} {asset} {lev} — {_fmt_usd(val)}")
            lines.append(f"  Total BT: {_fmt_usd(bt_total)}")
        else:
            lines.append("  INACTIVA — sin posiciones abiertas")

    # ── Trades cerrados últimas 24h (agrupados por classify_fill) ──
    if recent_fills:
        lines.append("")
        lines.append("TRADES CERRADOS (24h)")

        # Bucket fills by classification tag
        grouped: dict[str, list[dict[str, Any]]] = {}
        total_pnl = 0.0
        total_fees = 0.0
        for f in recent_fills:
            label = f.get("_wallet_label", "")
            tag = classify_fill(f, wallet_label=label)
            grouped.setdefault(tag, []).append(f)
            total_pnl += f.get("closedPnl", 0) or 0
            total_fees += f.get("fee", 0) or 0

        # Render order (primary categories first, then alpha)
        primary_order = ["Core DCA", "Basket trade", "HL perp"]
        ordered_tags = [t for t in primary_order if t in grouped] + \
                       [t for t in sorted(grouped.keys()) if t not in primary_order]

        from datetime import datetime as _dt, timezone as _tz

        for tag in ordered_tags:
            fills = grouped[tag]
            sub_pnl = sum(f.get("closedPnl", 0) or 0 for f in fills)
            sub_notional = sum((f.get("sz", 0) or 0) * (f.get("px", 0) or 0) for f in fills)
            # Aggregate by coin/side for compact subtotals inside Core DCA / Basket
            by_coin: dict[str, dict[str, float]] = {}
            for f in fills:
                coin = f.get("coin", "?")
                side = f.get("side", "?").upper()
                key = f"{side} {coin}"
                agg = by_coin.setdefault(key, {"sz": 0.0, "notional": 0.0, "count": 0, "last_px": 0.0})
                agg["sz"] += f.get("sz", 0) or 0
                agg["notional"] += (f.get("sz", 0) or 0) * (f.get("px", 0) or 0)
                agg["count"] += 1
                agg["last_px"] = f.get("px", 0) or 0

            lines.append(f"  [{tag}]  {len(fills)} fill(s) · PnL: {_fmt_usd(sub_pnl)} · Notional: {_fmt_usd(sub_notional)}")

            # Per-fill detail (top 8, rest collapsed)
            for f in fills[:8]:
                coin = f.get("coin", "?")
                side = f.get("side", "?").upper()
                sz = f.get("sz", 0) or 0
                px = f.get("px", 0) or 0
                pnl = f.get("closedPnl", 0) or 0
                icon = "🟢" if pnl >= 0 else ("🔴" if pnl < 0 else "⚪")
                ts = f.get("time")
                time_str = ""
                if ts:
                    time_str = _dt.fromtimestamp(ts / 1000, tz=_tz.utc).strftime("%d %b %H:%M")
                # For spot fills pnl is usually 0 — show notional instead
                pnl_str = f"PnL {_fmt_usd(pnl)}" if pnl != 0 else f"Notional {_fmt_usd(sz*px)}"
                lines.append(
                    f"    {icon} {side} {coin} {sz:.4f} @ ${px:,.4f} | {pnl_str} | {time_str}"
                )
            if len(fills) > 8:
                lines.append(f"    … +{len(fills)-8} fills más en este grupo")

        lines.append(
            f"  TOTAL PnL: {_fmt_usd(total_pnl)} | Fees: {_fmt_usd(total_fees)} | Net: {_fmt_usd(total_pnl - total_fees)}"
        )

    return "\n".join(lines)


def format_hf(hyperlend: list[dict[str, Any]] | dict[str, Any]) -> str:
    """Format HF for /hf command — supports both list and single dict."""
    hl_list = hyperlend if isinstance(hyperlend, list) else [hyperlend]

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
            # Regla operativa: <1.00 liquidación real, <1.10 acción, <1.15 monitoreo,
            # 1.10–1.20 normal operativo (NO alertar), >1.20 cómodo.
            if hf < 1.10:
                icon = "🚨"
            elif hf < 1.15:
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
        "Sin relleno, números específicos, conclusiones accionables."
    )
