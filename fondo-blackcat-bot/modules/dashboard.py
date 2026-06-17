"""Round 17 — Read-only HTML dashboard servido por aiohttp.

URL:  GET /dashboard?token=XXX
Auth simple por token (env DASHBOARD_TOKEN). Si DASHBOARD_TOKEN está vacío,
endpoint devuelve 503 "disabled".

Auto-refresh 60s (meta http-equiv="refresh").

HOTFIX (post R17): el dashboard ahora consume ``modules.portfolio_snapshot``
— la misma capa de agregación que /reporte. Antes leía HL/perp por separado
con un wallet-pick ad-hoc que mostraba el flywheel chico (0xCDDF UBTC/USDT0)
en vez del grande (0xA44E WHYPE/UETH), capital neto en vez de bruto, y
buscaba precios con keys equivocados (``prices["bitcoin"]`` que nunca
existió en este codebase). Single-source-of-truth ahora vive en
``portfolio_snapshot.build_portfolio_snapshot()``.
"""
from __future__ import annotations

import html
import logging
import os
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


def _get_token() -> str:
    return os.getenv("DASHBOARD_TOKEN", "").strip()


def _enabled() -> bool:
    if os.getenv("DASHBOARD_ENABLED", "true").strip().lower() == "false":
        return False
    return bool(_get_token())


def _esc(v: Any) -> str:
    return html.escape(str(v)) if v is not None else "—"


def _fmt_usd(v: Any, dec: int = 0) -> str:
    try:
        return f"${float(v):,.{dec}f}"
    except Exception:
        return "—"


def _fmt_compact_usd(v: Any) -> str:
    """Dashboard-friendly compact USD. ``$86,500`` → ``$86.5K``."""
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


def _signed(v: Any) -> tuple[str, str]:
    """Return (cls, formatted)."""
    try:
        f = float(v)
        sign = "+" if f >= 0 else "-"
        cls = "pos" if f >= 0 else "neg"
        return cls, f"{sign}${abs(f):,.2f}"
    except Exception:
        return "", "—"


def _fmt_token_amount(v: Any, dec: int = 2) -> str:
    try:
        return f"{float(v):,.{dec}f}"
    except Exception:
        return "—"


def _staleness_badge(state: dict[str, Any]) -> str:
    """Render the live/stale badge based on snapshot age + freshness flag.

    Categories:
    - is_fresh & age < TTL : ● live (green)
    - is_fresh & TTL ≤ age < 60s : ● live <Ns> (green, just refreshed)
    - !is_fresh & age < 60s : ● stale Ns (amber)
    - !is_fresh & 60s ≤ age < 600s : ⚠ stale Nmin (amber, louder)
    - !is_fresh & age ≥ 600s : ⚠ STALE Nmin — RPC issues (red)
    """
    age = state.get("snap_age_sec")
    is_fresh = bool(state.get("is_fresh"))
    if age is None:
        return ('<span style="color:#888;">○ no data</span>')
    age_int = int(age)
    if is_fresh and age_int < 60:
        return ('<span style="color:#00ff88;">● live</span>')
    if is_fresh:
        return (f'<span style="color:#00ff88;">● live {age_int}s</span>')
    if age_int < 60:
        return (f'<span style="color:#ffaa00;">● stale {age_int}s</span>')
    if age_int < 600:
        return (f'<span style="color:#ffaa00;">⚠ stale {age_int//60}min</span>')
    return (f'<span style="color:#ff4444;">⚠ STALE {age_int//60}min — RPC issues</span>')


