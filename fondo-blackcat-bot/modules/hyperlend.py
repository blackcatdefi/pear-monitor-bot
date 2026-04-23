"""HyperLend on-chain reader (Aave v3 fork on HyperEVM).

Reads getUserAccountData() from the Pool contract via web3.py to obtain the
aggregate USD values (collateral, debt, HF, LT, ltv).

Additionally — and this is the change on 2026-04-17 — it now enumerates
individual reserves via getReservesList() + getReserveData() and queries
per-asset collateral / variable-debt balances so the bot can report the
ACTUAL borrowed asset symbol (e.g. UETH instead of hardcoded USDH). This is
critical now that the Reserva wallet (0xA44E) rotated its debt from USDH
to UETH — the flywheel is now an implicit PAIR TRADE (LONG HYPE via kHYPE
collateral, SHORT ETH via UETH debt).

Mirrors the proven Node implementation in src/hyperLendApi.js plus the new
per-reserve enumeration.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from web3 import Web3
from web3.exceptions import ContractLogicError

from config import (
    FUND_WALLETS,
    HYPEREVM_CHAIN_ID,
    HYPEREVM_RPC,
    HYPERLEND_POOL_ADDRESS,
    HYPERLEND_WALLET,
)

log = logging.getLogger(__name__)

BASE_DECIMALS = 8
HF_DECIMALS = 18
MAX_UINT_THRESHOLD = 1 << 255  # values >= here are treated as "infinity" (no debt)

POOL_ABI: list[dict[str, Any]] = [
    {
        "inputs": [{"internalType": "address", "name": "user", "type": "address"}],
        "name": "getUserAccountData",
        "outputs": [
            {"internalType": "uint256", "name": "totalCollateralBase", "type": "uint256"},
            {"internalType": "uint256", "name": "totalDebtBase", "type": "uint256"},
            {"internalType": "uint256", "name": "availableBorrowsBase", "type": "uint256"},
            {"internalType": "uint256", "name": "currentLiquidationThreshold", "type": "uint256"},
            {"internalType": "uint256", "name": "ltv", "type": "uint256"},
            {"internalType": "uint256", "name": "healthFactor", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "getReservesList",
        "outputs": [{"internalType": "address[]", "name": "", "type": "address[]"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "asset", "type": "address"}],
        "name": "getReserveData",
        "outputs": [
            {
                "components": [
                    {
                        "components": [
                            {"internalType": "uint256", "name": "data", "type": "uint256"},
                        ],
                        "internalType": "struct DataTypes.ReserveConfigurationMap",
                        "name": "configuration",
                        "type": "tuple",
                    },
                    {"internalType": "uint128", "name": "liquidityIndex", "type": "uint128"},
                    {"internalType": "uint128", "name": "currentLiquidityRate", "type": "uint128"},
                    {"internalType": "uint128", "name": "variableBorrowIndex", "type": "uint128"},
                    {"internalType": "uint128", "name": "currentVariableBorrowRate", "type": "uint128"},
                    {"internalType": "uint128", "name": "currentStableBorrowRate", "type": "uint128"},
                    {"internalType": "uint40", "name": "lastUpdateTimestamp", "type": "uint40"},
                    {"internalType": "uint16", "name": "id", "type": "uint16"},
                    {"internalType": "address", "name": "aTokenAddress", "type": "address"},
                    {"internalType": "address", "name": "stableDebtTokenAddress", "type": "address"},
                    {"internalType": "address", "name": "variableDebtTokenAddress", "type": "address"},
                    {"internalType": "address", "name": "interestRateStrategyAddress", "type": "address"},
                    {"internalType": "uint128", "name": "accruedToTreasury", "type": "uint128"},
                    {"internalType": "uint128", "name": "unbacked", "type": "uint128"},
                    {"internalType": "uint128", "name": "isolationModeTotalDebt", "type": "uint128"},
                ],
                "internalType": "struct DataTypes.ReserveDataLegacy",
                "name": "",
                "type": "tuple",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

ERC20_ABI: list[dict[str, Any]] = [
    {
        "inputs": [],
        "name": "symbol",
        "outputs": [{"internalType": "string", "name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# Cache reserve metadata per Pool so we don't re-fetch on every call
_RESERVE_CACHE: dict[str, list[dict[str, Any]]] = {}

# ── Round 14 hotfix: authoritative address → canonical symbol map ──────────
# Sourced from https://api.hyperlend.finance/data/markets?chain=hyperEvm
# (verified 2026-04-23). Using a hardcoded map bypasses fragile on-chain
# symbol() calls which can:
#   (a) revert or time out under HyperEVM free-RPC rate limits,
#   (b) return unicode variants (e.g. "USD₮0" with U+20AE vs plain "USDT0"),
#   (c) return bytes32-packed symbols that web3.py can't decode into str.
# Any symbol lookup for matching on /flywheel MUST go through this map —
# never through symbol().call().
KNOWN_RESERVE_ADDRESSES: dict[str, str] = {
    "0x5555555555555555555555555555555555555555": "WHYPE",
    "0x94e8396e0869c9F2200760aF0621aFd240E1CF38": "wstHYPE",
    "0x9FDBdA0A5e284c32744D2f17Ee5c74B284993463": "UBTC",
    "0xBe6727B535545C67d5cAa73dEa54865B92CF7907": "UETH",
    "0x5d3a1Ff2b6BAb83b63cd9AD0787074081a52ef34": "USDe",
    "0xB8CE59FC3717ada4C02eaDF9682A9e934F625ebb": "USDT0",
    "0x211Cc4DD073734dA055fbF44a2b4667d5E5fE5d2": "sUSDe",
    "0xb50A96253aBDF803D85efcDce07Ad8becBc52BD5": "USDHL",
    "0xfD739d4e423301CE9385c1fb8850539D657C296D": "kHYPE",
    "0x0aD339d66BF4AeD5ce31c64Bc37B3244b6394A77": "USR",
    "0x311dB0FDe558689550c68355783c95eFDfe25329": "PT-kHYPE-13NOV2025",
    "0xb7379d395F3c83952ad794896205f7E33E358735": "PT-sUSDE-25SEP2025",
    "0x068f321Fa8Fb9f0D135f290Ef6a3e2813e1c8A29": "USOL",
    "0xd8FC8F0b03eBA61F64D08B0bef69d80916E5DdA9": "beHYPE",
    "0xb88339CB7199b77E23DB6E890353E22632Ba630f": "USDC",
    "0x111111a1a0667d36bD57c0A9f569b98057111111": "USDH",
    "0xea84ca9849D9e76a78B91F221F84e9Ca065FC9f5": "PT-kHYPE-19MAR2026",
}

# Reserves with supplyCap=1 and borrowCap=1 in HyperLend — deprecated /
# frozen / principal-token markets that return distorted APYs (often
# 80-110%). Never surface these in /flywheel or alerts.
DEPRECATED_RESERVE_ADDRESSES: set[str] = {
    "0x211Cc4DD073734dA055fbF44a2b4667d5E5fE5d2",  # sUSDe (frozen)
    "0xb50A96253aBDF803D85efcDce07Ad8becBc52BD5",  # USDHL (legacy Felix)
    "0x0aD339d66BF4AeD5ce31c64Bc37B3244b6394A77",  # USR (frozen)
    "0x311dB0FDe558689550c68355783c95eFDfe25329",  # PT-kHYPE-13NOV2025
    "0xb7379d395F3c83952ad794896205f7E33E358735",  # PT-sUSDE-25SEP2025
    "0xea84ca9849D9e76a78B91F221F84e9Ca065FC9f5",  # PT-kHYPE-19MAR2026
}

_KNOWN_RESERVE_LC = {a.lower(): s for a, s in KNOWN_RESERVE_ADDRESSES.items()}
_DEPRECATED_LC = {a.lower() for a in DEPRECATED_RESERVE_ADDRESSES}


def canonical_symbol_for(address: str) -> str | None:
    """Return the canonical symbol for a HyperLend reserve address, or None."""
    if not address:
        return None
    return _KNOWN_RESERVE_LC.get(address.lower())


def is_deprecated_reserve(address: str) -> bool:
    if not address:
        return False
    return address.lower() in _DEPRECATED_LC


class HyperLend:
    def __init__(
        self,
        rpc_url: str = HYPEREVM_RPC,
        pool_address: str = HYPERLEND_POOL_ADDRESS,
    ) -> None:
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 20}))
        self.pool_address = Web3.to_checksum_address(pool_address)
        self.pool = self.w3.eth.contract(
            address=self.pool_address,
            abi=POOL_ABI,
        )

    # ─── Reserve enumeration ────────────────────────────────────────────
    def _erc20(self, address: str):
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(address), abi=ERC20_ABI
        )

    def _safe_symbol(self, address: str) -> str:
        try:
            return self._erc20(address).functions.symbol().call()
        except Exception:  # noqa: BLE001
            return address[:10]

    def _safe_decimals(self, address: str) -> int:
        try:
            return int(self._erc20(address).functions.decimals().call())
        except Exception:  # noqa: BLE001
            return 18

    def _load_reserves_sync(self) -> list[dict[str, Any]]:
        """Load and cache reserve metadata (asset/aToken/variableDebtToken addresses + symbols)."""
        cache_key = self.pool_address
        cached = _RESERVE_CACHE.get(cache_key)
        if cached is not None:
            return cached

        try:
            assets: list[str] = self.pool.functions.getReservesList().call()
        except Exception as exc:  # noqa: BLE001
            log.warning("getReservesList() failed: %s", exc)
            _RESERVE_CACHE[cache_key] = []
            return []

        out: list[dict[str, Any]] = []
        for asset in assets:
            time.sleep(0.5)  # avoid RPC rate limit -32005
            try:
                rd = self.pool.functions.getReserveData(
                    Web3.to_checksum_address(asset)
                ).call()
                # rd is a tuple matching ReserveDataLegacy ordering.
                # Index map:
                #   0 configuration, 1 liquidityIndex, 2 currentLiquidityRate,
                #   3 variableBorrowIndex, 4 currentVariableBorrowRate (ray),
                #   5 currentStableBorrowRate, 6 lastUpdateTimestamp, 7 id,
                #   8 aTokenAddress, 9 stableDebtToken, 10 variableDebtToken
                liquidity_rate_ray = int(rd[2])
                variable_borrow_rate_ray = int(rd[4])
                stable_borrow_rate_ray = int(rd[5])
                last_update_ts = int(rd[6])
                a_token = rd[8]
                var_debt_token = rd[10]
            except (ContractLogicError, Exception) as exc:  # noqa: BLE001
                log.warning("getReserveData(%s) failed: %s", asset, exc)
                continue

            # Round 14 hotfix: prefer the authoritative address→symbol map.
            # Only fall back to on-chain symbol()/decimals() if the asset is
            # not in the map (so NEW HyperLend listings still show up, just
            # potentially with the chain's raw symbol until we update the map).
            canonical = canonical_symbol_for(asset)
            if canonical:
                symbol = canonical
                chain_symbol = canonical  # save an RPC round-trip
                decimals = 18 if canonical in ("UETH", "WHYPE", "wstHYPE", "kHYPE", "beHYPE", "USDe", "sUSDe", "USDHL", "USR") else None
                if decimals is None:
                    # USDC=6, USDT0=6, USDH=6, UBTC=8, USOL=6-ish — resolve via RPC
                    decimals = self._safe_decimals(asset)
            else:
                symbol = self._safe_symbol(asset)
                chain_symbol = symbol
                decimals = self._safe_decimals(asset)

            out.append(
                {
                    "asset": asset,
                    "symbol": symbol,
                    "chain_symbol": chain_symbol,
                    "deprecated": is_deprecated_reserve(asset),
                    "decimals": decimals,
                    "a_token": a_token,
                    "variable_debt_token": var_debt_token,
                    # Round 13: raw rates in ray (10^27) — Aave v3 semantics.
                    # APR = rate / 1e27 (expressed per year).
                    # APY = (1 + APR / SECONDS_PER_YEAR) ** SECONDS_PER_YEAR - 1
                    "liquidity_rate_ray": liquidity_rate_ray,
                    "variable_borrow_rate_ray": variable_borrow_rate_ray,
                    "stable_borrow_rate_ray": stable_borrow_rate_ray,
                    "last_update_ts": last_update_ts,
                }
            )

        _RESERVE_CACHE[cache_key] = out
        return out

    def _get_reserves_sync(self) -> list[dict[str, Any]]:
        return self._load_reserves_sync()

    def _per_reserve_balances_sync(self, user: str) -> dict[str, list[dict[str, Any]]]:
        """Return {'collateral': [...], 'debt': [...]} where each item has
        symbol, asset address, token_address, raw balance, human balance.
        """
        user_addr = Web3.to_checksum_address(user)
        reserves = self._get_reserves_sync()
        collateral: list[dict[str, Any]] = []
        debt: list[dict[str, Any]] = []
        for r in reserves:
            dec = r["decimals"] or 18
            try:
                a_bal = self._erc20(r["a_token"]).functions.balanceOf(user_addr).call()
            except Exception:  # noqa: BLE001
                a_bal = 0
            try:
                d_bal = (
                    self._erc20(r["variable_debt_token"])
                    .functions.balanceOf(user_addr)
                    .call()
                )
            except Exception:  # noqa: BLE001
                d_bal = 0

            if a_bal > 0:
                collateral.append(
                    {
                        "symbol": r["symbol"],
                        "asset": r["asset"],
                        "a_token": r["a_token"],
                        "balance_raw": a_bal,
                        "balance": a_bal / (10 ** dec),
                        "decimals": dec,
                    }
                )
            if d_bal > 0:
                debt.append(
                    {
                        "symbol": r["symbol"],
                        "asset": r["asset"],
                        "debt_token": r["variable_debt_token"],
                        "balance_raw": d_bal,
                        "balance": d_bal / (10 ** dec),
                        "decimals": dec,
                    }
                )

        return {"collateral": collateral, "debt": debt}

    # ─── Aggregate account data ─────────────────────────────────────────
    def _get_account_data_sync(self, address: str) -> dict[str, Any]:
        addr = Web3.to_checksum_address(address)
        r = self.pool.functions.getUserAccountData(addr).call()
        (
            total_collateral_base,
            total_debt_base,
            available_borrows_base,
            liq_threshold_bps,
            ltv_bps,
            hf_raw,
        ) = r

        def to_usd(v: int) -> float:
            return v / (10 ** BASE_DECIMALS)

        if hf_raw >= MAX_UINT_THRESHOLD:
            health_factor: float = float("inf")
        else:
            health_factor = hf_raw / (10 ** HF_DECIMALS)

        # Enumerate reserves to report the ACTUAL borrowed/collateral assets.
        try:
            breakdown = self._per_reserve_balances_sync(addr)
        except Exception as exc:  # noqa: BLE001
            log.warning("per-reserve balances failed for %s: %s", addr, exc)
            breakdown = {"collateral": [], "debt": []}

        asset_detail_ok = bool(breakdown.get("collateral") or breakdown.get("debt"))

        # Pick "primary" (largest balance) collateral and debt for convenience.
        primary_collateral = (
            max(breakdown["collateral"], key=lambda x: x["balance"])
            if breakdown["collateral"]
            else None
        )
        primary_debt = (
            max(breakdown["debt"], key=lambda x: x["balance"])
            if breakdown["debt"]
            else None
        )

        return {
            "wallet": addr,
            "total_collateral_usd": to_usd(total_collateral_base),
            "total_debt_usd": to_usd(total_debt_base),
            "available_borrows_usd": to_usd(available_borrows_base),
            "current_liquidation_threshold": liq_threshold_bps / 10000,
            "ltv": ltv_bps / 10000,
            "health_factor": health_factor,
            # Per-reserve breakdown — list of {symbol, balance, asset, ...}
            "collateral_assets": breakdown["collateral"],
            "debt_assets": breakdown["debt"],
            "primary_collateral": primary_collateral,  # dict or None
            "primary_debt": primary_debt,              # dict or None
            # Convenience scalars — ready to plug into formatters / analysis.
            "collateral_symbol": primary_collateral["symbol"] if primary_collateral else None,
            "collateral_balance": primary_collateral["balance"] if primary_collateral else 0.0,
            "debt_symbol": primary_debt["symbol"] if primary_debt else None,
            "debt_balance": primary_debt["balance"] if primary_debt else 0.0,
            "asset_detail_note": None if asset_detail_ok else "asset detail unavailable",
        }

    async def get_account_data(self, address: str = HYPERLEND_WALLET) -> dict[str, Any]:
        # web3.py is sync — run in a thread to avoid blocking the event loop
        return await asyncio.to_thread(self._get_account_data_sync, address)


# ─── Pricing helpers for HF projection / liq calc ───────────────────────
# Map reserve symbols → CoinGecko-ish ticker. Used by liq_calc / flywheel
# when we want to simulate HF under different price scenarios.
SYMBOL_TO_TICKER: dict[str, str] = {
    "kHYPE": "HYPE",
    "wkHYPE": "HYPE",
    "HYPE": "HYPE",
    "wHYPE": "HYPE",
    "UETH": "ETH",
    "WETH": "ETH",
    "ETH": "ETH",
    "USDH": "USD",
    "USDC": "USD",
    "USDT": "USD",
    "USDhl": "USD",
    "DAI": "USD",
}


def symbol_to_ticker(symbol: str | None) -> str:
    if not symbol:
        return "?"
    return SYMBOL_TO_TICKER.get(symbol, symbol.upper())


def project_health_factor(
    collateral_balance: float,
    collateral_price: float,
    liquidation_threshold: float,
    debt_balance: float,
    debt_price: float,
) -> float:
    """HF = (collateral_balance × collateral_price × LT) / (debt_balance × debt_price).

    Returns inf when debt is zero.
    """
    if debt_balance <= 0 or debt_price <= 0:
        return float("inf")
    numerator = collateral_balance * collateral_price * liquidation_threshold
    denominator = debt_balance * debt_price
    if denominator <= 0:
        return float("inf")
    return numerator / denominator


# ─── Public fetchers ────────────────────────────────────────────────────
async def fetch_hyperlend(address: str = HYPERLEND_WALLET) -> dict[str, Any]:
    """Fetch HyperLend state for a single wallet with graceful error handling."""
    try:
        client = HyperLend()
        data = await client.get_account_data(address)
        return {"status": "ok", "data": data}
    except Exception as exc:  # noqa: BLE001
        log.exception("HyperLend fetch failed")
        return {"status": "error", "error": str(exc)}


async def fetch_all_hyperlend() -> list[dict[str, Any]]:
    """Fetch HyperLend state for ALL fund wallets + the legacy HYPERLEND_WALLET.

    Returns a list of {status, data|error, label} dicts.
    Only wallets with non-zero collateral are included in the response.
    """
    wallets: dict[str, str] = {}
    if HYPERLEND_WALLET:
        wallets[HYPERLEND_WALLET] = "HyperLend Principal"
    for addr, label in FUND_WALLETS.items():
        if addr not in wallets:
            wallets[addr] = label

    if not wallets:
        return [{"status": "error", "error": "no_wallets_configured", "label": "?"}]

    client = HyperLend()

    async def _query(addr: str, label: str) -> dict[str, Any]:
        try:
            data = await client.get_account_data(addr)
            data["label"] = label
            return {"status": "ok", "data": data, "label": label}
        except Exception as exc:  # noqa: BLE001
            log.warning("HyperLend fetch %s (%s) failed: %s", label, addr[:10], exc)
            return {"status": "error", "error": str(exc), "label": label}

    results = await asyncio.gather(*[_query(a, l) for a, l in wallets.items()])

    filtered: list[dict[str, Any]] = []
    for r in results:
        if r["status"] == "error":
            continue
        coll = r["data"].get("total_collateral_usd", 0.0) or 0.0
        if coll > 0.01:
            filtered.append(r)

    if not filtered:
        return [{"status": "ok", "data": {
            "wallet": "",
            "label": "—",
            "total_collateral_usd": 0.0,
            "total_debt_usd": 0.0,
            "available_borrows_usd": 0.0,
            "current_liquidation_threshold": 0.0,
            "ltv": 0.0,
            "health_factor": float("inf"),
            "collateral_assets": [],
            "debt_assets": [],
            "primary_collateral": None,
            "primary_debt": None,
            "collateral_symbol": None,
            "collateral_balance": 0.0,
            "debt_symbol": None,
            "debt_balance": 0.0,
        }, "label": "—"}]

    return filtered


async def get_health_factor(address: str = HYPERLEND_WALLET) -> float | None:
    res = await fetch_hyperlend(address)
    if res["status"] == "ok":
        return res["data"]["health_factor"]
    return None


# ─── Round 13: borrow/supply rate reader (UETH flywheel cost) ──────────────
# Aave v3 semantics recap:
#   currentVariableBorrowRate is a uint128 in ray (1e27) expressed per year.
#   APR  = rate_ray / 1e27
#   APY  = (1 + APR / SECONDS_PER_YEAR) ** SECONDS_PER_YEAR - 1
# For UETH borrow cost monitoring, we surface both APR (raw) and APY
# (compounded) so /flywheel can show "Costo borrow UETH: X% APR / Y% APY"
# and modules/alerts.py can raise a warning if either exceeds 10%.
RAY = 10**27
SECONDS_PER_YEAR = 31_536_000  # 365 * 86_400 — matches Aave convention

_RATES_CACHE: dict[str, Any] = {
    "fetched_at": 0.0,
    "data": {},
}
_RATES_TTL_SEC = 15 * 60  # 15 minutes — same cadence as /reporte cadence


def _ray_to_apr(rate_ray: int) -> float:
    if rate_ray <= 0:
        return 0.0
    return rate_ray / RAY


def _apr_to_apy(apr: float) -> float:
    if apr <= 0:
        return 0.0
    try:
        return (1 + apr / SECONDS_PER_YEAR) ** SECONDS_PER_YEAR - 1
    except OverflowError:
        return apr  # degrade gracefully if extreme values appear


def _load_rates_sync() -> dict[str, Any]:
    """Return {symbol: {apr_borrow, apy_borrow, apr_supply, apy_supply, ...}}.

    Uses the module-level reserves cache so the only RPC cost here is the
    initial reserves scan (done once per process). Safe for every /flywheel
    call; additional TTL cache on top (_RATES_CACHE) deduplicates bursts.
    """
    client = HyperLend()
    reserves = client._load_reserves_sync()
    out: dict[str, dict[str, Any]] = {}
    for r in reserves:
        sym = (r.get("symbol") or "").strip()
        if not sym:
            continue
        apr_borrow = _ray_to_apr(r.get("variable_borrow_rate_ray", 0))
        apy_borrow = _apr_to_apy(apr_borrow)
        apr_supply = _ray_to_apr(r.get("liquidity_rate_ray", 0))
        apy_supply = _apr_to_apy(apr_supply)
        out[sym] = {
            "symbol": sym,
            "chain_symbol": r.get("chain_symbol"),
            "asset": r.get("asset"),
            "deprecated": bool(r.get("deprecated")),
            "apr_borrow": apr_borrow,
            "apy_borrow": apy_borrow,
            "apr_supply": apr_supply,
            "apy_supply": apy_supply,
            "last_update_ts": r.get("last_update_ts", 0),
        }
    return out


async def fetch_reserve_rates(force: bool = False) -> dict[str, Any]:
    """Return on-chain borrow/supply rates for all HyperLend reserves.

    Result shape:
        {
          "status": "ok" | "error",
          "fetched_at_iso": "2026-04-23T14:30:00Z",
          "rates": {
              "UETH": {"apr_borrow": 0.1617, "apy_borrow": 0.1759, ...},
              "USDH": {...}, "USDT0": {...}, "USDC": {...}, ...
          },
          "error": "..."  # only present on error
        }

    Results cached for 15 minutes to avoid hammering the RPC; set force=True
    to bypass the cache (used by /debug_x).
    """
    import time as _time
    now = _time.time()
    if not force and (now - _RATES_CACHE["fetched_at"]) < _RATES_TTL_SEC and _RATES_CACHE["data"]:
        return _RATES_CACHE["data"]

    try:
        rates = await asyncio.to_thread(_load_rates_sync)
    except Exception as exc:  # noqa: BLE001
        log.warning("fetch_reserve_rates failed: %s", exc)
        return {
            "status": "error",
            "error": str(exc),
            "fetched_at_iso": None,
            "rates": {},
        }

    from datetime import datetime as _dt, timezone as _tz
    payload = {
        "status": "ok",
        "fetched_at_iso": _dt.now(_tz.utc).isoformat(),
        "rates": rates,
    }
    _RATES_CACHE["fetched_at"] = now
    _RATES_CACHE["data"] = payload
    return payload


async def get_borrow_apy(symbol: str) -> float | None:
    """Convenience: return compounded APY for a given debt asset.

    Returns None if the reserve is not found or rates cannot be fetched.
    """
    data = await fetch_reserve_rates()
    if data.get("status") != "ok":
        return None
    entry = (data.get("rates") or {}).get(symbol)
    if not entry:
        # Try case-insensitive lookup
        for k, v in (data.get("rates") or {}).items():
            if k.lower() == symbol.lower():
                entry = v
                break
    if not entry:
        return None
    return float(entry.get("apy_borrow") or 0.0)
