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
from modules import spot_index

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


async def frontend_open_orders(wallet: str, dex: str | None = None) -> list[dict[str, Any]]:
    """Open orders for a wallet (HL ``frontendOpenOrders`` — SAME info endpoint).

    R-REPORTE-LIVE (2026-06-03): this is NOT a new data source — it is one
    more query type on the Hyperliquid info endpoint already powering the
    whole portfolio read. It is needed by the position classifier to detect
    SL/TP triggers and laddered DCA limit orders. NEVER raises: returns an
    empty list on any failure so a missing/blocked order read can never break
    /reporte (the classifier degrades to attribute-only when orders are empty).

    R-SLTP-NATIVE-DETECT (2026-06-09): ``dex`` param added. CONFIRMED LIVE:
    frontendOpenOrders WITHOUT ``dex`` returns ONLY main-dex orders (BTC/SOL),
    NOT the HIP-3 builder-dex ones — the native SL/TP triggers on the xyz:
    basket legs (xyz:HOOD etc) only surface with ``{"dex": "xyz"}``. That
    silent omission was the root cause of the false "SIN SL / ACCIÓN URGENTE"
    on every xyz: leg in the 2026-06-09 /reporte.
    """
    try:
        payload: dict[str, Any] = {"type": "frontendOpenOrders", "user": wallet}
        if dex:
            payload["dex"] = dex
        res = await _info(payload)
        return res if isinstance(res, list) else []
    except Exception as exc:  # noqa: BLE001
        log.warning("frontend_open_orders for %s (dex=%s) failed: %s", wallet, dex, exc)
        return []


async def fetch_all_open_orders(wallet: str) -> list[dict[str, Any]]:
    """Open orders across MAIN + every HIP-3 builder dex, merged (R-SLTP-NATIVE-DETECT).

    Queries ``frontendOpenOrders`` once per dex (main = no dex param) — same
    info endpoint, concurrent, NEVER raises. Without this, any SL/TP or DCA
    ladder living on a builder dex (xyz:, abcd:, …) is invisible to the
    position classifier.
    """
    try:
        dex_keys: list[str | None] = [None] + list(HIP3_DEXES)
        results = await asyncio.gather(
            *[frontend_open_orders(wallet, dex=d) for d in dex_keys],
            return_exceptions=True,
        )
        merged: list[dict[str, Any]] = []
        for r in results:
            if isinstance(r, list):
                merged.extend(r)
        return merged
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch_all_open_orders for %s failed: %s", wallet, exc)
        return []


