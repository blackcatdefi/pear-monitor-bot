"""HyperLiquid portfolio reader for all fund wallets.

Docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from config import FUND_WALLETS, HYPERLIQUID_API
from utils.http import post_json

log = logging.getLogger(__name__)

INFO_URL = f"{HYPERLIQUID_API}/info"


async def _info(payload: dict[str, Any]) -> Any:
    return await post_json(INFO_URL, payload)


async def clearinghouse_state(wallet: str) -> dict[str, Any]:
    return await _info({"type": "clearinghouseState", "user": wallet})


async def spot_state(wallet: str) -> dict[str, Any]:
    return await _info({"type": "spotClearinghouseState", "user": wallet})


async def meta_and_asset_ctxs() -> list[Any]:
    return await _info({"type": "metaAndAssetCtxs"})


async def user_fills(wallet: str) -> list[dict[str, Any]]:
    return await _info({"type": "userFills", "user": wallet})


def _summarize_positions(state: dict[str, Any]) -> dict[str, Any]:
    """Extract a compact summary from clearinghouseState response."""
    margin = state.get("marginSummary", {}) or {}
    cross = state.get("crossMarginSummary", {}) or {}
    asset_positions = state.get("assetPositions", []) or []
    positions: list[dict[str, Any]] = []
    unrealized_total = 0.0
    for ap in asset_positions:
        p = ap.get("position", {}) or {}
        try:
            szi = float(p.get("szi", 0) or 0)
        except (TypeError, ValueError):
            szi = 0.0
        if szi == 0:
            continue
        try:
            unrealized = float(p.get("unrealizedPnl", 0) or 0)
        except (TypeError, ValueError):
            unrealized = 0.0
        try:
            entry = float(p.get("entryPx", 0) or 0)
        except (TypeError, ValueError):
            entry = 0.0
        try:
            notional = float(p.get("positionValue", 0) or 0)
        except (TypeError, ValueError):
            notional = 0.0
        try:
            liq_px_raw = p.get("liquidationPx")
            liq_px = float(liq_px_raw) if liq_px_raw not in (None, "", "null") else None
        except (TypeError, ValueError):
            liq_px = None
        leverage = p.get("leverage", {}) or {}
        positions.append({
            "coin": p.get("coin", "?"),
            "size": szi,
            "side": "LONG" if szi > 0 else "SHORT",
            "entry_px": entry,
            "notional_usd": notional,
            "unrealized_pnl": unrealized,
            "liq_px": liq_px,
            "leverage": leverage.get("value"),
            "leverage_type": leverage.get("type"),
        })
        unrealized_total += unrealized

    def _f(v: Any) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    return {
        "account_value": _f(margin.get("accountValue")),
        "total_ntl_pos": _f(margin.get("totalNtlPos")),
        "total_margin_used": _f(margin.get("totalMarginUsed")),
        "cross_account_value": _f(cross.get("accountValue")),
        "withdrawable": _f(state.get("withdrawable", 0)),
        "positions": positions,
        "unrealized_pnl_total": unrealized_total,
    }


async def fetch_wallet(wallet: str, label: str) -> dict[str, Any]:
    """Fetch one wallet; returns {status, data|error}."""
    try:
        state = await clearinghouse_state(wallet)
        summary = _summarize_positions(state)
        summary["wallet"] = wallet
        summary["label"] = label
        return {"status": "ok", "data": summary}
    except Exception as exc:  # noqa: BLE001
        log.exception("Error fetching wallet %s", wallet)
        return {"status": "error", "wallet": wallet, "label": label, "error": str(exc)}


async def fetch_all_wallets() -> list[dict[str, Any]]:
    tasks = [fetch_wallet(w, label) for w, label in FUND_WALLETS.items()]
    return await asyncio.gather(*tasks)


async def get_spot_price(coin: str) -> float | None:
    """Try to derive a spot-ish price for `coin` from metaAndAssetCtxs (uses mark/mid)."""
    try:
        data = await meta_and_asset_ctxs()
        if not isinstance(data, list) or len(data) < 2:
            return None
        meta, ctxs = data[0], data[1]
        universe = meta.get("universe", []) if isinstance(meta, dict) else []
        for idx, asset in enumerate(universe):
            if asset.get("name", "").upper() == coin.upper():
                if idx < len(ctxs):
                    ctx = ctxs[idx] or {}
                    for key in ("markPx", "midPx", "oraclePx"):
                        if key in ctx:
                            try:
                                return float(ctx[key])
                            except (TypeError, ValueError):
                                continue
                return None
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("get_spot_price(%s) failed: %s", coin, exc)
        return None