def _render_loading_placeholder(error: str | None = None) -> str:
    """Cold-start screen: cache empty AND fetch failed. Auto-refresh
    every 10s until the cache populates."""
    err_html = (
        f"<p style='color:#666; font-size:11px; margin-top:24px;'>last error: {html.escape(error)}</p>"
        if error else ""
    )
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <title>Fondo Black Cat — Dashboard</title>
    <meta http-equiv="refresh" content="10">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {{
            background:#0a0a0a; color:#00ff88;
            font-family:'SF Mono', Menlo, Monaco, monospace;
            padding:40px; text-align:center; line-height:1.6;
        }}
        h1 {{ color:#ffaa00; }}
        .spin {{ display:inline-block; animation:spin 2s linear infinite; }}
        @keyframes spin {{ 0%{{transform:rotate(0)}} 100%{{transform:rotate(360deg)}} }}
    </style>
</head>
<body>
    <h1>🐱‍⬛ Fondo Black Cat — Dashboard</h1>
    <p style="color:#ffaa00; font-size:18px;"><span class="spin">⏳</span> Initializing dashboard…</p>
    <p style="color:#888; font-size:14px;">Loading data from HyperEVM RPC. This may take 10-25s on cold start.</p>
    <p style="color:#888; font-size:12px;">Page auto-refreshes every 10s.</p>
    {err_html}
</body>
</html>"""


def _render_html(state: dict[str, Any]) -> str:
    # Cold-start escape hatch — no wallets, no flywheel, no market block:
    # cache was empty AND the fetch failed. Show a clear loading screen
    # instead of a half-rendered, all-zeros dashboard.
    if (
        not (state.get("wallets") or [])
        and state.get("main_flywheel") is None
        and state.get("btc") is None
        and state.get("eth") is None
    ):
        return _render_loading_placeholder(error=state.get("last_error"))

    cap_total = _fmt_compact_usd(state["capital_total"])
    # R-DASHBOARD-DOUBLECOUNT-FIX (2026-05-06) Bug #5: header UPnL is the
    # SAME number as the basket card UPnL. Pre-fix the header summed every
    # perp position's UPnL (header +$40.22) while the basket card summed
    # only the SHORT-basket leg UPnLs (basket -$35.79). Two truths is one
    # too many — `basket_upnl` is the single source of truth: all open
    # perp positions in active basket wallets aggregated once.
    upnl_for_header = state.get("basket_upnl")
    if upnl_for_header is None:
        upnl_for_header = state.get("upnl_perp_total") or 0.0
    upnl_cls, upnl_fmt = _signed(upnl_for_header)

    # ─── R-DASH: NET CAPITAL via single-source-of-truth ───────────────────
    # Replaces the misleading "Total: $79K" first line that summed gross
    # exposure including leveraged HL collateral. NET is the post-leverage
    # number; gross is rendered as informative footer.
    from auto.capital_calc import compute_net_capital, render_net_capital_html
    _net_cap = compute_net_capital(state)
    capital_block_html = render_net_capital_html(
        _net_cap,
        fmt_compact_usd=_fmt_compact_usd,
        signed=_signed,
        upnl_cls=upnl_cls,
        upnl_fmt=upnl_fmt,
    )

    # ─── Flywheel principal ───────────────────────────────────────────────
    # R-DASHBOARD-RABBY-PARITY (2026-05-06): branch on hf_status so the
    # card never renders literal ``nan``. The previous implementation
    # called ``f"{float(main['hf']):.3f}"`` directly on a NaN whenever the
    # HyperEVM RPC rate-limited the per-wallet refresh — surfaced 'nan'
    # in both flywheels in the may 6 09:09 UTC parity audit.
    #
    # R-DASHBOARD-DOUBLECOUNT-FIX (2026-05-06):
    # * Bug #2: collateral_symbol now falls back to the canonical reserve
    #   address map (WHYPE = 0x5555…5555) so the main flywheel renders
    #   "1,751.18 WHYPE ($75.7K)" instead of "0.00 UETH ($75.7K)".
    # * Bug #4: when the wallet is in the CLOSED_AT_ISO map, render
    #   "CLOSED at <date>, last HF: X" instead of an auto-staleness "(cached
    #   Xh ago)" counter that is meaningless for a retired wallet.
    from auto.wallet_labels import (
        apply_wallet_label as _apply_label,
        is_closed_wallet as _is_closed,
        closed_at_iso as _closed_at,
    )
    # Canonical reserve address → symbol map (mirror of
    # auto.hyperlend_reader._KNOWN_RESERVE_SYMBOLS) for the last-resort
    # collateral_symbol fallback at render time.
    _RESERVE_SYMBOLS = {
        "0x5555555555555555555555555555555555555555": "WHYPE",
        "0x94e8396e0869c9f2200760af0621afd240e1cf38": "wstHYPE",
        "0x9fdbda0a5e284c32744d2f17ee5c74b284993463": "UBTC",
        "0xbe6727b535545c67d5caa73dea54865b92cf7907": "UETH",
        "0x5d3a1ff2b6bab83b63cd9ad0787074081a52ef34": "USDe",
        "0xb8ce59fc3717ada4c02eadf9682a9e934f625ebb": "USDT0",
        "0xb88339cb7199b77e23db6e890353e22632ba630f": "USDC",
        "0x111111a1a0667d36bd57c0a9f569b98057111111": "USDH",
        "0xfd739d4e423301ce9385c1fb8850539d657c296d": "kHYPE",
        "0xd8fc8f0b03eba61f64d08b0bef69d80916e5dda9": "beHYPE",
        "0x068f321fa8fb9f0d135f290ef6a3e2813e1c8a29": "USOL",
    }

    def _render_hf_block(card_data: dict[str, Any]) -> str:
        """Render a flywheel card with hf_status-aware HF fallback."""
        status = (card_data.get("hf_status") or "OK").upper()
        addr = card_data.get("address") or ""
        is_closed = _is_closed(addr)
        closed_iso = _closed_at(addr)

        # Live HF
        live_hf = card_data.get("hf")
        live_hf_str = "—"
        if live_hf is not None:
            try:
                if live_hf == float("inf"):
                    live_hf_str = "∞"
                else:
                    f_hf = float(live_hf)
                    import math as _math
                    if _math.isfinite(f_hf):
                        live_hf_str = f"{f_hf:.3f}"
            except Exception:  # noqa: BLE001
                live_hf_str = "—"

        # Cached HF (fallback path for UNKNOWN / CLOSED)
        last_hf = card_data.get("last_known_hf")
        age_s = card_data.get("age_seconds")
        if isinstance(age_s, (int, float)):
            if age_s < 60:
                age_label = f"{int(age_s)}s ago"
            elif age_s < 3600:
                age_label = f"{int(age_s) // 60}min ago"
            else:
                age_label = f"{int(age_s) // 3600}h ago"
        else:
            age_label = "?"

        # R-DASHBOARD-DOUBLECOUNT-FIX Bug #4: closed wallets render the
        # last-known HF with the closure date, NOT a stale-counter. The
        # cached value is informative ("HF was X when we walked away")
        # but is no longer changing.
        if is_closed:
            try:
                last_hf_str = (
                    f"{float(last_hf):.3f}" if last_hf is not None and not isinstance(last_hf, str)
                    else "—"
                )
            except Exception:  # noqa: BLE001
                last_hf_str = "—"
            if closed_iso:
                hf_render = (
                    f"<span class='dim'>CLOSED at {_esc(closed_iso)}, "
                    f"last HF: <strong>{_esc(last_hf_str)}</strong></span>"
                )
            else:
                hf_render = (
                    f"<span class='dim'>CLOSED, "
                    f"last HF: <strong>{_esc(last_hf_str)}</strong></span>"
                )
        elif status == "OK" and live_hf_str != "—":
            hf_render = f"<strong>{_esc(live_hf_str)}</strong>"
        elif status == "ZERO":
            hf_render = "<span class='dim'>n/a (no positions)</span>"
        else:
            # UNKNOWN — render last-known with cache badge
            if last_hf is None:
                hf_render = (
                    "<span style='color:#ffaa00;'>⚠️ rate-limited "
                    "(no prior cache)</span>"
                )
            elif isinstance(last_hf, str):
                # 'inf' sentinel
                hf_render = (
                    "<span style='color:#ffaa00;'>⚠️ "
                    f"last HF ∞ (cached {_esc(age_label)})</span>"
                )
            else:
                try:
                    last_hf_str = f"{float(last_hf):.3f}"
                except Exception:  # noqa: BLE001
                    last_hf_str = "—"
                hf_render = (
                    f"<strong>{_esc(last_hf_str)}</strong>"
                    f" <span style='color:#ffaa00;'>(cached {_esc(age_label)})</span>"
                )

        # Apply canonical wallet label
        canonical_label = _apply_label(
            card_data.get("address"), card_data.get("label")
        )
        # R-DASHBOARD-DOUBLECOUNT-FIX Bug #2: collateral_symbol fallback
        # chain — live data → primary_collateral.asset → known-reserve
        # map. Without this fallback the main flywheel rendered
        # "Collateral: 0.00 UETH ($75.7K)" because UETH was the FIRST
        # entry in collateral_assets but the actual collateral asset on
        # this wallet is WHYPE (0x5555…5555).
        coll_amt = (
            _fmt_token_amount(card_data.get("collateral_balance"), dec=2)
            if card_data.get("collateral_balance") else "—"
        )
        coll_sym = card_data.get("collateral_symbol")
        if not coll_sym:
            coll_asset = (card_data.get("collateral_asset") or "").lower()
            if coll_asset:
                coll_sym = _RESERVE_SYMBOLS.get(coll_asset)
        coll_sym = coll_sym or "?"

        debt_amt = (
            _fmt_token_amount(card_data.get("debt_balance"), dec=4)
            if card_data.get("debt_balance") else "—"
        )
        debt_sym_raw = card_data.get("debt_symbol")
        debt_asset = (card_data.get("debt_asset") or "").lower()
        debt_sym = (
            debt_sym_raw
            or _RESERVE_SYMBOLS.get(debt_asset)
            or (
                debt_asset[:6] + "…" + debt_asset[-4:]
                if len(debt_asset) >= 10 else "?"
            )
        )
        return (
            f"<p><strong>{_esc(canonical_label)}</strong>"
            f" <span class='dim'>{_esc(card_data.get('short'))}</span></p>"
            f"<p>HF: {hf_render}</p>"
            f"<p>Collateral: {_esc(coll_amt)} {_esc(coll_sym)}"
            f" <span class='dim'>({_esc(_fmt_compact_usd(card_data.get('collateral_usd')))})</span></p>"
            f"<p>Debt: {_esc(debt_amt)} {_esc(debt_sym)}"
            f" <span class='dim'>({_esc(_fmt_compact_usd(card_data.get('debt_usd')))})</span></p>"
        )

    # R-DASHBOARD-DOUBLECOUNT-FIX Bug #3: pick the FIRST non-closed
    # flywheel as "main". The legacy logic took flywheels[0] by collateral
    # rank, which surfaced the closed 0xcddf wallet next to the main one
    # whenever the closed wallet still carried collateral residue.
    main = state.get("main_flywheel")
    sec = state.get("secondary_flywheel")
    if main is not None and _is_closed(main.get("address") or ""):
        # Promote the secondary if main is closed.
        main, sec = sec, main
    if main is not None and not _is_closed(main.get("address") or ""):
        flywheel_html = _render_hf_block(main)
    else:
        flywheel_html = "<p class='dim'>No active HyperLend flywheel (no debt).</p>"

    # Secondary flywheel (chico) — solo si existe, no es dust ($50 floor)
    # AND no está cerrada. Wallets cerradas se renderizan en su propia
    # sección "Wallets cerradas (histórico)" debajo.
    from auto.wallet_labels import is_dust as _is_dust
    secondary_html = ""
    closed_html = ""
    closed_cards: list[str] = []

    # Collect every flywheel referenced by main/secondary that is closed
    # so they render in the historical section instead of beside the
    # main one. Using both main and sec slots covers the case where the
    # raw snapshot put the closed wallet first.
    _seen_addrs: set[str] = set()
    raw_main = state.get("main_flywheel")
    raw_sec = state.get("secondary_flywheel")
    for cand in (raw_main, raw_sec):
        if cand is None:
            continue
        addr_l = (cand.get("address") or "").lower()
        if not addr_l or addr_l in _seen_addrs:
            continue
        if _is_closed(addr_l):
            _seen_addrs.add(addr_l)
            closed_cards.append(_render_hf_block(cand))

    if sec is not None and not _is_closed(sec.get("address") or ""):
        sec_capital = float(
            (sec.get("collateral_usd") or 0.0)
            + abs(sec.get("debt_usd") or 0.0)
        )
        if not _is_dust(sec_capital):
            secondary_html = (
                "<div class='card'>"
                "<h2>Secondary flywheel</h2>"
                + _render_hf_block(sec)
                + "</div>"
            )

    if closed_cards:
        closed_html = (
            "<div class='card'>"
            "<h2>Wallets cerradas (histórico)</h2>"
            + "".join(closed_cards)
            + "</div>"
        )

    # ─── Basket activa (R-SILENT autodetect) ─────────────────────────────
    # Datos vienen de auto.fund_state_v2.detect_active_baskets() (on-chain).
    # No hardcodeamos número de basket — eso es metadata humana, no del bot.
    #
    # R-DASHBOARD-RABBY-PARITY (2026-05-06): each leg now also renders the
    # entry price, mark price, leverage and side colour so the card is
    # actionable at-a-glance. ``leverage = ntl / margin_used`` derived from
    # the underlying perp account state when available.
    basket_rows: list[str] = []
    basket_state = state.get("basket_state") or {}
    active_wallets = []
    for addr, w in (basket_state.get("wallets") or {}).items():
        if w.get("status") == "ACTIVE":
            active_wallets.append((addr, w))

    if active_wallets:
        for addr, w in active_wallets:
            short_addr = addr[:6] + "…" + addr[-4:] if len(addr) >= 10 else addr
            # R-DASH-FIX Bug 5: dynamic label — show basket_id_inferido + leg
            # count so "Alt Short Bleed v4" (stale env-var label) is never shown.
            basket_id = w.get("basket_id_inferido") or ""
            all_legs = w.get("positions") or w.get("shorts") or []
            n_legs = len(all_legs)
            if basket_id and n_legs > 0:
                basket_display = f"Basket {basket_id} ({n_legs} legs)"
            elif basket_id:
                basket_display = f"Basket {basket_id}"
            else:
                basket_display = w.get("label", "")
            # R-DASHBOARD-RABBY-PARITY: apply canonical wallet label
            wallet_canonical = _apply_label(addr, w.get("label"))
            basket_rows.append(
                f"<p><strong>{_esc(wallet_canonical)}</strong>"
                f" <span class='dim'>{_esc(short_addr)}</span> ·"
                f" <strong>{_esc(basket_display)}</strong></p>"
            )
            entries = w.get("positions") or w.get("shorts") or []
            # Sort by notional desc
            entries_sorted = sorted(
                entries,
                key=lambda s: float(s.get("ntl") or 0.0),
                reverse=True,
            )
            for s in entries_sorted:
                # Prefer the inline upnl carried in the position dict
                # (basket-agnostic detector now ships it). Fall back to
                # snapshot lookup for older basket_positions feed.
                upnl = s.get("upnl")
                if upnl is None:
                    for bp in state.get("basket_positions") or []:
                        if str(bp.get("coin", "")).upper() == str(s.get("coin", "")).upper():
                            upnl = bp.get("upnl")
                            break
                cls, fmt = _signed(upnl) if upnl is not None else ("dim", "—")
                side = (s.get("side") or "SHORT").upper()
                # R-DASHBOARD-RABBY-PARITY: side colour cue (LONG green / SHORT red).
                side_colour = "#00ff88" if side == "LONG" else "#ff8866"
                ntl_val = float(s.get("ntl") or 0.0)
                # Optional enrichment: entry / mark / leverage (basket detector
                # ships them when available; gracefully skipped when absent).
                entry_px = s.get("entry_px") or s.get("entryPx")
                mark_px = s.get("mark_px") or s.get("markPx")
                leverage = s.get("leverage") or s.get("lev")
                detail_bits: list[str] = []
                if entry_px:
                    try:
                        detail_bits.append(f"entry ${float(entry_px):,.4f}")
                    except (TypeError, ValueError):
                        pass
                if mark_px:
                    try:
                        detail_bits.append(f"mark ${float(mark_px):,.4f}")
                    except (TypeError, ValueError):
                        pass
                if leverage:
                    try:
                        detail_bits.append(f"{float(leverage):.1f}x")
                    except (TypeError, ValueError):
                        pass
                detail_str = (
                    " · " + " · ".join(detail_bits) if detail_bits else ""
                )
                basket_rows.append(
                    f"<p>&nbsp;&nbsp;{_esc(s.get('coin'))} "
                    f"<span style='color:{side_colour};'>{_esc(side)}</span>"
                    f" <span class='{cls}'>{_esc(fmt)}</span>"
                    f" <span class='dim'>(ntl ${ntl_val:,.0f}{detail_str})</span></p>"
                )
        # Total
        bcls, bfmt = _signed(state.get("basket_upnl") or 0.0)
        total_ntl = float(basket_state.get("summary", {}).get("total_basket_notional_usd") or 0.0)
        basket_rows.append(
            f"<p class='dim'>Total UPnL: <span class='{bcls}'>{_esc(bfmt)}</span>"
            f" · ntl ${total_ntl:,.0f}</p>"
        )
    else:
        basket_rows.append(
            "<p class='dim'>No open positions in fund wallets.</p>"
        )

    # ─── Pear Protocol staked (R-DASHBOARD-RABBY-PARITY) ─────────────────
    # Surface external DeFi positions that don't show up in HL / perp / spot
    # endpoints. Today only Pear Protocol staked is exposed (env-driven);
    # an on-chain reader will replace the env var in a future round.
    # R-PEAR-ASSET-INTEGRATION (2026-06-17): PEAR is the fund's 2nd asset —
    # live stPEAR balance × price (or n/d on read failure). First-class card.
    pear_staked_total = float(state.get("pear_staked_total") or 0.0)
    pear_known = bool(state.get("pear_staked_known", True))
    pear_balance = float(state.get("pear_staked_balance") or 0.0)
    pear_price = float(state.get("pear_staked_price") or 0.0)
    pear_card_html = ""
    if not pear_known:
        pear_card_html = (
            "<div class='card'>"
            "<h2>PEAR (2º activo)</h2>"
            "<p><strong>stPEAR</strong> · <strong>n/d</strong></p>"
            "<p class='dim'>Lectura on-chain/precio falló — excluido del equity "
            "(nunca se fabrica un valor).</p>"
            "</div>"
        )
    elif pear_staked_total > 0.01:
        _detail = (
            f"{pear_balance:,.0f} stPEAR × ${pear_price:.5f}"
            if pear_balance > 0 and pear_price > 0
            else "staked"
        )
        pear_card_html = (
            "<div class='card'>"
            "<h2>PEAR (2º activo)</h2>"
            f"<p><strong>stPEAR</strong> · "
            f"<strong>{_esc(_fmt_compact_usd(pear_staked_total))}</strong></p>"
            f"<p class='dim'>{_esc(_detail)} · live on-chain (Arbitrum)</p>"
            "<p class='dim'>Folded into TOTAL EQUITY (Rabby parity).</p>"
            "</div>"
        )

    # ─── Vault Deposits (R-VAULTDEP) ─────────────────────────────────────
    # Fund capital deposited INTO HL vaults (e.g. "Systemic Strategies
    # HyperGrowth"). Lives under the vault address, NOT in any fund wallet,
    # so it would be invisible without this card. Equity is read live via the
    # keyless userVaultEquities endpoint and is already folded into the
    # TOTAL EQUITY headline by compute_net_capital. The card shows the full
    # breakdown (label, current equity, cost basis, unrealized PnL USD/%) plus
    # an evolution line (all-time vs cost + delta vs the prior-day snapshot).
    vault_card_html = ""
    _vault_detail = [
        d for d in (state.get("vault_deposits_detail") or [])
        if d.get("found")
    ]
    _vault_total = float(state.get("vault_deposits_total") or 0.0)
    if _vault_detail:
        _vrows: list[str] = []
        for d in _vault_detail:
            _pnl_cls, _pnl_fmt = _signed(d.get("pnl_usd"))
            try:
                _pnl_pct = float(d.get("pnl_pct") or 0.0)
            except (TypeError, ValueError):
                _pnl_pct = 0.0
            _pct_sign = "+" if _pnl_pct >= 0 else ""
            _evo = ""
            if d.get("has_prev"):
                _dlt_cls, _dlt_fmt = _signed(d.get("delta_prev_usd"))
                _evo = (
                    f"<p class='dim'>Evolución: <span class='{_esc(_dlt_cls)}'>"
                    f"{_esc(_dlt_fmt)}</span> vs {_esc(d.get('prev_label'))}</p>"
                )
            else:
                _evo = (
                    "<p class='dim'>Evolución: baseline guardado "
                    "(sin snapshot previo todavía)</p>"
                )
            # R-PMCORE: max drawdown + auto-discovery tag per vault.
            try:
                _mdd = float(d.get("mdd_pct") or 0.0)
            except (TypeError, ValueError):
                _mdd = 0.0
            _mdd_html = (
                f"<p class='dim'>Max drawdown: -{_mdd:.1f}%</p>" if _mdd > 0.0 else ""
            )
            if d.get("auto_discovered"):
                _cb_html = "<p class='dim'>Cost basis: auto-descubierto (sin costo configurado)</p>"
            else:
                _cb_html = (
                    "<p class='dim'>Cost basis: " + _esc(_fmt_usd(d.get("cost_basis_usd")))
                    + " · PnL: <span class='" + _esc(_pnl_cls) + "'>" + _esc(_pnl_fmt)
                    + " (" + _pct_sign + f"{_pnl_pct:.2f}%" + ")</span></p>"
                )
            _vrows.append(
                "<p><strong>" + _esc(d.get("label")) + "</strong> · "
                "<strong>" + _esc(_fmt_usd(d.get("equity_usd"))) + "</strong></p>"
                + _cb_html
                + _mdd_html
                + _evo
            )
        vault_card_html = (
            "<div class='card'>"
            "<h2>Vault Deposits (HL)</h2>"
            + "".join(_vrows)
            + "<p class='dim'>Total folded into TOTAL EQUITY: "
            + _esc(_fmt_compact_usd(_vault_total))
            + " · keyless userVaultEquities (read-only).</p>"
            "</div>"
        )

    # ─── Portfolio Margin (R-PMCORE) ─────────────────────────────────────
    pm_card_html = ""
    _pm = state.get("pm_state")
    if _pm is not None and getattr(_pm, "collateral_usd", 0.0) > 0:
        _status = getattr(_pm, "status", "CALM")
        _status_cls = {
            "CALM": "pos", "WARN": "warn", "STRESS": "warn", "LIQ": "neg",
        }.get(_status, "dim")
        _naked = (
            "<p class='neg'><strong>🚨 HEDGE MISSING</strong> — USDC debt vs HYPE "
            "con shorts en 0: naked leveraged long.</p>"
            if getattr(_pm, "naked_long", False) else ""
        )
        pm_card_html = (
            "<div class='card'>"
            "<h2>Portfolio Margin (cuenta primaria)</h2>"
            "<p>Colateral HYPE: <strong>" + _esc(_fmt_usd(getattr(_pm, "collateral_usd", 0.0)))
            + "</strong></p>"
            "<p class='dim'>Deuda (USDC/USDH): " + _esc(_fmt_usd(getattr(_pm, "debt_usd", 0.0)))
            + " · Capacidad: " + _esc(_fmt_usd(getattr(_pm, "capacity_usd", 0.0)))
            + " · disponible: " + _esc(_fmt_usd(getattr(_pm, "available_usd", 0.0))) + "</p>"
            "<p>Margin ratio: <span class='" + _esc(_status_cls) + "'><strong>"
            + f"{getattr(_pm, 'ratio', 0.0) * 100:.1f}% {_esc(_status)}"
            + "</strong></span> <span class='dim'>(WARN 40% · STRESS 70% · LIQ 95%)</span></p>"
            + _naked
            + "</div>"
        )

    # ─── Próximos catalysts (macro calendar) ──────────────────────────────
    upcoming_rows: list[str] = []
    for ev in state.get("upcoming") or []:
        when = ev.timestamp_utc.strftime("%Y-%m-%d %H:%M UTC")
        upcoming_rows.append(
            f"<p>{_esc(when)} — <strong>{_esc(ev.name)}</strong>"
            f" <span class='dim'>[{_esc(ev.impact_level)}/{_esc(ev.category)}]</span></p>"
        )
    if not upcoming_rows:
        upcoming_rows.append("<p class='dim'>No upcoming events in calendar.</p>")

    # ─── Wallets breakdown ───────────────────────────────────────────────
    # R-DASHBOARD-RABBY-PARITY (2026-05-06): apply canonical labels + dust
    # filter ($50 floor). Wallets carrying < $50 of capital collapse into a
    # single "Dust: $X" footer line so the card is dominated by the wallets
    # that actually matter for risk decisions.
    from auto.wallet_labels import (
        apply_wallet_label as _apply_label_w,
        is_dust as _is_dust_w,
    )
    wallet_rows: list[str] = []
    dust_total = 0.0
    dust_count = 0
    for ws in state.get("wallets") or []:
        cap_val = float(ws.get("capital") or 0.0)
        if cap_val < 0.01:
            continue
        # Pre-canonical: stale env-var label may say e.g. "Reserva histórica"
        # or "Alt Short Bleed v4"; the canonical map fixes this regardless of
        # whether Railway env vars were rotated.
        canonical = _apply_label_w(ws.get("address"), ws.get("label"))
        # Dust gate — every wallet below $50 collapses into the dust footer.
        if _is_dust_w(cap_val):
            dust_total += cap_val
            dust_count += 1
            continue
        parts = []
        if ws.get("perp", 0) > 0.01:
            parts.append(f"Perp {_fmt_compact_usd(ws['perp'])}")
        if ws.get("spot", 0) > 0.01:
            parts.append(f"Spot {_fmt_compact_usd(ws['spot'])}")
        if ws.get("spot_stables", 0) > 0.01:
            parts.append(f"Stables {_fmt_compact_usd(ws['spot_stables'])}")
        if ws.get("hl_coll", 0) > 0.01:
            parts.append(f"HL {_fmt_compact_usd(ws['hl_coll'])}")
        if ws.get("hl_debt", 0) > 0.01:
            parts.append(f"Debt -{_fmt_compact_usd(ws['hl_debt'])}")
        wallet_rows.append(
            f"<p><strong>{_esc(canonical)}</strong>"
            f" <span class='dim'>{_esc(ws.get('short'))}</span> ·"
            f" <strong>{_esc(_fmt_compact_usd(cap_val))}</strong>"
            f"<br><span class='dim'>{' · '.join(parts) if parts else '—'}</span></p>"
        )
    if dust_count > 0:
        wallet_rows.append(
            f"<p class='dim'>Dust ({dust_count} wallet"
            f"{'s' if dust_count != 1 else ''}): "
            f"{_fmt_compact_usd(dust_total)}</p>"
        )
    if not wallet_rows:
        wallet_rows.append("<p class='dim'>No wallets reported.</p>")

    # ─── Spot tokens (R-DASH-FIX Bug 1) ──────────────────────────────────
    # Show ALL spot tokens individually (USDC, USDH, USDT0, kHYPE, etc.)
    # matching /posiciones single-source-of-truth from spot_tokens list.
    DUST_USD = 1.0
    spot_token_rows: list[str] = []
    for st in state.get("spot_tokens") or []:
        if st.get("usd", 0) < DUST_USD:
            continue
        coin = st.get("coin", "?")
        total_amt = float(st.get("total") or 0)
        usd_val = float(st.get("usd") or 0)
        wallets_list = st.get("wallets") or []
        wallet_hint = (
            f" <span class='dim'>[{_esc(', '.join(set(wallets_list)))}]</span>"
            if wallets_list else ""
        )
        spot_token_rows.append(
            f"<p>{_esc(coin)}: "
            f"<strong>{_esc(_fmt_token_amount(total_amt, dec=4))}</strong>"
            f" <span class='dim'>({_esc(_fmt_compact_usd(usd_val))})</span>"
            f"{wallet_hint}</p>"
        )
    if not spot_token_rows:
        spot_token_rows.append("<p class='dim'>No spot tokens.</p>")

    btc = state.get("btc")
    eth = state.get("eth")
    hype = state.get("hype")
    fg_value = state.get("fg_value")
    fg_label = state.get("fg_label")

    # R-SILENT: prices fallback via persistent cache.
    cached_prices = state.get("cached_prices") or {}
    cache_age = cached_prices.get("age_s")
    cached_btc = cached_prices.get("btc")
    cached_eth = cached_prices.get("eth")
    cached_hype = cached_prices.get("hype")

    btc_eff = btc if btc is not None else cached_btc
    eth_eff = eth if eth is not None else cached_eth
    hype_eff = hype if hype is not None else cached_hype

    market_loading = (btc is None and eth is None and hype is None)
    market_html = (
        f"<p>BTC: {_esc(_fmt_usd(btc_eff))}</p>"
        f"<p>ETH: {_esc(_fmt_usd(eth_eff))}</p>"
        f"<p>HYPE: {_esc(_fmt_usd(hype_eff, dec=2))}</p>"
        f"<p>F&amp;G: {_esc(fg_value)} ({_esc(fg_label)})</p>"
    )
    if market_loading:
        if (cached_btc is not None or cached_eth is not None or cached_hype is not None):
            age_min = (cache_age or 0) // 60
            market_html = (
                f"<p class='dim'>⚠️ API down — using cache ({age_min}min ago)</p>"
                + market_html
            )
        else:
            market_html = (
                "<p class='dim'>(API down — no cache available)</p>" + market_html
            )

    html_doc = f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <title>Fondo Black Cat — Dashboard</title>
    <meta http-equiv="refresh" content="60">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        * {{ box-sizing: border-box; }}
        body {{
            background: #0a0a0a;
            color: #00ff88;
            font-family: 'SF Mono', Menlo, Monaco, 'Courier New', monospace;
            padding: 16px;
            margin: 0;
            line-height: 1.5;
        }}
        h1 {{ color: #ffaa00; font-size: 1.4rem; margin: 0 0 4px 0; }}
        h2 {{ color: #ffaa00; font-size: 1.05rem; margin: 0 0 8px 0; }}
        .ts {{ color: #888; font-size: 0.85rem; margin-bottom: 18px; }}
        .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 12px;
        }}
        .card {{
            border: 1px solid #00ff88;
            padding: 14px;
            border-radius: 4px;
            background: #051010;
        }}
        .pos {{ color: #00ff88; }}
        .neg {{ color: #ff4444; }}
        .dim {{ color: #666; font-size: 0.85rem; }}
        p {{ margin: 4px 0; }}
        footer {{ color: #444; font-size: 0.75rem; margin-top: 24px; text-align: center; }}
    </style>
</head>
<body>
    <h1>🐱‍⬛ Fondo Black Cat — Dashboard</h1>
    <div class="ts">{_esc(state["ts"])} · auto-refresh 60s · {_staleness_badge(state)}</div>

    <div class="grid">
        <div class="card">
            <h2>Capital</h2>
            {capital_block_html}
        </div>

        {pm_card_html}

        <div class="card">
            <h2>Main flywheel</h2>
            {flywheel_html}
        </div>

        {secondary_html}

        {closed_html}

        {pear_card_html}

        {vault_card_html}

        <div class="card">
            <h2>Active basket (autodetect)</h2>
            {''.join(basket_rows)}
        </div>

        <div class="card">
            <h2>Market</h2>
            {market_html}
        </div>

        <div class="card">
            <h2>Spot tokens</h2>
            {''.join(spot_token_rows)}
        </div>

        <div class="card">
            <h2>Wallets ({len(state.get('wallets') or [])})</h2>
            {''.join(wallet_rows)}
        </div>

        <div class="card" style="grid-column: 1/-1;">
            <h2>Upcoming catalysts (5)</h2>
            {''.join(upcoming_rows)}
        </div>
    </div>

    <footer>Read-only · single source of truth with /reporte · live on-chain data + cache</footer>
</body>
</html>"""
    return html_doc


async def _build_state() -> dict[str, Any]:
    """Translate ``PortfolioSnapshot`` into the flat dict ``_render_html`` consumes."""
    import asyncio as _asyncio
    import time as _time
    from modules.portfolio_snapshot import build_portfolio_snapshot
    from modules.macro_calendar import upcoming_events

    snap = await build_portfolio_snapshot()
    snap_age = (_time.time() - snap.built_at_ts) if getattr(snap, "built_at_ts", 0) else None

    # R-DASH-FIX Bug 2: fetch fresh wallet data once, reuse for both UPnL
    # (single-source-of-truth with /posiciones) and basket detection (avoids
    # a redundant second fetch_all_wallets() call inside detect_active_baskets).
    from modules.portfolio import fetch_all_wallets as _faw
    fresh_wallets: list[dict[str, Any]] = []
    try:
        fresh_wallets = await _faw()
    except Exception:  # noqa: BLE001
        log.exception("dashboard: fresh fetch_all_wallets failed (non-fatal)")

    # Compute UPnL from fresh wallet data — same formula as /posiciones so
    # the Capital block always agrees with the live /posiciones snapshot.
    upnl_fresh: float = sum(
        float((w.get("data") or {}).get("unrealized_pnl_total") or 0.0)
        for w in (fresh_wallets or [])
        if w.get("status") == "ok"
    )

    # R-DASH-FIX Bug 2: also collect fresh spot_balances for Bug 1 token display.
    # Collect all spot balances across wallets — same source as /posiciones.
    from modules.portfolio_snapshot import _spot_usd_value  # reuse price calc
    prices_for_spot: dict[str, Any] = {}
    if snap.market:
        try:
            from modules.market import fetch_market_data as _fmd
            # Use market data already in the snapshot rather than re-fetching.
            # Reconstruct a prices dict compatible with _current_usd_value.
            _btc = snap.market.btc
            _eth = snap.market.eth
            _hype = snap.market.hype
            if _btc:
                prices_for_spot["BTC"] = {"price_usd": _btc}
            if _eth:
                prices_for_spot["ETH"] = {"price_usd": _eth}
            if _hype:
                prices_for_spot["HYPE"] = {"price_usd": _hype}
        except Exception:  # noqa: BLE001
            pass

    # Build per-coin spot token list from fresh wallet data (Bug 1).
    _all_spot_raw: list[dict[str, Any]] = []
    for w in (fresh_wallets or []):
        if w.get("status") != "ok":
            continue
        d = w.get("data") or {}
        wallet_label = d.get("label") or "?"
        for sb in (d.get("spot_balances") or []):
            _all_spot_raw.append({**sb, "_wallet_label": wallet_label})

    # Aggregate by coin, compute USD value.
    _coin_map: dict[str, dict[str, Any]] = {}
    for sb in _all_spot_raw:
        coin = (sb.get("coin") or "?").upper()
        if coin not in _coin_map:
            _coin_map[coin] = {"coin": coin, "total": 0.0, "entry_ntl": 0.0,
                               "usd": 0.0, "wallets": []}
        amt = float(sb.get("total") or 0)
        entl = float(sb.get("entry_ntl") or 0)
        _coin_map[coin]["total"] += amt
        _coin_map[coin]["entry_ntl"] += entl
        if sb.get("_wallet_label"):
            _coin_map[coin]["wallets"].append(sb["_wallet_label"])
        # USD valuation: stables 1:1, others via price map
        c = coin
        if c in {"USDC", "USDH", "USDT", "USDT0", "DAI"}:
            usd_val = amt
        else:
            lookup = c[1:] if c.startswith("K") else c  # kHYPE → HYPE
            entry = prices_for_spot.get(lookup) or prices_for_spot.get(c) or {}
            px = entry.get("price_usd") if isinstance(entry, dict) else None
            usd_val = amt * float(px) if (px and amt) else entl
        _coin_map[coin]["usd"] += usd_val

    # Sort by USD value desc, keep only tokens >= $1 USD value.
    spot_tokens: list[dict[str, Any]] = sorted(
        [v for v in _coin_map.values() if v["usd"] >= 1.0],
        key=lambda x: -x["usd"],
    )

    # R-SILENT: on-chain basket autodetect — reuse fresh_wallets to avoid a
    # second fetch_all_wallets() call inside detect_active_baskets().
    basket_state: dict[str, Any] = {}
    try:
        from auto.fund_state_v2 import detect_active_baskets

        async def _preloaded_wallets():
            return fresh_wallets

        basket_state = await detect_active_baskets(
            fetch_wallets_fn=_preloaded_wallets if fresh_wallets else None
        )
    except Exception:  # noqa: BLE001
        log.exception("dashboard: fund_state_v2 detect_active_baskets failed (non-fatal)")

    # R-SILENT: persist prices for fallback when fetch_market_data 502s.
    cached_prices: dict[str, Any] = {}
    try:
        from auto import price_cache as _pc
        _pc.record(
            getattr(snap.market, "btc", None),
            getattr(snap.market, "eth", None),
            getattr(snap.market, "hype", None),
        )
        cached_prices = _pc.read()
    except Exception:  # noqa: BLE001
        log.exception("dashboard: price_cache failed (non-fatal)")

    def _ws_to_dict(ws):
        if ws is None:
            return None
        return {
            "address": ws.address,
            "short": ws.short,
            "label": ws.label,
            "hf": ws.health_factor,
            "collateral_symbol": ws.collateral_symbol,
            "collateral_balance": ws.collateral_balance,
            "collateral_usd": ws.hl_collateral_usd,
            "debt_symbol": ws.debt_symbol,
            "debt_balance": ws.debt_balance,
            "debt_usd": ws.hl_debt_usd,
            # R-DASHBOARD-DEBT-SYMBOL: underlying asset address used as
            # short-form fallback when debt_symbol is still None (unknown reserve).
            "debt_asset": ws.debt_asset,
            # R-DASHBOARD-DOUBLECOUNT-FIX (2026-05-06) Bug #2: same surface
            # for collateral so the renderer's _RESERVE_SYMBOLS map can pick
            # the WHYPE label out of 0x5555…5555 when the live entry has
            # no symbol (per-reserve RPC failure).
            "collateral_asset": getattr(ws, "collateral_asset", None),
            # R-DASHBOARD-RABBY-PARITY (2026-05-06): HF cache-fallback fields
            # propagated to the renderer so it can branch UNKNOWN / OK / ZERO
            # and never emit literal NaN.
            "hf_status": getattr(ws, "hf_status", "OK"),
            "last_known_hf": getattr(ws, "last_known_hf", None),
            "age_seconds": getattr(ws, "age_seconds", None),
            "last_known_at_iso": getattr(ws, "last_known_at_iso", None),
            "last_known_collateral_usd": getattr(
                ws, "last_known_collateral_usd", 0.0
            ),
            "last_known_debt_usd": getattr(ws, "last_known_debt_usd", 0.0),
            "recovered_from_cache": getattr(ws, "recovered_from_cache", False),
        }

    return {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "capital_total": snap.capital_total,
        "hl_collateral_total": snap.hl_collateral_total,
        "hl_debt_total": snap.hl_debt_total,
        "perp_equity_total": snap.perp_equity_total,
        # R-DASHBOARD-SPOT-FIX: spot_usd_total now means NON-STABLE only
        # (real exposure). spot_stables_total is the cash-equivalent bucket
        # surfaced separately so the Capital card no longer inflates the
        # "Spot non-USDC" line by counting USDT0/USDH/etc.
        "spot_usd_total": snap.spot_usd_total,
        "spot_stables_total": getattr(snap, "spot_stables_total", 0.0),
        # R-DASHBOARD-RABBY-PARITY (2026-05-06): Pear Protocol staked is
        # surfaced via env var (or future on-chain reader) and folded into
        # the TOTAL EQUITY headline by ``auto.capital_calc.compute_net_capital``.
        "pear_staked_total": getattr(snap, "pear_staked_total", 0.0),
        # R-PEAR-ASSET-INTEGRATION (2026-06-17): live stPEAR balance + price
        # detail + known flag (n/d when the on-chain/price read failed).
        "pear_staked_balance": getattr(snap, "pear_staked_balance", 0.0),
        "pear_staked_price": getattr(snap, "pear_staked_price", 0.0),
        "pear_staked_known": getattr(snap, "pear_staked_known", True),
        # R-VAULTDEP (2026-05-30): fund capital deposited INTO HL vaults,
        # folded into TOTAL EQUITY by compute_net_capital. Read live via
        # the keyless userVaultEquities endpoint in the snapshot builder.
        "vault_deposits_total": getattr(snap, "vault_deposits_total", 0.0),
        # R-VAULTDEP dashboard: per-vault breakdown (label, equity, cost basis,
        # PnL USD/%, evolution vs prior snapshot) feeding the dedicated card.
        "vault_deposits_detail": getattr(snap, "vault_deposits_detail", []) or [],
        # R-PMCORE (2026-06-01): Portfolio Margin state of the primary account.
        "pm_state": getattr(snap, "pm_state", None),
        # R-DASH-FIX Bug 2: use fresh UPnL — same source as /posiciones.
        "upnl_perp_total": upnl_fresh if fresh_wallets else snap.upnl_perp_total,
        "main_flywheel": _ws_to_dict(snap.main_flywheel),
        "secondary_flywheel": _ws_to_dict(snap.secondary_flywheel),
        "basket_positions": [
            {"coin": p["coin"], "upnl": p["unrealized_pnl"], "notional_usd": p["notional_usd"]}
            for p in snap.basket_positions
        ],
        "basket_upnl": snap.basket_upnl,
        "basket_notional": snap.basket_notional,
        "btc": snap.market.btc,
        "eth": snap.market.eth,
        "hype": snap.market.hype,
        "fg_value": snap.market.fear_greed_value,
        "fg_label": snap.market.fear_greed_label,
        "wallets": [
            {
                "address": ws.address,
                "short": ws.short,
                "label": ws.label,
                "capital": ws.capital_total,
                "perp": ws.perp_equity,
                # R-DASHBOARD-SPOT-FIX: ``spot`` carries non-stable only.
                "spot": ws.spot_usd,
                "spot_stables": getattr(ws, "spot_stables_usd", 0.0),
                "hl_coll": ws.hl_collateral_usd,
                "hl_debt": ws.hl_debt_usd,
            }
            for ws in snap.wallets
        ],
        "upcoming": upcoming_events(limit=5),
        # HOTFIX 2: staleness metadata for badge + cold-start screen
        "snap_age_sec": snap_age,
        "is_fresh": getattr(snap, "is_fresh", True),
        "last_error": getattr(snap, "last_error", None),
        # R-SILENT: on-chain basket autodetect + price cache fallback.
        "basket_state": basket_state,
        "cached_prices": cached_prices,
        # R-DASH-FIX Bug 1: per-coin spot token list for the Spot Tokens card.
        "spot_tokens": spot_tokens,
    }


async def dashboard_handler(request):
    """aiohttp handler. Mounted by health_server on the same app."""
    if not _enabled():
        from aiohttp import web
        return web.Response(
            text="Dashboard disabled (set DASHBOARD_TOKEN env to enable)",
            status=503,
        )

    from aiohttp import web
    token = request.query.get("token", "").strip()
    expected = _get_token()
    if not token or token != expected:
        return web.Response(text="Unauthorized", status=401)

    try:
        state = await _build_state()
        body = _render_html(state)
        return web.Response(text=body, content_type="text/html")
    except Exception:
        log.exception("dashboard render failed")
        from aiohttp import web
        return web.Response(text="Error rendering dashboard", status=500)