async def user_fills_by_time(
    wallet: str,
    start_time_ms: int,
    end_time_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch fills for a wallet within a time range (HL userFillsByTime endpoint)."""
    payload: dict[str, Any] = {
        "type": "userFillsByTime",
        "user": wallet,
        "startTime": start_time_ms,
    }
    if end_time_ms is not None:
        payload["endTime"] = end_time_ms
    return await _info(payload)


def _normalize_fill(f: dict[str, Any], wallet_label: str = "") -> dict[str, Any]:
    """Normalise a raw HL fill dict to the canonical shape used across the bot."""
    return {
        "coin": spot_index.resolve_spot_coin(f.get("coin", "?")),
        "side": f.get("side", "?"),
        "dir": f.get("dir", ""),
        "px": float(f.get("px", 0) or 0),
        "sz": float(f.get("sz", 0) or 0),
        "time": f.get("time"),
        "closedPnl": float(f.get("closedPnl", 0) or 0),
        "fee": float(f.get("fee", 0) or 0),
        "_wallet_label": wallet_label,
    }


async def fetch_fills_since(
    wallet: str,
    since: datetime,
    label: str = "",
) -> list[dict[str, Any]]:
    """Fetch all fills for one wallet since `since` datetime (inclusive)."""
    start_ms = int(since.timestamp() * 1000)
    try:
        await spot_index.ensure_spot_index_map()
        raw = await user_fills_by_time(wallet, start_ms)
        if not isinstance(raw, list):
            return []
        return [_normalize_fill(f, wallet_label=label) for f in raw]
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch_fills_since(%s, %s) failed: %s", wallet, since.isoformat(), exc)
        return []


async def fetch_all_fills_since(since: datetime) -> list[dict[str, Any]]:
    """Fetch fills for ALL fund wallets since `since` datetime."""
    if not FUND_WALLETS:
        return []
    tasks = [
        fetch_fills_since(wallet, since, label=label)
        for wallet, label in FUND_WALLETS.items()
    ]
    results = await asyncio.gather(*tasks)
    out: list[dict[str, Any]] = []
    for r in results:
        out.extend(r)
    out.sort(key=lambda x: x.get("time") or 0, reverse=True)
    return out


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
        # P1.6: cumulative funding PAID since the position opened
        # (HL cumFunding.sinceOpen, USD; HL convention: positive = paid out,
        # negative = received). Captured here so the funding tracker can show
        # carry-to-date per position without a second API round-trip.
        # R-FUNDING-TRUTH (2026-06-15) / FIX 2 (never fabricate a value):
        # distinguish a GENUINE zero (the cumFunding block is present and
        # sinceOpen really is 0.0) from a MISSING value (a partial/failed HL
        # payload with no cumFunding block at all). A genuine 0.0 stays 0.0; a
        # missing block becomes ``None`` so downstream renders "n/d" instead of a
        # confident "+0.00 USD" carry. HL always includes cumFunding.sinceOpen
        # on a healthy assetPositions payload, so its absence is a real signal.
        _cf = p.get("cumFunding")
        if isinstance(_cf, dict) and _cf.get("sinceOpen") is not None:
            try:
                cum_funding_open = float(_cf.get("sinceOpen"))
            except (TypeError, ValueError):
                cum_funding_open = None
        else:
            cum_funding_open = None
        # R-PM-MARGIN-MODE-FIX (2026-06-07): carry the per-leg margin facts the
        # PM model needs to distinguish CROSS (shared pool) from ISOLATED
        # (walled-off) legs — ``margin_used`` is the posted isolated margin on an
        # isolated leg, ``max_leverage`` lets the cross-pool math derive each
        # cross leg's maintenance contribution. ``leverage_type`` already tags
        # the mode (isolated|cross), read live from HL — never hardcoded.
        try:
            margin_used = float(p.get("marginUsed", 0) or 0)
        except (TypeError, ValueError):
            margin_used = 0.0
        try:
            max_leverage = float(p.get("maxLeverage", 0) or 0)
        except (TypeError, ValueError):
            max_leverage = 0.0
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
            "margin_used": margin_used,
            "max_leverage": max_leverage,
            "cum_funding_since_open": cum_funding_open,
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
        # R-MARGIN-STRESS-HOTFIX (2026-06-10): cross-only margin used, straight
        # from HL ``crossMarginSummary.totalMarginUsed``. The blended
        # ``marginSummary`` double-counts isolated margin in BOTH used and
        # equity (iso-only account → used/equity == 100% by construction), so
        # the margin-stress alert must read THIS field, never the blended one.
        "cross_margin_used": _f(cross.get("totalMarginUsed")),
        # R-PM-LIQ: cross-perp maintenance margin. Only CROSS perps share the
        # PM account's collateral and therefore raise the spot liquidation
        # point; isolated perps are walled off (live fund = isolated → 0.0).
        "cross_maintenance_margin_used": _f(state.get("crossMaintenanceMarginUsed")),
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
            "cross_margin_used": 0.0,
            "cross_maintenance_margin_used": 0.0,
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
            try:
                borrowed = float(b.get("borrowed", 0) or 0)
            except (TypeError, ValueError):
                borrowed = 0.0
            # R-WALLET-FIX (2026-06-06): a HyperLiquid Portfolio Margin borrow
            # shows as a NEGATIVE spot total (USDC total=-10,740) WITH a
            # ``borrowed`` field (=39,808 — the true gross liability). The
            # legacy ``total <= 0: continue`` DROPPED that row entirely, so the
            # fund's USDC debt vanished from accounting → false "deuda $0 /
            # ratio 0% CALM" and a ~$40K equity overstatement. Keep any row
            # that is non-zero OR carries a borrow; only skip truly empty dust.
            if total == 0 and borrowed <= 0:
                continue
            try:
                hold = float(b.get("hold", 0) or 0)
            except (TypeError, ValueError):
                hold = 0.0
            try:
                entry_ntl = float(b.get("entryNtl", 0) or 0)
            except (TypeError, ValueError):
                entry_ntl = 0.0
            try:
                supplied = float(b.get("supplied", 0) or 0)
            except (TypeError, ValueError):
                supplied = 0.0
            try:
                ltv = float(b.get("ltv", 0) or 0)
            except (TypeError, ValueError):
                ltv = 0.0
            result.append({
                "coin": spot_index.resolve_spot_coin(b.get("coin", "?")),
                "total": total,
                "hold": hold,
                "entry_ntl": entry_ntl,
                # R-WALLET-FIX: preserve PM lending fields so downstream
                # accounting can net the real borrowed liability.
                "borrowed": borrowed,
                "supplied": supplied,
                "ltv": ltv,
            })
        return result
    except Exception as exc:  # noqa: BLE001
        log.warning("Fetch spot for %s failed: %s", wallet, exc)
        return []


async def _empty_orders() -> list[dict[str, Any]]:
    """Awaitable that yields no orders (used when order fetch is disabled)."""
    return []


def _to_float(v: Any) -> float | None:
    """Parse HL numeric strings ("63500.0", "0.0") to float; None if invalid.

    HL serialises ALL numbers as strings, so truthiness checks like
    ``bool(o.get("triggerPx"))`` are unsafe (``bool("0.0") is True``). Always
    compare the parsed numeric value instead.
    """
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _normalize_open_orders(orders: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Normalise HL ``frontendOpenOrders`` into the shape the classifier reads.

    HL order side ``"B"`` = bid/buy, ``"A"`` = ask/sell. Trigger orders carry
    ``isTrigger=True`` + ``triggerPx`` + ``orderType`` ("Stop Market" / "Take
    Profit Market" / etc.); resting limit orders are ``isTrigger=False``.
    ``reduceOnly`` / ``isPositionTpsl`` mark protective (SL/TP) orders. NEVER
    raises — a malformed order is skipped, not fatal.
    """
    out: list[dict[str, Any]] = []
    for o in orders or []:
        if not isinstance(o, dict):
            continue
        try:
            side_raw = (o.get("side") or "").upper()
            tpsl = (o.get("tpsl") or "").lower()  # "tp" | "sl" | ""
            reduce_only = bool(o.get("reduceOnly") or o.get("isPositionTpsl"))
            is_position_tpsl = bool(o.get("isPositionTpsl"))
            order_type = (o.get("orderType") or "").lower()
            # ── Trigger detection ──
            # CRITICAL: HL returns triggerPx as the STRING "0.0" for plain
            # limit orders, and bool("0.0") is True — so the old
            # ``bool(o.get("triggerPx"))`` flagged every resting limit as a
            # trigger. Parse it numerically: a real trigger has triggerPx > 0
            # (or the explicit isTrigger flag, a populated triggerCondition,
            # or a stop/take-profit order_type name).
            trigger_px_val = _to_float(o.get("triggerPx")) or 0.0
            trig_cond = (o.get("triggerCondition") or "").strip().lower()
            has_trig_cond = trig_cond not in ("", "n/a", "na", "none")
            order_type_is_trigger = ("stop" in order_type) or ("take profit" in order_type)
            is_trigger = (
                bool(o.get("isTrigger"))
                or trigger_px_val > 0
                or has_trig_cond
                or order_type_is_trigger
            )
            # ── SL/TP detection ──
            # A position carries SL/TP ONLY via reduce-only TRIGGER orders
            # (HL native position TP/SL: isPositionTpsl=True, also a
            # reduce-only trigger). A plain limit DCA buy is NOT SL/TP, even
            # if same-coin. Require trigger-ness AND reduce-only.
            is_sl_tp = is_position_tpsl or (is_trigger and reduce_only) or (tpsl in ("tp", "sl") and reduce_only)
            out.append({
                "coin": spot_index.resolve_spot_coin(o.get("coin", "?")),
                "side": "BUY" if side_raw == "B" else ("SELL" if side_raw == "A" else side_raw),
                "limit_px": _to_float(o.get("limitPx")) or 0.0,
                "trigger_px": trigger_px_val if trigger_px_val > 0 else None,
                "size": _to_float(o.get("sz")) or 0.0,
                "is_trigger": is_trigger,
                "reduce_only": reduce_only,
                "tpsl": tpsl,
                "order_type": o.get("orderType", ""),
                "is_sl_tp": is_sl_tp,
            })
        except Exception:  # noqa: BLE001
            continue
    return out


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
            # Warm the spot-index → ticker map so @N spot orders/balances
            # resolve by name (HYPE, not @107). Cached 1h, so this is a
            # no-op fast path after the first wallet of the run.
            await spot_index.ensure_spot_index_map()
            dex_keys: list[str | None] = [None] + list(HIP3_DEXES)
            # Fetch all perp dexes + spot + open orders concurrently.
            # R-REPORTE-LIVE: open orders come from the SAME HL info endpoint
            # (frontendOpenOrders) — used by the position classifier to tell
            # CYCLE-ACCUMULATION (laddered limits, no SL/TP) from TACTICAL.
            dex_tasks = [_fetch_dex(wallet, d) for d in dex_keys]
            spot_task = _fetch_spot(wallet)
            orders_enabled = os.getenv("OPEN_ORDERS_FETCH_ENABLED", "true").lower() == "true"
            # R-SLTP-NATIVE-DETECT (2026-06-09): fetch orders across MAIN +
            # every HIP-3 dex — frontendOpenOrders without ``dex`` omits the
            # builder-dex SL/TP triggers (xyz: legs), which caused the false
            # "SIN SL" classification on the whole basket.
            orders_task = fetch_all_open_orders(wallet) if orders_enabled else _empty_orders()
            all_results = await asyncio.gather(*dex_tasks, spot_task, orders_task)
            open_orders = all_results[-1] if isinstance(all_results[-1], list) else []
            spot_balances = all_results[-2]  # second-to-last result is spot
            dex_results = all_results[:-2]   # everything else is perp dexes

            positions: list[dict[str, Any]] = []
            account_value = 0.0
            total_ntl_pos = 0.0
            total_margin_used = 0.0
            withdrawable = 0.0
            unrealized_total = 0.0
            cross_maint_margin = 0.0
            cross_account_value = 0.0
            cross_margin_used = 0.0
            for r in dex_results:
                positions.extend(r.get("positions") or [])
                account_value += r.get("account_value", 0.0)
                total_ntl_pos += r.get("total_ntl_pos", 0.0)
                total_margin_used += r.get("total_margin_used", 0.0)
                withdrawable += r.get("withdrawable", 0.0)
                unrealized_total += r.get("unrealized_pnl_total", 0.0)
                cross_maint_margin += r.get("cross_maintenance_margin_used", 0.0)
                cross_account_value += r.get("cross_account_value", 0.0)
                cross_margin_used += r.get("cross_margin_used", 0.0)

            summary = {
                "wallet": wallet,
                "label": label,
                "account_value": account_value,
                "total_ntl_pos": total_ntl_pos,
                "total_margin_used": total_margin_used,
                # R-MARGIN-STRESS-HOTFIX: REAL cross equity (sum of per-dex
                # crossMarginSummary.accountValue) — the old value here was the
                # blended account_value, which made cross metrics meaningless.
                "cross_account_value": cross_account_value,
                "cross_margin_used": cross_margin_used,
                # R-PM-LIQ: aggregated cross-perp maintenance margin (0.0 for the
                # live fund — its perps are isolated). Consumed by compute_pm_state
                # to fold cross perp risk into the spot liquidation point.
                "cross_maintenance_margin_used": cross_maint_margin,
                "withdrawable": withdrawable,
                "positions": positions,
                "unrealized_pnl_total": unrealized_total,
                "spot_balances": spot_balances,
                # R-REPORTE-LIVE: normalised open orders (for the classifier).
                "open_orders": _normalize_open_orders(open_orders),
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
        await spot_index.ensure_spot_index_map()
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
                    "coin": spot_index.resolve_spot_coin(f.get("coin", "?")),
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
