"""
HyperLiquid portfolio reader.

Lee posiciones perpetuas y spot de todas las wallets del fondo usando la API
pública de HyperLiquid (POST /info).

REGLAS (del spec):
- Solo reportar posiciones PERPETUAS (HyperDash double-counts spot+futures).
- Margin usage puede ser hasta -200% → NORMAL, no alertar.
- PnL se evalúa a nivel basket cross, no por posición individual.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from config import FUND_WALLETS, HYPERLIQUID_INFO_URL

log = logging.getLogger(__name__)


async def _post_info(client: httpx.AsyncClient, payload: dict) -> Any:
    """POST a HyperLiquid /info con retries básicos."""
    for attempt in range(3):
        try:
            resp = await client.post(HYPERLIQUID_INFO_URL, json=payload, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:  # noqa: BLE001
            log.warning("hyperliquid /info attempt %d failed: %s", attempt + 1, e)
            await asyncio.sleep(2 ** attempt)
    return None


async def fetch_clearinghouse_state(client: httpx.AsyncClient, wallet: str) -> dict | None:
    """Perps state: assetPositions + marginSummary."""
    return await _post_info(client, {"type": "clearinghouseState", "user": wallet})


async def fetch_spot_state(client: httpx.AsyncClient, wallet: str) -> dict | None:
    return await _post_info(client, {"type": "spotClearinghouseState", "user": wallet})


async def fetch_meta_and_ctxs(client: httpx.AsyncClient) -> list | None:
    """Metadata + funding rates de todos los assets."""
    return await _post_info(client, {"type": "metaAndAssetCtxs"})


async def fetch_user_fills(client: httpx.AsyncClient, wallet: str) -> list | None:
    return await _post_info(client, {"type": "userFills", "user": wallet})


def _parse_positions(ch_state: dict | None) -> list[dict]:
    """Extrae posiciones perpetuas limpias para el reporte."""
    if not ch_state:
        return []
    out = []
    for ap in ch_state.get("assetPositions", []) or []:
        pos = ap.get("position") or {}
        coin = pos.get("coin")
        szi = float(pos.get("szi") or 0)
        if szi == 0:
            continue
        lev = pos.get("leverage") or {}
        out.append({
            "coin": coin,
            "szi": szi,
            "side": "LONG" if szi > 0 else "SHORT",
            "entry": float(pos.get("entryPx") or 0),
            "unrealized_pnl": float(pos.get("unrealizedPnl") or 0),
            "return_on_equity": float(pos.get("returnOnEquity") or 0),
            "liquidation_px": float(pos.get("liquidationPx") or 0) if pos.get("liquidationPx") else 0,
            "position_value": float(pos.get("positionValue") or 0),
            "leverage_type": lev.get("type"),
            "leverage_value": lev.get("value"),
            "margin_used": float(pos.get("marginUsed") or 0),
        })
    return out


def _parse_margin_summary(ch_state: dict | None) -> dict:
    if not ch_state:
        return {}
    ms = ch_state.get("marginSummary") or {}
    cross = ch_state.get("crossMarginSummary") or {}
    return {
        "account_value": float(ms.get("accountValue") or 0),
        "total_margin_used": float(ms.get("totalMarginUsed") or 0),
        "total_ntl_pos": float(ms.get("totalNtlPos") or 0),
        "total_raw_usd": float(ms.get("totalRawUsd") or 0),
        "cross_account_value": float(cross.get("accountValue") or 0),
        "withdrawable": float(ch_state.get("withdrawable") or 0),
    }


async def fetch_wallet(client: httpx.AsyncClient, wallet: str, label: str) -> dict:
    ch = await fetch_clearinghouse_state(client, wallet)
    spot = await fetch_spot_state(client, wallet)
    positions = _parse_positions(ch)
    summary = _parse_margin_summary(ch)

    # UPnL agregado
    upnl_total = sum(p["unrealized_pnl"] for p in positions)

    # Leverage efectivo
    equity = summary.get("account_value") or 0
    ntl = summary.get("total_ntl_pos") or 0
    effective_leverage = (ntl / equity) if equity > 0 else 0

    # Bias (LONG/SHORT) por posición dominante
    long_notional = sum(p["position_value"] for p in positions if p["side"] == "LONG")
    short_notional = sum(p["position_value"] for p in positions if p["side"] == "SHORT")
    if short_notional > long_notional * 1.1:
        bias = "SHORT"
    elif long_notional > short_notional * 1.1:
        bias = "LONG"
    elif positions:
        bias = "NEUTRAL"
    else:
        bias = "NO POS"

    return {
        "wallet": wallet,
        "label": label,
        "positions": positions,
        "summary": summary,
        "spot": spot,
        "upnl_total": upnl_total,
        "effective_leverage": effective_leverage,
        "bias": bias,
    }


async def fetch_all_wallets() -> list[dict]:
    """Devuelve snapshot de todas las wallets del fondo."""
    async with httpx.AsyncClient() as client:
        tasks = [fetch_wallet(client, w, label) for w, label in FUND_WALLETS.items()]
        return await asyncio.gather(*tasks)


async def fetch_funding_context() -> dict | None:
    """metaAndAssetCtxs → dict {coin: {funding, openInterest, markPx, ...}}."""
    async with httpx.AsyncClient() as client:
        data = await fetch_meta_and_ctxs(client)
    if not data or not isinstance(data, list) or len(data) < 2:
        return None
    meta, ctxs = data[0], data[1]
    universe = meta.get("universe", [])
    out = {}
    for asset, ctx in zip(universe, ctxs):
        coin = asset.get("name")
        if not coin:
            continue
        out[coin] = {
            "funding": float(ctx.get("funding") or 0),
            "open_interest": float(ctx.get("openInterest") or 0),
            "mark_px": float(ctx.get("markPx") or 0),
            "prev_day_px": float(ctx.get("prevDayPx") or 0),
            "oracle_px": float(ctx.get("oraclePx") or 0),
            "day_volume": float(ctx.get("dayNtlVlm") or 0),
            "premium": float(ctx.get("premium") or 0),
        }
    return out


def format_quick_positions(snapshots: list[dict]) -> str:
    """Formato corto para /posiciones — texto plano para Telegram."""
    lines = ["📊 *PORTFOLIO SNAPSHOT*", ""]
    total_equity = 0.0
    total_upnl = 0.0
    for s in snapshots:
        eq = s["summary"].get("account_value", 0)
        upnl = s["upnl_total"]
        total_equity += eq
        total_upnl += upnl
        lines.append(
            f"• {s['label']} `{s['wallet'][:6]}...{s['wallet'][-4:]}`\n"
            f"   Equity: ${eq:,.0f} | UPnL: ${upnl:,.0f} | "
            f"Lev: {s['effective_leverage']:.1f}x | Bias: {s['bias']} | "
            f"Pos: {len(s['positions'])}"
        )
    lines.append("")
    lines.append(f"*TOTAL Equity:* ${total_equity:,.0f}")
    lines.append(f"*TOTAL UPnL:* ${total_upnl:,.0f}")
    return "\n".join(lines)
