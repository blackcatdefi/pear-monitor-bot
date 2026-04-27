"""Round 17 — Read-only HTML dashboard servido por aiohttp.

URL:  GET /dashboard?token=XXX
Auth simple por token (env DASHBOARD_TOKEN). Si DASHBOARD_TOKEN está vacío,
endpoint devuelve 503 "disabled".

Auto-refresh 60s (meta http-equiv="refresh").

Reusa los mismos fetchers que /status_quick para evitar duplicar lógica.
"""
from __future__ import annotations

import asyncio
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


async def _safe(coro, label: str):
    try:
        return await coro
    except Exception as exc:
        log.warning("dashboard %s failed: %s", label, exc)
        return None


def _esc(v: Any) -> str:
    return html.escape(str(v)) if v is not None else "—"


def _fmt_usd(v: Any, dec: int = 0) -> str:
    try:
        return f"${float(v):,.{dec}f}"
    except Exception:
        return "—"


def _signed(v: Any) -> tuple[str, str]:
    """Return (cls, formatted)."""
    try:
        f = float(v)
        sign = "+" if f >= 0 else "-"
        cls = "pos" if f >= 0 else "neg"
        return cls, f"{sign}${abs(f):,.2f}"
    except Exception:
        return "", "—"


async def _gather_state() -> dict[str, Any]:
    from modules.hyperlend import fetch_all_hyperlend
    from modules.market import fetch_market_data
    from modules.portfolio import fetch_all_wallets
    from modules.macro_calendar import upcoming_events

    hl, market, wallets = await asyncio.gather(
        _safe(fetch_all_hyperlend(), "hyperlend"),
        _safe(fetch_market_data(), "market"),
        _safe(fetch_all_wallets(), "wallets"),
    )

    # Capital
    hl_collateral = 0.0
    hl_debt = 0.0
    flywheel_hf = None
    flywheel_collateral_bal = None
    flywheel_debt_sym = None
    flywheel_debt_bal = None
    if isinstance(hl, list):
        for r in hl:
            if r.get("status") != "ok":
                continue
            d = r["data"]
            hl_collateral += float(d.get("total_collateral_usd") or 0)
            hl_debt += float(d.get("total_debt_usd") or 0)
            if (d.get("total_debt_usd") or 0) > 0 and flywheel_hf is None:
                flywheel_hf = d.get("health_factor")
                flywheel_collateral_bal = d.get("collateral_balance")
                flywheel_debt_sym = d.get("debt_symbol")
                flywheel_debt_bal = d.get("debt_balance")

    perp_acct = 0.0
    perp_upnl = 0.0
    basket_positions: list[dict] = []
    if isinstance(wallets, list):
        try:
            from fund_state import BASKET_PERP_TOKENS
        except Exception:
            BASKET_PERP_TOKENS = []  # type: ignore
        for w in wallets:
            if w.get("status") != "ok":
                continue
            d = w.get("data", {})
            perp_acct += float(d.get("account_value") or 0)
            perp_upnl += float(d.get("unrealized_pnl_total") or 0)
            for pos in d.get("positions") or []:
                coin = (pos.get("coin") or "").upper()
                if coin in BASKET_PERP_TOKENS:
                    basket_positions.append({
                        "symbol": coin,
                        "upnl": float(pos.get("unrealized_pnl") or 0),
                        "ntl": float(pos.get("position_value") or pos.get("ntl_pos") or 0),
                    })

    capital_total = hl_collateral - hl_debt + perp_acct

    btc = eth = hype = None
    fg_value = fg_label = None
    if isinstance(market, dict) and market.get("status") == "ok":
        prices = market["data"].get("prices", {})
        btc = (prices.get("bitcoin") or {}).get("usd")
        eth = (prices.get("ethereum") or {}).get("usd")
        hype = (prices.get("hyperliquid") or {}).get("usd")
        fg = market["data"].get("fear_greed") or {}
        fg_value = fg.get("value")
        fg_label = fg.get("classification") or fg.get("label")

    upcoming = upcoming_events(limit=5)

    return {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "capital_total": capital_total,
        "hl_collateral": hl_collateral,
        "hl_debt": hl_debt,
        "perp_acct": perp_acct,
        "perp_upnl": perp_upnl,
        "flywheel_hf": flywheel_hf,
        "flywheel_collateral_bal": flywheel_collateral_bal,
        "flywheel_debt_sym": flywheel_debt_sym,
        "flywheel_debt_bal": flywheel_debt_bal,
        "basket_positions": basket_positions,
        "btc": btc,
        "eth": eth,
        "hype": hype,
        "fg_value": fg_value,
        "fg_label": fg_label,
        "upcoming": upcoming,
    }


