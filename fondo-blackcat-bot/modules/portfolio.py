"""HyperLiquid portfolio reader.

Reads perp positions, margin summary, spot balances and funding rates
for the Fondo Black Cat wallets via the public HyperLiquid `/info` endpoint.

RULES (del CLAUDE.md/spec):
- Solo reportar posiciones PERPETUAS (spot+perp duplica si mezclamos).
- Margin usage -200% es normal, no alertar.
- PnL se evalúa a nivel BASKET CROSS, no por posición individual.
- Wallet DreamCash (0x171b...) puede no mostrar perps si está en HIP-3.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from config import FUND_WALLETS, HYPERLIQUID_RPC

log = logging.getLogger(__name__)

INFO_URL = f"{HYPERLIQUID_RPC.rstrip('/')}/info"
TIMEOUT = httpx.Timeout(15.0, connect=10.0)


async def _post(client: httpx.AsyncClient, payload: dict[str, Any]) -> Any:
    try:
        r = await client.post(INFO_URL, json=payload, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("hyperliquid /info failed (%s): %s", payload.get("type"), e)
        return None


async def get_clearinghouse_state(client: httpx.AsyncClient, wallet: str) -> dict | None:
    return await _post(client, {"type": "clearinghouseState", "user": wallet})


async def get_spot_state(client: httpx.AsyncClient, wallet: str) -> dict | None:
    return await _post(client, {"type": "spotClearinghouseState", "user": wallet})


async def get_meta_and_ctxs(client: httpx.AsyncClient) -> list | None:
    """Returns [universe_meta, asset_ctxs]. asset_ctxs[i] has funding, markPx, openInterest."""
    return await _post(client, {"type": "metaAndAssetCtxs"})


async def get_all_mids(client: httpx.AsyncClient) -> dict[str, str] | None:
    return await _post(client, {"type": "allMids"})


def _summarize_wallet(wallet: str, label: str, chs: dict | None) -> dict:
    """Collapse clearinghouseState into the fields the report needs."""
    out = {
        "wallet": wallet,
        "label": label,
        "account_value": 0.0,
        "total_ntl_pos": 0.0,
        "total_margin_used": 0.0,
        "withdrawable": 0.0,
        "leverage": 0.0,
        "upnl_total": 0.0,
        "positions": [],
        "bias": "—",
        "error": None,
    }
    if not chs:
        out["error"] = "no response"
        return out

    ms = chs.get("marginSummary") or {}
    try:
        out["account_value"] = float(ms.get("accountValue") or 0)
        out["total_ntl_pos"] = float(ms.get("totalNtlPos") or 0)
        out["total_margin_used"] = float(ms.get("totalMarginUsed") or 0)
        out["withdrawable"] = float(chs.get("withdrawable") or 0)
    except (TypeError, ValueError):
        pass

    if out["account_value"] > 0:
        out["leverage"] = out["total_ntl_pos"] / out["account_value"]

    longs = 0.0
    shorts = 0.0
    for asset in chs.get("assetPositions") or []:
        p = asset.get("position") or {}
        try:
            szi = float(p.get("szi") or 0)  # signed size
            entry = float(p.get("entryPx") or 0)
            upnl = float(p.get("unrealizedPnl") or 0)
            mark = 0.0
            if szi != 0 and entry != 0:
                # approximate mark from upnl: mark = entry + upnl/szi
                mark = entry + (upnl / szi)
            liq = p.get("liquidationPx")
            liq = float(liq) if liq else None
            lev = (p.get("leverage") or {}).get("value")
            lev = float(lev) if lev else None
            out["positions"].append({
                "coin": p.get("coin"),
                "szi": szi,
                "side": "LONG" if szi > 0 else "SHORT",
                "notional": abs(szi) * (mark or entry),
                "entry": entry,
                "mark": mark,
                "upnl": upnl,
                "liq_px": liq,
                "leverage": lev,
                "margin_used": float(p.get("marginUsed") or 0),
                "return_pct": float(p.get("returnOnEquity") or 0),
            })
            out["upnl_total"] += upnl
            if szi > 0:
                longs += abs(szi) * (mark or entry)
            else:
                shorts += abs(szi) * (mark or entry)
        except (TypeError, ValueError) as e:
            log.debug("bad position payload: %s", e)

    if longs == 0 and shorts == 0:
        out["bias"] = "FLAT"
    elif shorts > longs * 1.2:
        out["bias"] = "SHORT"
    elif longs > shorts * 1.2:
        out["bias"] = "LONG"
    else:
        out["bias"] = "MIXED"
    return out


async def fetch_all_wallets() -> dict:
    """Fetch the full fund snapshot. Returns dict with per-wallet and totals."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        tasks = [get_clearinghouse_state(client, w) for w in FUND_WALLETS]
        states = await asyncio.gather(*tasks, return_exceptions=True)
        meta = await get_meta_and_ctxs(client)
        mids = await get_all_mids(client)

    wallets = []
    total_equity = 0.0
    total_upnl = 0.0
    total_notional = 0.0
    for (addr, label), state in zip(FUND_WALLETS.items(), states):
        if isinstance(state, Exception):
            state = None
        summary = _summarize_wallet(addr, label, state)
        wallets.append(summary)
        total_equity += summary["account_value"]
        total_upnl += summary["upnl_total"]
        total_notional += summary["total_ntl_pos"]

    funding = {}
    if meta and isinstance(meta, list) and len(meta) >= 2:
        universe = (meta[0] or {}).get("universe") or []
        ctxs = meta[1] or []
        for u, ctx in zip(universe, ctxs):
            coin = u.get("name")
            if not coin or not ctx:
                continue
            try:
                funding[coin] = {
                    "funding": float(ctx.get("funding") or 0),
                    "mark": float(ctx.get("markPx") or 0),
                    "oi": float(ctx.get("openInterest") or 0),
                    "prev_day": float(ctx.get("prevDayPx") or 0),
                }
            except (TypeError, ValueError):
                continue

    return {
        "wallets": wallets,
        "totals": {
            "equity": total_equity,
            "upnl": total_upnl,
            "notional": total_notional,
            "gross_leverage": (total_notional / total_equity) if total_equity else 0,
        },
        "funding": funding,
        "mids": mids or {},
    }


