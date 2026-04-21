"""HyperLiquid portfolio reader for all fund wallets.

Docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint

Covers main perp dex + HIP-3 builder-deployed dexes (cash, para, flx, vntl, hyna, km, abcd, xyz).
Also fetches spot token balances (kHYPE, PEAR, etc.).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from config import FUND_WALLETS, HIP3_DEXES, HYPERLIQUID_API, WALLET_FETCH_TIMEOUT, DATA_DIR
from utils.http import post_json

log = logging.getLogger(__name__)

INFO_URL = f"{HYPERLIQUID_API}/info"

# Module-level cache: {wallet_address: {cached_data}}
_wallet_cache: dict[str, dict] = {}
_WALLET_CACHE_FILE = os.path.join(DATA_DIR, "wallet_cache.json")


def _load_wallet_cache() -> None:
    """Load wallet cache from disk on startup."""
    global _wallet_cache
    if os.path.isfile(_WALLET_CACHE_FILE):
        try:
            with open(_WALLET_CACHE_FILE) as f:
                _wallet_cache = json.load(f)
            log.info("Loaded wallet cache for %d wallets", len(_wallet_cache))
        except Exception as e:
            log.warning("Could not load wallet cache: %s", e)
            _wallet_cache = {}


def _save_wallet_cache() -> None:
    """Persist wallet cache to disk."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(_WALLET_CACHE_FILE, "w") as f:
            json.dump(_wallet_cache, f, indent=2, default=str)
    except Exception as e:
        log.warning("Could not save wallet cache: %s", e)


# Load cache on module import
_load_wallet_cache()


async def _info(payload: dict[str, Any]) -> Any:
    return await post_json(INFO_URL, payload, timeout=WALLET_FETCH_TIMEOUT)


async def clearinghouse_state(wallet: str, dex: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": "clearinghouseState", "user": wallet}
    if dex:
        payload["dex"] = dex
    return await _info(payload)


async def spot_state(wallet: str) -> dict[str, Any]:
    return await _info({"type": "spotClearinghouseState", "user": wallet})


async def meta_and_asset_ctxs() -> list[Any]:
    return await _info({"type": "metaAndAssetCtxs"})


async def user_fills(wallet: str) -> list[dict[str, Any]]:
    return await _info({"type": "userFills", "user": wallet})


def _summarize_positions(state: dict[str, Any], dex_label: str = "main") -> dict[str, Any]:
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
            "dex": dex_label,
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


async def _fetch_dex(wallet: str, dex: str | None) -> dict[str, Any]:
    """Fetch one dex for a wallet. Returns summary dict, or empty if error."""
    dex_label = dex if dex else "main"
    try:
        state = await clearinghouse_state(wallet, dex=dex)
        return _summarize_positions(state, dex_label=dex_label)
    except Exception as exc:  # noqa: BLE001
        log.warning("Fetch dex %s for %s failed: %s", dex_label, wallet, exc)
        return {
            "account_value": 0.0,
            "total_ntl_pos": 0.0,
            "total_margin_used": 0.0,
            "cross_account_value": 0.0,
            "withdrawable": 0.0,
            "positions": [],
            "unrealized_pnl_total": 0.0,
        }


async def _fetch_spot(wallet: str) -> list[dict[str, Any]]:
    """Fetch spot token balances (kHYPE, PEAR, HYPE, etc.) for a wallet."""
    try:
        state = await spot_state(wallet)
        balances = state.get("balances", []) or []
        result: list[dict[str, Any]] = []
        for b in balances:
            try:
                total = float(b.get("total", 0) or 0)
            except (TypeError, ValueError):
                total = 0.0
            if total <= 0:
                continue
            try:
                hold = float(b.get("hold", 0) or 0)
            except (TypeError, ValueError):
                hold = 0.0
            try:
                entry_ntl = float(b.get("entryNtl", 0) or 0)
            except (TypeError, ValueError):
                entry_ntl = 0.0
            result.append({
                "coin": b.get("coin", "?"),
                "total": total,
                "hold": hold,
                "entry_ntl": entry_ntl,
            })
        return result
    except Exception as exc:  # noqa: BLE001
        log.warning("Fetch spot for %s failed: %s", wallet, exc)
        return []


async def fetch_wallet(wallet: str, label: str) -> dict[str, Any]:
    """Fetch one wallet across main + HIP-3 dexes + spot with retry logic.

    Returns {status, data|error, stale_from_cache}.
    Implements retry with exponential backoff (1s, 2s, 4s).
    Falls back to cached value on failure.
    """
    max_retries = 3
    retry_delays = [1, 2, 4]  # exponential backoff in seconds

    for attempt in range(max_retries):
        try:
            dex_keys: list[str | None] = [None] + list(HIP3_DEXES)
            # Fetch all perp dexes + spot concurrently
            dex_tasks = [_fetch_dex(wallet, d) for d in dex_keys]
            spot_task = _fetch_spot(wallet)
            all_results = await asyncio.gather(*dex_tasks, spot_task)
            spot_balances = all_results[-1]  # last result is spot
            dex_results = all_results[:-1]   # everything else is perp dexes

            positions: list[dict[str, Any]] = []
            account_value = 0.0
            total_ntl_pos = 0.0
            total_margin_used = 0.0
            withdrawable = 0.0
            unrealized_total = 0.0
            for r in dex_results:
                positions.extend(r.get("positions") or [])
                account_value += r.get("account_value", 0.0)
                total_ntl_pos += r.get("total_ntl_pos", 0.0)
                total_margin_used += r.get("total_margin_used", 0.0)
                withdrawable += r.get("withdrawable", 0.0)
                unrealized_total += r.get("unrealized_pnl_total", 0.0)

            summary = {
                "wallet": wallet,
                "label": label,
                "account_value": account_value,
                "total_ntl_pos": total_ntl_pos,
                "total_margin_used": total_margin_used,
                "cross_account_value": account_value,
                "withdrawable": withdrawable,
                "positions": positions,
                "unrealized_pnl_total": unrealized_total,
                "spot_balances": spot_balances,
            }

            # Cache successful fetch
            _wallet_cache[wallet] = summary
            _save_wallet_cache()

            return {"status": "ok", "data": summary}

        except Exception as exc:
            log.warning("Attempt %d/%d for wallet %s failed: %s", attempt + 1, max_retries, wallet, exc)
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delays[attempt])

    # All retries exhausted — fall back to cache if available
    if wallet in _wallet_cache:
        cached = _wallet_cache[wallet]
        log.warning("Using stale cache for wallet %s (fetch failed after %d retries)", wallet, max_retries)
        return {
            "status": "ok",
            "data": cached,
            "stale": True,
            "stale_reason": "fetch_failed_after_retries"
        }

    # No cache and fetch failed
    log.error("Could not fetch wallet %s and no cache available", wallet)
    return {
        "status": "error",
        "wallet": wallet,
        "label": label,
        "error": f"Fetch failed after {max_retries} retries and no cache available"
    }


