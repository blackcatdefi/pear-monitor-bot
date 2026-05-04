"""R-DASHBOARD-COMMAND — Telegram message renderer for /dashboard.

Reuses the same _build_state() from modules/dashboard.py so the Telegram
command always renders the same data as the web dashboard.

All blocks mirror the HTML dashboard (R-DASHBOARD-FIX):
  - Capital       (NET / gross via capital_calc SSoT)
  - Main flywheel (kHYPE / UETH)
  - Secondary flywheel (if active)
  - Active basket (dynamic label + legs + UPnL — autodetect, not hardcoded)
  - Market        (BTC / ETH / HYPE / F&G)
  - Wallets       (per-wallet capital breakdown)
  - Catalysts     (next 5 from macro calendar)
  - Footer        (timestamp + staleness)
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

_SEP = "─" * 32


def _fmt_usd(v: Any, dec: int = 0) -> str:
    try:
        return f"${float(v):,.{dec}f}"
    except Exception:
        return "—"


def _fmt_compact_usd(v: Any) -> str:
    try:
        f = float(v)
    except Exception:
        return "—"
    sign = "-" if f < 0 else ""
    f = abs(f)
    if f >= 1_000_000:
        return f"{sign}${f/1_000_000:.2f}M"
    if f >= 1_000:
        return f"{sign}${f/1_000:.1f}K"
    return f"{sign}${f:.2f}"


def _fmt_signed(v: Any) -> str:
    """Return signed dollar amount like +$120.50 or -$45.00."""
    try:
        f = float(v)
        sign = "+" if f >= 0 else ""
        return f"{sign}${f:,.2f}"
    except Exception:
        return "—"


def _fmt_token(v: Any, dec: int = 2) -> str:
    try:
        return f"{float(v):,.{dec}f}"
    except Exception:
        return "—"


def render_dashboard_telegram(state: dict[str, Any]) -> str:
    """Render dashboard state as a Telegram plain-text message.

    ``state`` is the flat dict returned by ``modules.dashboard._build_state()``.
    """
    lines: list[str] = []

    # ── Header ────────────────────────────────────────────────────
    lines.append(f"🐱‍⬛ DASHBOARD — {state.get('ts', '—')}")
    lines.append(_SEP)

    # ── Capital block (SSoT: capital_calc) ────────────────────────
    lines.append("")
    lines.append("💰 CAPITAL")
    try:
        from auto.capital_calc import compute_net_capital, format_net_capital_telegram
        net_cap = compute_net_capital(state)
        lines.append(format_net_capital_telegram(net_cap))
    except Exception:  # noqa: BLE001
        log.exception("dashboard_telegram: capital_calc failed (non-fatal)")
        lines.append(f"Total: {_fmt_compact_usd(state.get('capital_total'))}")

    # ── Main flywheel ─────────────────────────────────────────────
    lines.append("")
    lines.append("🔄 MAIN FLYWHEEL")
    main = state.get("main_flywheel")
    if main:
        try:
            hf_str = f"{float(main['hf']):.3f}" if main.get("hf") is not None else "—"
        except Exception:
            hf_str = "—"
        coll_amt = (
            _fmt_token(main.get("collateral_balance"), dec=2)
            if main.get("collateral_balance") else "—"
        )
        coll_sym = main.get("collateral_symbol") or "?"
        debt_amt = (
            _fmt_token(main.get("debt_balance"), dec=4)
            if main.get("debt_balance") else "—"
        )
        debt_sym = main.get("debt_symbol") or "?"
        lines.append(f"Wallet: {main.get('short', '—')}")
        lines.append(f"HF: {hf_str}")
        lines.append(
            f"Collateral: {coll_amt} {coll_sym}"
            f" ({_fmt_compact_usd(main.get('collateral_usd'))})"
        )
        lines.append(
            f"Debt: {debt_amt} {debt_sym}"
            f" ({_fmt_compact_usd(main.get('debt_usd'))})"
        )
    else:
        lines.append("No active HyperLend flywheel (no debt).")

    # ── Secondary flywheel (only if present) ─────────────────────
    sec = state.get("secondary_flywheel")
    if sec is not None:
        lines.append("")
        lines.append("🔄 SECONDARY FLYWHEEL")
        try:
            hf_str2 = f"{float(sec['hf']):.3f}" if sec.get("hf") is not None else "—"
        except Exception:
            hf_str2 = "—"
        sec_coll_amt = (
            _fmt_token(sec.get("collateral_balance"), dec=4)
            if sec.get("collateral_balance") else "—"
        )
        sec_coll_sym = sec.get("collateral_symbol") or "?"
        sec_debt_amt = (
            _fmt_token(sec.get("debt_balance"), dec=4)
            if sec.get("debt_balance") else "—"
        )
        sec_debt_sym = sec.get("debt_symbol") or "?"
        lines.append(f"Wallet: {sec.get('short', '—')}")
        lines.append(f"HF: {hf_str2}")
        lines.append(
            f"Collateral: {sec_coll_amt} {sec_coll_sym}"
            f" ({_fmt_compact_usd(sec.get('collateral_usd'))})"
        )
        lines.append(
            f"Debt: {sec_debt_amt} {sec_debt_sym}"
            f" ({_fmt_compact_usd(sec.get('debt_usd'))})"
        )

    # ── Active basket (autodetect) ────────────────────────────────
    lines.append("")
    lines.append("📦 ACTIVE BASKET (autodetect)")
    basket_state = state.get("basket_state") or {}
    active_wallets = [
        (addr, w)
        for addr, w in (basket_state.get("wallets") or {}).items()
        if w.get("status") == "ACTIVE"
    ]

    if active_wallets:
        for addr, w in active_wallets:
            short_addr = addr[:6] + "…" + addr[-4:] if len(addr) >= 10 else addr
            # Dynamic label — same logic as HTML dashboard (R-DASH-FIX Bug 5)
            basket_id = w.get("basket_id_inferido") or ""
            all_legs = w.get("positions") or w.get("shorts") or []
            n_legs = len(all_legs)
            if basket_id and n_legs > 0:
                basket_display = f"Basket {basket_id} ({n_legs} legs)"
            elif basket_id:
                basket_display = f"Basket {basket_id}"
            else:
                basket_display = w.get("label", "")
            lines.append(f"{short_addr} — {basket_display}")

            entries = w.get("positions") or w.get("shorts") or []
            entries_sorted = sorted(
                entries,
                key=lambda s: float(s.get("ntl") or 0.0),
                reverse=True,
            )
            for s in entries_sorted:
                upnl = s.get("upnl")
                if upnl is None:
                    for bp in state.get("basket_positions") or []:
                        if (
                            str(bp.get("coin", "")).upper()
                            == str(s.get("coin", "")).upper()
                        ):
                            upnl = bp.get("upnl")
                            break
                upnl_str = _fmt_signed(upnl) if upnl is not None else "—"
                side = (s.get("side") or "SHORT").upper()
                ntl = float(s.get("ntl") or 0.0)
                lines.append(
                    f"  {s.get('coin')} {side} {upnl_str}"
                    f" (ntl ${ntl:,.0f})"
                )

        # Basket total UPnL
        basket_upnl_fmt = _fmt_signed(state.get("basket_upnl") or 0.0)
        total_ntl = float(
            basket_state.get("summary", {}).get("total_basket_notional_usd") or 0.0
        )
        lines.append(f"Total UPnL: {basket_upnl_fmt} · ntl ${total_ntl:,.0f}")
    else:
        lines.append("No open positions in fund wallets.")

    # ── Market ────────────────────────────────────────────────────
    lines.append("")
    lines.append("📈 MARKET")
    btc = state.get("btc")
    eth = state.get("eth")
    hype = state.get("hype")
    fg_value = state.get("fg_value")
    fg_label = state.get("fg_label")

    cached_prices = state.get("cached_prices") or {}
    cached_btc = cached_prices.get("btc")
    cached_eth = cached_prices.get("eth")
    cached_hype = cached_prices.get("hype")
    cache_age = cached_prices.get("age_s")

    btc_eff = btc if btc is not None else cached_btc
    eth_eff = eth if eth is not None else cached_eth
    hype_eff = hype if hype is not None else cached_hype

    if btc is None and eth is None and hype is None:
        if cached_btc is not None or cached_eth is not None or cached_hype is not None:
            age_min = (cache_age or 0) // 60
            lines.append(f"⚠️ API down — using cache ({age_min}min ago)")
        else:
            lines.append("(API down — no cache)")

    lines.append(f"BTC: {_fmt_usd(btc_eff)}")
    lines.append(f"ETH: {_fmt_usd(eth_eff)}")
    lines.append(f"HYPE: {_fmt_usd(hype_eff, dec=2)}")
    fg_str = f"{fg_value} ({fg_label})" if fg_value is not None else "—"
    lines.append(f"F&G: {fg_str}")

    # ── Wallets summary ───────────────────────────────────────────
    wallets = state.get("wallets") or []
    lines.append("")
    lines.append(f"👛 WALLETS ({len(wallets)})")
    has_wallets = False
    for ws in wallets:
        if ws.get("capital", 0) < 0.01:
            continue
        has_wallets = True
        parts: list[str] = []
        if ws.get("perp", 0) > 0.01:
            parts.append(f"Perp {_fmt_compact_usd(ws['perp'])}")
        if ws.get("spot", 0) > 0.01:
            parts.append(f"Spot {_fmt_compact_usd(ws['spot'])}")
        if ws.get("hl_coll", 0) > 0.01:
            parts.append(f"HL {_fmt_compact_usd(ws['hl_coll'])}")
        if ws.get("hl_debt", 0) > 0.01:
            parts.append(f"Debt -{_fmt_compact_usd(ws['hl_debt'])}")
        detail = " · ".join(parts) if parts else "—"
        lines.append(
            f"{ws.get('label')} {ws.get('short')} — "
            f"{_fmt_compact_usd(ws.get('capital'))}"
        )
        lines.append(f"  {detail}")
    if not has_wallets:
        lines.append("No wallets reported.")

    # ── Upcoming catalysts ────────────────────────────────────────
    lines.append("")
    lines.append("📅 UPCOMING CATALYSTS")
    upcoming = state.get("upcoming") or []
    if upcoming:
        for ev in upcoming:
            try:
                when = ev.timestamp_utc.strftime("%Y-%m-%d %H:%M UTC")
            except Exception:
                when = "?"
            lines.append(
                f"{when} — {ev.name}"
                f" [{ev.impact_level}/{ev.category}]"
            )
    else:
        lines.append("No upcoming events in calendar.")

    # ── Footer ────────────────────────────────────────────────────
    lines.append("")
    lines.append(_SEP)
    stale_tag = " ⚠️ stale" if not state.get("is_fresh", True) else ""
    lines.append(
        f"Read-only · SSoT with /reporte · {state.get('ts', '—')}{stale_tag}"
    )

    return "\n".join(lines)


async def build_dashboard_telegram() -> str:
    """Fetch live dashboard state and render as a Telegram message.

    Reuses ``modules.dashboard._build_state()`` — same data path as the web
    dashboard so both surfaces always show the same numbers.
    """
    from modules.dashboard import _build_state
    state = await _build_state()
    return render_dashboard_telegram(state)