async def get_price(coin: str) -> float | None:
    """Quick helper: current mark price for a coin (BTC, ETH, HYPE, ...)."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        mids = await get_all_mids(client)
    if not mids:
        return None
    val = mids.get(coin)
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def format_quick_positions(snapshot: dict, hf: float | None = None) -> str:
    """Compact Telegram-safe snapshot for /posiciones."""
    lines = ["📊 POSICIONES — FONDO BLACK CAT", ""]
    for w in snapshot["wallets"]:
        short_addr = w["wallet"][:6] + "…" + w["wallet"][-4:]
        lines.append(f"🔹 {w['label']}  ({short_addr})")
        if w.get("error"):
            lines.append(f"   ⚠️ {w['error']}")
            continue
        lines.append(
            f"   Equity ${w['account_value']:,.0f}  |  UPnL ${w['upnl_total']:+,.0f}  |  Lev {w['leverage']:.2f}x  |  {w['bias']}"
        )
        for p in w["positions"][:6]:
            liq = f" liq@${p['liq_px']:,.4f}" if p.get("liq_px") else ""
            lines.append(
                f"     • {p['side']} {p['coin']}  notional ${p['notional']:,.0f}  UPnL ${p['upnl']:+,.0f}{liq}"
            )
    t = snapshot["totals"]
    lines.append("")
    lines.append(
        f"Σ Equity ${t['equity']:,.0f}  |  UPnL ${t['upnl']:+,.0f}  |  Notional ${t['notional']:,.0f}  |  Lev {t['gross_leverage']:.2f}x"
    )
    if hf is not None:
        emoji = "🟢" if hf >= 1.20 else ("🟡" if hf >= 1.10 else "🔴")
        lines.append(f"{emoji} HyperLend HF: {hf:.2f}")
    return "\n".join(lines)