async def fetch_all_wallets() -> list[dict[str, Any]]:
    if not FUND_WALLETS:
        log.warning("fetch_all_wallets: FUND_WALLETS is empty (no FUND_WALLET_N env vars set)")
        return []
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

# ─── Recent fills (closed trades) ─────────────────────────────────────────────
async def fetch_recent_fills(wallet: str, hours: int = 24) -> list[dict[str, Any]]:
    """Fetch recent fills (closed trades) for a wallet within the last N hours."""
    try:
        fills = await user_fills(wallet)
        if not isinstance(fills, list):
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        recent: list[dict[str, Any]] = []
        for f in fills:
            try:
                ts = f.get("time")
                if ts:
                    dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                    if dt < cutoff:
                        continue
                recent.append({
                    "coin": f.get("coin", "?"),
                    "side": f.get("side", "?"),
                    "px": float(f.get("px", 0) or 0),
                    "sz": float(f.get("sz", 0) or 0),
                    "time": ts,
                    "closedPnl": float(f.get("closedPnl", 0) or 0),
                    "fee": float(f.get("fee", 0) or 0),
                    "dir": f.get("dir", ""),
                })
            except Exception:  # noqa: BLE001
                continue
        return recent
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch_recent_fills for %s failed: %s", wallet, exc)
        return []


async def fetch_all_recent_fills(hours: int = 24) -> list[dict[str, Any]]:
    """Fetch recent fills for all fund wallets."""
    if not FUND_WALLETS:
        return []
    results: list[dict[str, Any]] = []
    for wallet, label in FUND_WALLETS.items():
        fills = await fetch_recent_fills(wallet, hours)
        for f in fills:
            f["_wallet_label"] = label
        results.extend(fills)
    results.sort(key=lambda x: x.get("time", 0), reverse=True)
    return results
