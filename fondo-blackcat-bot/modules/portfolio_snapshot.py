"""Shared portfolio aggregator — single source of truth for the dashboard.

Builds one structured snapshot using the SAME formulas as
``templates/formatters.format_quick_positions`` (which is what /reporte ends
up showing under the hood):

    capital_total_per_wallet = perp_account_value + spot_non_usdc_usd + hl_collateral_usd

(Debt is reported as a separate line — it is **not** subtracted from capital,
matching the way /reporte displays the consolidated portfolio.)

================================================================
CRITICAL: HYPERLIQUID UNIFIED ACCOUNT (do NOT remove this note)
================================================================
HyperLiquid unified SPOT and PERPETUALS into a single account. The USDC
that backs an open basket appears in BOTH endpoints of the info API:

  * ``clearinghouseState.marginSummary.accountValue``  (perp equity / margin)
  * ``spotClearinghouseState.balances[USDC].total``    (spot USDC balance)

NEVER sum both — it is the SAME USDC reported under two views.

The authoritative source for a wallet's total capital is
``clearinghouseState.marginSummary.accountValue``. From the spot endpoint we
ONLY add NON-USDC tokens (HYPE, kHYPE, PEAR, USDH, USDT0, etc.) valued at
current market price.

This bug (2026-04-28) caused wallet 0xc7ae to be reported as $11.6K
when its real capital was $5.8K — a 2x double-count of basket margin.
================================================================

Identifies, dynamically:

* ``main_flywheel``  — HyperLend wallet with the HIGHEST collateral among
  those carrying debt (the wallet driving the pair trade). Hardcoding the
  address would break the moment BCD rotates the position to a different
  wallet, so we pick by capital weight.
* ``basket_positions`` — ALL SHORT perp positions across all fund wallets
  with notional > ``$50``. Token-list-agnostic: captures the live basket
  regardless of whether symbols are listed in
  ``fund_state.BASKET_PERP_TOKENS``. The static list is kept for /status
  classification but is the wrong primary key for live basket detection.

Market prices are read from the canonical ``market.fetch_market_data`` shape:

    market.data.prices  ==>  { "BTC": {"price_usd": ..., ...}, "ETH": {...}, "HYPE": {...} }

(The buggy older code looked up ``prices["bitcoin"]["usd"]`` — that key path
never existed in this codebase.)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# Module-level cache so /reporte and /dashboard share work and we don't
# hammer the HyperEVM RPC with 200+ parallel calls on every dashboard hit.
# Browser refresh cadence vs RPC rate-limits (-32005) was the root cause of
# HL collateral showing $0 when the dashboard was loaded right after a
# /reporte run.
#
# HOTFIX 2 (2026-04-27): added stale-while-revalidate.
# Previous implementation: when cache TTL expired and refresh failed, the
# next call would do another blocking fetch and on RPC rate-limit the
# dashboard rendered an empty placeholder ($0 capital, "loading prices..."
# market block, missing flywheel cards). UX: dashboard parpadeaba entre
# full y empty cada 45-60s.
# New behavior: cache NEVER returns empty. After successful first fetch,
# every subsequent caller receives the last-good snapshot. If the data is
# older than the FRESH TTL, the snapshot is flagged stale and a
# best-effort refresh is attempted with a short timeout. If it fails, the
# last-good snapshot is still returned and the dashboard shows a "stale
# Ns" badge instead of going blank.
_SNAPSHOT_CACHE: dict[str, Any] = {"snap": None, "ts": 0.0}
_SNAPSHOT_LOCK: asyncio.Lock | None = None
# Background revalidation task. The stale branch fires-and-forgets a
# refresh and returns last-good immediately so user requests never block.
_BG_REFRESH_TASK: asyncio.Task | None = None

SNAPSHOT_TTL_SEC = float(
    os.getenv("DASHBOARD_CACHE_FRESH_TTL_SECONDS", "45")
)  # fresh enough for dashboard refresh + /reporte cadence
STALE_MAX_AGE_SEC = float(
    os.getenv("DASHBOARD_CACHE_STALE_MAX_AGE_SECONDS", "600")
)  # after 10 min, flag with stronger warning
STALE_REFRESH_TIMEOUT_SEC = float(
    os.getenv("DASHBOARD_STALE_REFRESH_TIMEOUT_SECONDS", "8")
)  # max wait when revalidating from a stale-but-present cache
COLD_START_TIMEOUT_SEC = float(
    os.getenv("DASHBOARD_COLD_START_TIMEOUT_SECONDS", "25")
)  # cold start can take longer (200+ RPC calls)


# ─── Datatypes ──────────────────────────────────────────────────────────────


@dataclass
class WalletSnapshot:
    address: str
    label: str
    short: str
    perp_equity: float = 0.0
    spot_usd: float = 0.0
    hl_collateral_usd: float = 0.0
    hl_debt_usd: float = 0.0
    capital_total: float = 0.0
    upnl_perp: float = 0.0
    health_factor: float | None = None
    collateral_symbol: str | None = None
    collateral_balance: float = 0.0
    debt_symbol: str | None = None
    debt_balance: float = 0.0
    short_positions: list[dict[str, Any]] = field(default_factory=list)
    raw_positions: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class MarketBlock:
    btc: float | None = None
    eth: float | None = None
    hype: float | None = None
    fear_greed_value: int | None = None
    fear_greed_label: str | None = None


@dataclass
class PortfolioSnapshot:
    wallets: list[WalletSnapshot]
    capital_total: float
    hl_collateral_total: float
    hl_debt_total: float
    perp_equity_total: float
    spot_usd_total: float
    upnl_perp_total: float
    main_flywheel: WalletSnapshot | None
    secondary_flywheel: WalletSnapshot | None
    basket_positions: list[dict[str, Any]]
    basket_upnl: float
    basket_notional: float
    market: MarketBlock
    # HOTFIX 2: staleness metadata for stale-while-revalidate UX badge.
    # ``built_at_ts`` is the epoch-second when the underlying fetch started.
    # ``is_fresh`` is True the moment a successful fetch lands; the
    # dashboard render layer flips it to False (with an "age" computation)
    # if the cached snapshot is older than ``SNAPSHOT_TTL_SEC``.
    built_at_ts: float = 0.0
    is_fresh: bool = True
    fetch_attempts: int = 1
    last_error: str | None = None


# ─── Aggregator ─────────────────────────────────────────────────────────────


async def _safe(coro, label: str):
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001
        log.warning("portfolio_snapshot: %s failed: %s", label, exc)
        return None


def _short(addr: str) -> str:
    if not addr or len(addr) < 10:
        return addr or "?"
    return addr[:6] + "…" + addr[-4:]


def _spot_usd_value(spot_balances: list[dict[str, Any]],
                    prices: dict[str, Any]) -> float:
    """Sum spot USD value EXCLUDING USDC.

    Why USDC is excluded: under HyperLiquid Unified Account, USDC sitting in
    spot is the SAME asset already reported by ``clearinghouseState.marginSummary
    .accountValue`` (which we use as ``perp_equity``). Adding it again here
    double-counts the margin behind any open perp/basket position.

    Other stablecoins (USDH, USDT, USDT0, DAI) are real on-chain spot tokens
    and are *not* the unified-account collateral, so they are summed 1:1.

    Non-stable coins use live price from ``market.data.prices`` with
    entry-notional cost basis as last-resort proxy.
    """
    total = 0.0
    for sb in spot_balances or []:
        coin = (sb.get("coin") or "").upper()
        amount = float(sb.get("total") or 0)
        entry_ntl = float(sb.get("entry_ntl") or 0)
        # CRITICAL: skip USDC — already in perp accountValue (Unified Account).
        if coin == "USDC":
            continue
        if coin in {"USDH", "USDT", "USDT0", "DAI"}:
            total += amount
            continue
        # kHYPE → HYPE proxy
        lookup = coin.removeprefix("K") if coin.startswith("K") else coin
        entry = (prices.get(lookup) or prices.get(coin) or {}) if prices else {}
        px = entry.get("price_usd") if isinstance(entry, dict) else None
        if px and amount:
            total += amount * float(px)
        else:
            total += entry_ntl
    return total


def _is_regression(new_snap: PortfolioSnapshot,
                    prev_snap: PortfolioSnapshot | None) -> str | None:
    """Detect a partial-fetch regression that would poison the cache.

    Heuristic: if the previous snapshot had a non-trivial HL position
    AND the new one shows < 5% of that, the HL fetch silently failed
    (``fetch_all_hyperlend`` returning None / empty list inside the
    ``_safe`` wrapper). Same for total capital. Returns the reason
    string if a regression is detected, else None.

    This prevents the screenshot-2 bug: after a successful refresh, the
    dashboard would briefly show $11K capital (perp+spot only) instead
    of $85K (with HL collateral), then flip back on the next refresh.
    """
    if prev_snap is None:
        return None
    # HL collateral disappeared
    if prev_snap.hl_collateral_total > 1000 and new_snap.hl_collateral_total < (
        prev_snap.hl_collateral_total * 0.05
    ):
        return (
            f"HL collateral collapsed "
            f"(${prev_snap.hl_collateral_total:,.0f} → ${new_snap.hl_collateral_total:,.0f})"
        )
    # Total capital halved (perp loss tolerated, but >50% drop in 30s is fetch failure)
    if prev_snap.capital_total > 5000 and new_snap.capital_total < (
        prev_snap.capital_total * 0.5
    ):
        return (
            f"Capital halved "
            f"(${prev_snap.capital_total:,.0f} → ${new_snap.capital_total:,.0f})"
        )
    # Wallet count dropped to 0/1 from 3+
    if len(prev_snap.wallets) >= 3 and len(new_snap.wallets) < 2:
        return (
            f"Wallet count collapsed "
            f"({len(prev_snap.wallets)} → {len(new_snap.wallets)})"
        )
    return None


async def _do_blocking_fetch(timeout_sec: float, cached_fallback: PortfolioSnapshot | None
                              ) -> PortfolioSnapshot:
    """Blocking fetch path. Used by force_refresh=True and cold-start.

    On success: populate cache, return fresh snapshot.
    On regression: REJECT the new snapshot and keep the cached one
    (tagged stale). Prevents partial-fetch poisoning.
    On failure: return cached_fallback (tagged stale) if provided, else
    a loading placeholder. The cache is left untouched on failure so the
    next request still gets the last-good snapshot.
    """
    global _SNAPSHOT_LOCK
    if _SNAPSHOT_LOCK is None:
        _SNAPSHOT_LOCK = asyncio.Lock()
    try:
        async with asyncio.timeout(timeout_sec):
            async with _SNAPSHOT_LOCK:
                # Re-check inside lock: a concurrent caller may have just refreshed.
                cached_inside: PortfolioSnapshot | None = _SNAPSHOT_CACHE.get("snap")
                cached_inside_ts: float = _SNAPSHOT_CACHE.get("ts") or 0.0
                if (
                    cached_inside is not None
                    and (time.time() - cached_inside_ts) < SNAPSHOT_TTL_SEC
                ):
                    cached_inside.is_fresh = True
                    return cached_inside
                snap = await _build_portfolio_snapshot_inner()

                # Regression guard: if the new snapshot looks broken vs.
                # the previous good one, reject it and keep the cache.
                regression = _is_regression(snap, cached_inside)
                if regression and cached_inside is not None:
                    log.warning(
                        "Rejecting refreshed snapshot — %s. Keeping previous good cache.",
                        regression,
                    )
                    cached_inside.is_fresh = False
                    cached_inside.fetch_attempts = (cached_inside.fetch_attempts or 1) + 1
                    cached_inside.last_error = f"regression: {regression}"
                    return cached_inside

                snap.built_at_ts = time.time()
                snap.is_fresh = True
                snap.fetch_attempts = 1
                snap.last_error = None
                _SNAPSHOT_CACHE["snap"] = snap
                _SNAPSHOT_CACHE["ts"] = snap.built_at_ts
                return snap
    except Exception as exc:  # noqa: BLE001
        err_msg = type(exc).__name__ + (f": {exc}" if str(exc) else "")
        if cached_fallback is not None:
            stale_age = time.time() - (_SNAPSHOT_CACHE.get("ts") or 0.0)
            log.warning(
                "Snapshot refresh failed (timeout=%.0fs, age=%.0fs, err=%s) — last-good stays in cache.",
                timeout_sec, stale_age, err_msg,
            )
            cached_fallback.is_fresh = False
            cached_fallback.fetch_attempts = (cached_fallback.fetch_attempts or 1) + 1
            cached_fallback.last_error = err_msg[:200]
            return cached_fallback
        log.error("Cold-start snapshot fetch failed: %s", err_msg)
        return _make_loading_placeholder(error=err_msg[:200])


async def _background_revalidate() -> None:
    """Fire-and-forget refresh kicked off by a stale cache hit. The
    blocking timeout is the cold-start one (longer) because we're
    decoupled from the user request — taking 15-25s here is fine, the
    user already got their stale page back."""
    try:
        await _do_blocking_fetch(COLD_START_TIMEOUT_SEC, cached_fallback=None)
    except Exception as exc:  # noqa: BLE001
        log.warning("Background revalidate failed (suppressed): %s", exc)


def _kick_background_refresh() -> None:
    """Schedule a background revalidation if not already running."""
    global _BG_REFRESH_TASK
    if _BG_REFRESH_TASK is not None and not _BG_REFRESH_TASK.done():
        return  # one in-flight refresh is plenty
    try:
        loop = asyncio.get_event_loop()
        _BG_REFRESH_TASK = loop.create_task(_background_revalidate())
    except RuntimeError:
        # No running loop (shouldn't happen inside aiohttp/PTB), bail silently
        log.debug("No running event loop — skipping background revalidate")


async def build_portfolio_snapshot(force_refresh: bool = False) -> PortfolioSnapshot:
    """Stale-while-revalidate snapshot getter.

    Three branches:

    1. **Cache fresh** (age < ``SNAPSHOT_TTL_SEC``): return cache as-is,
       no fetch.
    2. **Cache stale-but-present** (age >= TTL, snapshot exists): kick off
       a *background* revalidation task and return the last-good snapshot
       *immediately* tagged ``is_fresh=False``. User requests never block
       on a refresh — they always get sub-second response.
    3. **Cache empty (cold start)**: blocking fetch with the longer
       cold-start timeout. On failure, returns a loading placeholder.

    ``force_refresh=True`` does a *blocking* refresh with cold-start
    timeout, falling back to the cached snapshot on failure (never
    returns empty when a cache exists). This is what the proactive
    scheduler job uses, so the cache stays warm even when individual
    refreshes take 15-20s under RPC pressure.
    """
    global _SNAPSHOT_LOCK
    if _SNAPSHOT_LOCK is None:
        _SNAPSHOT_LOCK = asyncio.Lock()

    now = time.time()
    cached: PortfolioSnapshot | None = _SNAPSHOT_CACHE.get("snap")
    cached_ts: float = _SNAPSHOT_CACHE.get("ts") or 0.0
    age = now - cached_ts if cached_ts else float("inf")

    # force_refresh path: blocking fetch (used by the proactive scheduler).
    if force_refresh:
        return await _do_blocking_fetch(COLD_START_TIMEOUT_SEC, cached_fallback=cached)

    # Branch 1: cache fresh — fast path, no fetch.
    if cached is not None and age < SNAPSHOT_TTL_SEC:
        cached.is_fresh = True
        return cached

    # Branch 2: cache stale-but-present — background revalidate, return
    # last-good immediately. Zero blocking on user requests.
    if cached is not None:
        _kick_background_refresh()
        cached.is_fresh = False
        if age > STALE_MAX_AGE_SEC:
            log.error(
                "Snapshot stale > %.0fs (age=%.0fs) — proactive refresh stuck under RPC pressure.",
                STALE_MAX_AGE_SEC, age,
            )
        return cached

    # Branch 3: cache empty — cold start. Must block.
    return await _do_blocking_fetch(COLD_START_TIMEOUT_SEC, cached_fallback=None)


def _make_loading_placeholder(error: str = "") -> PortfolioSnapshot:
    """Empty snapshot returned only when the cache is empty AND the cold
    fetch failed. Dashboard renderer detects this (no wallets + no
    flywheel) and shows a "loading…" screen with auto-refresh."""
    return PortfolioSnapshot(
        wallets=[],
        capital_total=0.0,
        hl_collateral_total=0.0,
        hl_debt_total=0.0,
        perp_equity_total=0.0,
        spot_usd_total=0.0,
        upnl_perp_total=0.0,
        main_flywheel=None,
        secondary_flywheel=None,
        basket_positions=[],
        basket_upnl=0.0,
        basket_notional=0.0,
        market=MarketBlock(),
        built_at_ts=time.time(),
        is_fresh=False,
        fetch_attempts=1,
        last_error=error or "cold-start failed",
    )


def snapshot_age_seconds() -> float | None:
    """Return age (s) of the current cached snapshot, or None if empty."""
    cached_ts: float = _SNAPSHOT_CACHE.get("ts") or 0.0
    if not cached_ts:
        return None
    return time.time() - cached_ts


async def proactive_refresh() -> bool:
    """Background-job entry point. Refreshes the cache with no propagation
    of exceptions — failure is logged and the existing cache is preserved.
    Called by the scheduler every ``DASHBOARD_PROACTIVE_REFRESH_INTERVAL``
    seconds so dashboard hits always find a warm cache."""
    try:
        await build_portfolio_snapshot(force_refresh=True)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Proactive snapshot refresh failed: %s", exc)
        return False


async def _build_portfolio_snapshot_inner() -> PortfolioSnapshot:
    """Inner uncached path — does the actual fetch + aggregate work."""
    from modules.hyperlend import fetch_all_hyperlend
    from modules.market import fetch_market_data
    from modules.portfolio import fetch_all_wallets

    hl, market, wallets = await asyncio.gather(
        _safe(fetch_all_hyperlend(), "hyperlend"),
        _safe(fetch_market_data(), "market"),
        _safe(fetch_all_wallets(), "wallets"),
    )

    # ─── Market block ──────────────────────────────────────────────────────
    prices: dict[str, Any] = {}
    fg_value: int | None = None
    fg_label: str | None = None
    if isinstance(market, dict) and market.get("status") == "ok":
        data = market.get("data") or {}
        prices = data.get("prices") or {}
        fg = data.get("fear_greed") or {}
        try:
            fg_value = int(fg.get("value")) if fg.get("value") is not None else None
        except (TypeError, ValueError):
            fg_value = None
        fg_label = fg.get("classification") or fg.get("label")

    def _px(sym: str) -> float | None:
        entry = prices.get(sym) or prices.get(sym.upper()) or {}
        if isinstance(entry, dict):
            v = entry.get("price_usd") or entry.get("usd")
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None
        return None

    market_block = MarketBlock(
        btc=_px("BTC"),
        eth=_px("ETH"),
        hype=_px("HYPE"),
        fear_greed_value=fg_value,
        fear_greed_label=fg_label,
    )

    # ─── HyperLend by wallet ──────────────────────────────────────────────
    # Lower-cased addr → full HL data row (data dict from fetch_all_hyperlend)
    hl_by_wallet: dict[str, dict[str, Any]] = {}
    if isinstance(hl, list):
        for r in hl:
            if r.get("status") != "ok":
                continue
            d = r.get("data") or {}
            addr = (d.get("wallet") or "").lower()
            if addr:
                hl_by_wallet[addr] = d

    # ─── Per-wallet snapshot ──────────────────────────────────────────────
    wallet_snaps: list[WalletSnapshot] = []
    if isinstance(wallets, list):
        for w in wallets:
            if w.get("status") != "ok":
                continue
            d = w.get("data") or {}
            addr = (d.get("wallet") or "").lower()
            label = d.get("label") or w.get("label") or "?"
            perp_equity = float(d.get("account_value") or 0.0)
            spot_usd = _spot_usd_value(d.get("spot_balances") or [], prices)
            hl_data = hl_by_wallet.get(addr, {})
            hl_coll = float(hl_data.get("total_collateral_usd") or 0.0)
            hl_debt = float(hl_data.get("total_debt_usd") or 0.0)
            cap = perp_equity + spot_usd + hl_coll
            upnl = float(d.get("unrealized_pnl_total") or 0.0)

            short_positions: list[dict[str, Any]] = []
            for pos in d.get("positions") or []:
                try:
                    sz = float(pos.get("size") or 0.0)
                except (TypeError, ValueError):
                    sz = 0.0
                try:
                    ntl = float(pos.get("notional_usd") or 0.0)
                except (TypeError, ValueError):
                    ntl = 0.0
                if sz < 0 and abs(ntl) > 50:
                    try:
                        upnl_pos = float(pos.get("unrealized_pnl") or 0.0)
                    except (TypeError, ValueError):
                        upnl_pos = 0.0
                    short_positions.append({
                        "coin": (pos.get("coin") or "?").upper(),
                        "size": sz,
                        "notional_usd": abs(ntl),
                        "unrealized_pnl": upnl_pos,
                        "wallet": addr,
                        "wallet_label": label,
                    })

            wallet_snaps.append(WalletSnapshot(
                address=addr,
                label=label,
                short=_short(addr),
                perp_equity=perp_equity,
                spot_usd=spot_usd,
                hl_collateral_usd=hl_coll,
                hl_debt_usd=hl_debt,
                capital_total=cap,
                upnl_perp=upnl,
                health_factor=hl_data.get("health_factor"),
                collateral_symbol=hl_data.get("collateral_symbol"),
                collateral_balance=float(hl_data.get("collateral_balance") or 0.0),
                debt_symbol=hl_data.get("debt_symbol"),
                debt_balance=float(hl_data.get("debt_balance") or 0.0),
                short_positions=short_positions,
                raw_positions=list(d.get("positions") or []),
            ))

    # Some HyperLend wallets may not be listed in FUND_WALLETS (e.g. legacy
    # HYPERLEND_WALLET). Surface them as their own minimal wallet snapshots
    # so the flywheel selector sees them.
    seen = {ws.address for ws in wallet_snaps}
    for addr, hl_data in hl_by_wallet.items():
        if addr in seen:
            continue
        coll = float(hl_data.get("total_collateral_usd") or 0.0)
        debt = float(hl_data.get("total_debt_usd") or 0.0)
        if coll < 0.01 and debt < 0.01:
            continue
        wallet_snaps.append(WalletSnapshot(
            address=addr,
            label=hl_data.get("label") or "HyperLend",
            short=_short(addr),
            perp_equity=0.0,
            spot_usd=0.0,
            hl_collateral_usd=coll,
            hl_debt_usd=debt,
            capital_total=coll,
            upnl_perp=0.0,
            health_factor=hl_data.get("health_factor"),
            collateral_symbol=hl_data.get("collateral_symbol"),
            collateral_balance=float(hl_data.get("collateral_balance") or 0.0),
            debt_symbol=hl_data.get("debt_symbol"),
            debt_balance=float(hl_data.get("debt_balance") or 0.0),
        ))

    # Sort wallets by capital descending (matches /reporte ordering)
    wallet_snaps.sort(key=lambda ws: ws.capital_total, reverse=True)

    # ─── Flywheel selection ───────────────────────────────────────────────
    # Main flywheel = wallet with debt > 0 ranked by COLLATERAL value (the
    # one driving the pair trade carries the most collateral). The legacy
    # bug picked "first wallet with debt" which was order-dependent and
    # surfaced the small UBTC/USDT0 flywheel instead of the large
    # WHYPE/UETH one.
    flywheels = sorted(
        [ws for ws in wallet_snaps if ws.hl_debt_usd > 0.01],
        key=lambda ws: ws.hl_collateral_usd,
        reverse=True,
    )
    main_flywheel = flywheels[0] if flywheels else None
    secondary_flywheel = flywheels[1] if len(flywheels) > 1 else None

    # ─── Basket positions across all wallets ──────────────────────────────
    basket_positions: list[dict[str, Any]] = []
    for ws in wallet_snaps:
        basket_positions.extend(ws.short_positions)
    basket_positions.sort(key=lambda p: p["notional_usd"], reverse=True)
    basket_upnl = sum(p["unrealized_pnl"] for p in basket_positions)
    basket_notional = sum(p["notional_usd"] for p in basket_positions)

    # ─── Aggregates ───────────────────────────────────────────────────────
    capital_total = sum(ws.capital_total for ws in wallet_snaps if ws.capital_total > 0)
    hl_collateral_total = sum(ws.hl_collateral_usd for ws in wallet_snaps)
    hl_debt_total = sum(ws.hl_debt_usd for ws in wallet_snaps)
    perp_equity_total = sum(ws.perp_equity for ws in wallet_snaps)
    spot_usd_total = sum(ws.spot_usd for ws in wallet_snaps)
    upnl_perp_total = sum(ws.upnl_perp for ws in wallet_snaps)

    return PortfolioSnapshot(
        wallets=wallet_snaps,
        capital_total=capital_total,
        hl_collateral_total=hl_collateral_total,
        hl_debt_total=hl_debt_total,
        perp_equity_total=perp_equity_total,
        spot_usd_total=spot_usd_total,
        upnl_perp_total=upnl_perp_total,
        main_flywheel=main_flywheel,
        secondary_flywheel=secondary_flywheel,
        basket_positions=basket_positions,
        basket_upnl=basket_upnl,
        basket_notional=basket_notional,
        market=market_block,
        built_at_ts=time.time(),
        is_fresh=True,
        fetch_attempts=1,
        last_error=None,
    )
