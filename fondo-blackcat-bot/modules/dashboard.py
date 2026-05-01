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
    <p style="color:#ffaa00; font-size:18px;"><span class="spin">⏳</span> Inicializando dashboard…</p>
    <p style="color:#888; font-size:14px;">Cargando datos desde HyperEVM RPC. Esto puede tomar 10-25s en cold start.</p>
    <p style="color:#888; font-size:12px;">La página se refresca automáticamente cada 10s.</p>
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
    upnl_cls, upnl_fmt = _signed(state["upnl_perp_total"])

    # ─── Flywheel principal ───────────────────────────────────────────────
    main = state.get("main_flywheel")
    if main is not None:
        try:
            hf_str = f"{float(main['hf']):.3f}" if main.get("hf") is not None else "—"
        except Exception:
            hf_str = "—"
        coll_amt = _fmt_token_amount(main.get("collateral_balance"), dec=2) \
            if main.get("collateral_balance") else "—"
        coll_sym = main.get("collateral_symbol") or "?"
        debt_amt = _fmt_token_amount(main.get("debt_balance"), dec=4) \
            if main.get("debt_balance") else "—"
        debt_sym = main.get("debt_symbol") or "?"
        flywheel_html = (
            f"<p>Wallet: <span class='dim'>{_esc(main.get('short'))}</span></p>"
            f"<p>HF: <strong>{_esc(hf_str)}</strong></p>"
            f"<p>Colateral: {_esc(coll_amt)} {_esc(coll_sym)}"
            f" <span class='dim'>({_esc(_fmt_compact_usd(main.get('collateral_usd')))})</span></p>"
            f"<p>Deuda: {_esc(debt_amt)} {_esc(debt_sym)}"
            f" <span class='dim'>({_esc(_fmt_compact_usd(main.get('debt_usd')))})</span></p>"
        )
    else:
        flywheel_html = "<p class='dim'>Sin flywheel HyperLend activo (sin deuda).</p>"

    # Secondary flywheel (chico) — solo si existe
    sec = state.get("secondary_flywheel")
    secondary_html = ""
    if sec is not None:
        try:
            hf_str2 = f"{float(sec['hf']):.3f}" if sec.get("hf") is not None else "—"
        except Exception:
            hf_str2 = "—"
        secondary_html = (
            "<div class='card'>"
            "<h2>Flywheel secundario</h2>"
            f"<p>Wallet: <span class='dim'>{_esc(sec.get('short'))}</span></p>"
            f"<p>HF: <strong>{_esc(hf_str2)}</strong></p>"
            f"<p>Colateral: {_esc(_fmt_token_amount(sec.get('collateral_balance'), dec=4))}"
            f" {_esc(sec.get('collateral_symbol') or '?')}"
            f" <span class='dim'>({_esc(_fmt_compact_usd(sec.get('collateral_usd')))})</span></p>"
            f"<p>Deuda: {_esc(_fmt_token_amount(sec.get('debt_balance'), dec=4))}"
            f" {_esc(sec.get('debt_symbol') or '?')}"
            f" <span class='dim'>({_esc(_fmt_compact_usd(sec.get('debt_usd')))})</span></p>"
            "</div>"
        )

    # ─── Basket activa (R-SILENT autodetect) ─────────────────────────────
    # Datos vienen de auto.fund_state_v2.detect_active_baskets() (on-chain).
    # No hardcodeamos número de basket — eso es metadata humana, no del bot.
    basket_rows: list[str] = []
    basket_state = state.get("basket_state") or {}
    active_wallets = []
    for addr, w in (basket_state.get("wallets") or {}).items():
        if w.get("status") == "ACTIVE":
            active_wallets.append((addr, w))

    if active_wallets:
        # Por wallet → mostrar shorts ordenados por notional desc
        for addr, w in active_wallets:
            short_addr = addr[:6] + "…" + addr[-4:] if len(addr) >= 10 else addr
            basket_rows.append(
                f"<p>Wallet: <span class='dim'>{_esc(short_addr)}</span>"
                f" <strong>{_esc(w.get('label', ''))}</strong></p>"
            )
            # Sort by notional desc
            shorts = sorted(
                w.get("shorts") or [],
                key=lambda s: float(s.get("ntl") or 0.0),
                reverse=True,
            )
            for s in shorts:
                # Find UPnL for this position from snap.basket_positions if available
                upnl = None
                for bp in state.get("basket_positions") or []:
                    if str(bp.get("coin", "")).upper() == str(s.get("coin", "")).upper():
                        upnl = bp.get("upnl")
                        break
                cls, fmt = _signed(upnl) if upnl is not None else ("dim", "—")
                basket_rows.append(
                    f"<p>&nbsp;&nbsp;{_esc(s.get('coin'))} SHORT"
                    f" <span class='{cls}'>{_esc(fmt)}</span>"
                    f" <span class='dim'>(ntl ${float(s.get('ntl') or 0.0):,.0f})</span></p>"
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
            "<p class='dim'>Sin posiciones abiertas en wallets del fondo.</p>"
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
        upcoming_rows.append("<p class='dim'>Sin eventos próximos en calendar.</p>")

    # ─── Wallets breakdown ───────────────────────────────────────────────
    wallet_rows: list[str] = []
    for ws in state.get("wallets") or []:
        if ws.get("capital") < 0.01:
            continue
        parts = []
        if ws.get("perp", 0) > 0.01:
            parts.append(f"Perp {_fmt_compact_usd(ws['perp'])}")
        if ws.get("spot", 0) > 0.01:
            parts.append(f"Spot {_fmt_compact_usd(ws['spot'])}")
        if ws.get("hl_coll", 0) > 0.01:
            parts.append(f"HL {_fmt_compact_usd(ws['hl_coll'])}")
        if ws.get("hl_debt", 0) > 0.01:
            parts.append(f"Debt -{_fmt_compact_usd(ws['hl_debt'])}")
        wallet_rows.append(
            f"<p><strong>{_esc(ws.get('label'))}</strong>"
            f" <span class='dim'>{_esc(ws.get('short'))}</span> ·"
            f" <strong>{_esc(_fmt_compact_usd(ws.get('capital')))}</strong>"
            f"<br><span class='dim'>{' · '.join(parts) if parts else '—'}</span></p>"
        )
    if not wallet_rows:
        wallet_rows.append("<p class='dim'>Sin wallets reportadas.</p>")

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
                f"<p class='dim'>⚠️ API down — usando cache (hace {age_min}min)</p>"
                + market_html
            )
        else:
            market_html = (
                "<p class='dim'>(API down — sin cache disponible)</p>" + market_html
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
            <p>Total: <strong>{_esc(cap_total)}</strong></p>
            <p>HL collateral: {_esc(_fmt_compact_usd(state["hl_collateral_total"]))}</p>
            <p>HL debt: {_esc(_fmt_compact_usd(state["hl_debt_total"]))}</p>
            <p>Account Value (perp+USDC unif.): {_esc(_fmt_compact_usd(state["perp_equity_total"]))}</p>
            <p>Spot non-USDC: {_esc(_fmt_compact_usd(state["spot_usd_total"]))}</p>
            <p>UPnL perp: <span class="{upnl_cls}">{_esc(upnl_fmt)}</span></p>
        </div>

        <div class="card">
            <h2>Flywheel principal</h2>
            {flywheel_html}
        </div>

        {secondary_html}

        <div class="card">
            <h2>Basket activa (autodetect)</h2>
            {''.join(basket_rows)}
        </div>

        <div class="card">
            <h2>Mercado</h2>
            {market_html}
        </div>

        <div class="card">
            <h2>Wallets ({len(state.get('wallets') or [])})</h2>
            {''.join(wallet_rows)}
        </div>

        <div class="card" style="grid-column: 1/-1;">
            <h2>Próximos catalysts (5)</h2>
            {''.join(upcoming_rows)}
        </div>
    </div>

    <footer>Read-only · single-source-of-truth con /reporte · datos en vivo on-chain + cache</footer>
</body>
</html>"""
    return html_doc


async def _build_state() -> dict[str, Any]:
    """Translate ``PortfolioSnapshot`` into the flat dict ``_render_html`` consumes."""
    import time as _time
    from modules.portfolio_snapshot import build_portfolio_snapshot
    from modules.macro_calendar import upcoming_events

    snap = await build_portfolio_snapshot()
    snap_age = (_time.time() - snap.built_at_ts) if getattr(snap, "built_at_ts", 0) else None

    # R-SILENT: on-chain basket autodetect (single source of truth).
    basket_state: dict[str, Any] = {}
    try:
        from auto.fund_state_v2 import detect_active_baskets
        basket_state = await detect_active_baskets()
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
        }

    return {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "capital_total": snap.capital_total,
        "hl_collateral_total": snap.hl_collateral_total,
        "hl_debt_total": snap.hl_debt_total,
        "perp_equity_total": snap.perp_equity_total,
        "spot_usd_total": snap.spot_usd_total,
        "upnl_perp_total": snap.upnl_perp_total,
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
                "spot": ws.spot_usd,
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
