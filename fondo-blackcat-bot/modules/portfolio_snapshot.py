"""Shared portfolio aggregator — single source of truth for the dashboard.

Builds one structured snapshot using the SAME formulas as
``templates/formatters.format_quick_positions`` (which is what /reporte ends
up showing under the hood):

    capital_total_per_wallet = perp_account_value + spot_usd + hl_collateral_usd

(Debt is reported as a separate line — it is **not** subtracted from capital,
matching the way /reporte displays the consolidated portfolio.)

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
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# Module-level cache so /reporte and /dashboard share work and we don't
# hammer the HyperEVM RPC with 200+ parallel calls on every dashboard hit.
# Browser refresh cadence vs RPC rate-limits (-32005) was the root cause of
# HL collateral showing $0 when the dashboard was loaded right after a
# /reporte run.
_SNAPSHOT_CACHE: dict[str, Any] = {"snap": None, "ts": 0.0}
_SNAPSHOT_LOCK: asyncio.Lock | None = None
SNAPSHOT_TTL_SEC = 45.0  # fresh enough for dashboard refresh + /reporte cadence


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
    """Replicates templates.formatters._current_usd_value but aggregated.

    Order of preference per coin:
      1. Stablecoins (USDC/USDH/USDT/USDT0/DAI) → amount 1:1
      2. Live price from ``market.data.prices``
      3. Entry notional (cost basis) as last-resort proxy
    """
    total = 0.0
    for sb in spot_balances or []:
        coin = (sb.get("coin") or "").upper()
        amount = float(sb.get("total") or 0)
        entry_ntl = float(sb.get("entry_ntl") or 0)
        if coin in {"USDC", "USDH", "USDT", "USDT0", "DAI"}:
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


async def build_portfolio_snapshot(force_refresh: bool = False) -> PortfolioSnapshot:
    """Run the four canonical fetches in parallel and stitch the snapshot.

    Cached for ``SNAPSHOT_TTL_SEC`` seconds so concurrent callers (dashboard
    HTTP requests + /reporte + /status) reuse the same fetch instead of
    each issuing their own 200+ RPC calls. The cache is invalidated when
    ``force_refresh=True`` (e.g. by a manual /reporte command if needed).
    """
    global _SNAPSHOT_LOCK
    if _SNAPSHOT_LOCK is None:
        _SNAPSHOT_LOCK = asyncio.Lock()

    now = time.time()
    cached = _SNAPSHOT_CACHE.get("snap")
    cached_ts = _SNAPSHOT_CACHE.get("ts") or 0.0
    if (
        not force_refresh
        and cached is not None
        and (now - cached_ts) < SNAPSHOT_TTL_SEC
    ):
        return cached

    async with _SNAPSHOT_LOCK:
        # Re-check inside the lock — another caller may have just refreshed.
        cached = _SNAPSHOT_CACHE.get("snap")
        cached_ts = _SNAPSHOT_CACHE.get("ts") or 0.0
        if (
            not force_refresh
            and cached is not None
            and (time.time() - cached_ts) < SNAPSHOT_TTL_SEC
        ):
            return cached

        snap = await _build_portfolio_snapshot_inner()
        _SNAPSHOT_CACHE["snap"] = snap
        _SNAPSHOT_CACHE["ts"] = time.time()
        return snap


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
    )