def _render_html(state: dict[str, Any]) -> str:
    cap_cls, cap_fmt = _signed(state["capital_total"])  # capital is positive usually but format helper handles sign
    cap_total = _fmt_usd(state["capital_total"])

    upnl_cls, upnl_fmt = _signed(state["perp_upnl"])

    hf_str = "—"
    if state.get("flywheel_hf") is not None:
        try:
            hf_str = f"{float(state['flywheel_hf']):.3f}"
        except Exception:
            pass

    coll_bal = state.get("flywheel_collateral_bal")
    debt_bal = state.get("flywheel_debt_bal")
    debt_sym = state.get("flywheel_debt_sym") or ""

    basket_rows = []
    for p in state["basket_positions"]:
        cls, fmt = _signed(p["upnl"])
        basket_rows.append(
            f"<p>{_esc(p['symbol'])}: <span class='{cls}'>{_esc(fmt)}</span>"
            f" <span class='dim'>(ntl ${abs(p['ntl']):,.0f})</span></p>"
        )
    if not basket_rows:
        basket_rows.append("<p class='dim'>Sin posiciones SHORT activas (notional &gt;$50)</p>")

    upcoming_rows = []
    for ev in state.get("upcoming") or []:
        when = ev.timestamp_utc.strftime("%Y-%m-%d %H:%M UTC")
        upcoming_rows.append(
            f"<p>{_esc(when)} — <strong>{_esc(ev.name)}</strong>"
            f" <span class='dim'>[{_esc(ev.impact_level)}/{_esc(ev.category)}]</span></p>"
        )
    if not upcoming_rows:
        upcoming_rows.append("<p class='dim'>Sin eventos próximos en calendar.</p>")

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
    <div class="ts">{_esc(state["ts"])} · auto-refresh 60s</div>

    <div class="grid">
        <div class="card">
            <h2>Capital</h2>
            <p>Total: <strong>{_esc(cap_total)}</strong></p>
            <p>HL collateral: {_esc(_fmt_usd(state["hl_collateral"]))}</p>
            <p>HL debt: {_esc(_fmt_usd(state["hl_debt"]))}</p>
            <p>Perp acct value: {_esc(_fmt_usd(state["perp_acct"]))}</p>
            <p>UPnL perp: <span class="{upnl_cls}">{_esc(upnl_fmt)}</span></p>
        </div>

        <div class="card">
            <h2>Flywheel HyperLend</h2>
            <p>HF: <strong>{_esc(hf_str)}</strong></p>
            <p>Colateral: {_esc(f"{float(coll_bal):.2f}" if coll_bal else "—")} kHYPE</p>
            <p>Deuda: {_esc(f"{float(debt_bal):.4f}" if debt_bal else "—")} {_esc(debt_sym)}</p>
        </div>

        <div class="card">
            <h2>Basket v5</h2>
            {''.join(basket_rows)}
        </div>

        <div class="card">
            <h2>Mercado</h2>
            <p>BTC: {_esc(_fmt_usd(state["btc"]))}</p>
            <p>ETH: {_esc(_fmt_usd(state["eth"]))}</p>
            <p>HYPE: {_esc(_fmt_usd(state["hype"], dec=2))}</p>
            <p>F&amp;G: {_esc(state["fg_value"])} ({_esc(state["fg_label"])})</p>
        </div>

        <div class="card" style="grid-column: 1/-1;">
            <h2>Próximos catalysts (5)</h2>
            {''.join(upcoming_rows)}
        </div>
    </div>

    <footer>Read-only · token-based auth · datos en vivo on-chain + cache</footer>
</body>
</html>"""
    return html_doc


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
        state = await _gather_state()
        body = _render_html(state)
        return web.Response(text=body, content_type="text/html")
    except Exception:
        log.exception("dashboard render failed")
        from aiohttp import web
        return web.Response(text="Error rendering dashboard", status=500)
