"""R-PEAR-ASSET-INTEGRATION — live on-chain stPEAR valuation (2º activo).

The fund holds PEAR as its second directional long asset, staked as **stPEAR**
on Pear Protocol (Arbitrum). Previously the bot surfaced a static stale value
(``PEAR_STAKED_USD`` ≈ $1.2K) that massively under-counted the real position
(~158K stPEAR ≈ $2.5K and growing with rebate inflows). This module replaces
that fiction with a live read:

    value_usd = stPEAR_balance(on-chain, balanceOf) × PEAR_price(live) × ratio

* **Balance**: ``balanceOf(wallet)`` on the Pear Staker contract (Arbitrum),
  summed across all configured fund wallets. stPEAR is a non-transferable
  ERC-20 minted 1:1 against PEAR, so its balance == the underlying PEAR units.
* **Price**: PEAR/USD from DefiLlama coins (primary, keyless) with a CoinGecko
  fallback (keyless). Both agree to ~0.5%.
* **Ratio**: 1:1 (``STPEAR_TO_PEAR_RATIO``). The linear exit fee applies only at
  redeem time, not to the in-books valuation of the asset.

Robustness contract (mirrors modules.vault_deposits)
-----------------------------------------------------
``get_pear_staked()`` NEVER raises. On ANY failure (RPC down, price feed down,
malformed payloads) it returns ``ok=False`` with ``value_usd=None`` so every
renderer shows **"n/d"** — it NEVER fabricates a value and NEVER falls back to
the old static ``PEAR_STAKED_USD`` hardcode. Read-only and non-custodial: a
single ``eth_call`` (``balanceOf``), no private key, no signing, no transfer.
Results cached in-memory for ``PEAR_STAKING_TTL_SEC`` (default 90s).
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

try:
    from config import (
        ARBITRUM_RPC,
        PEAR_PRICE_COINGECKO,
        PEAR_PRICE_COINGECKO_ID,
        PEAR_PRICE_DEFILLAMA,
        PEAR_STAKER_CONTRACT,
        PEAR_STAKING_TTL_SEC,
        PEAR_STAKING_WALLETS,
        PEAR_TOKEN_ARBITRUM,
        STPEAR_TO_PEAR_RATIO,
    )
except Exception:  # noqa: BLE001 — keep importable in isolated tests
    ARBITRUM_RPC = "https://arb1.arbitrum.io/rpc"
    PEAR_STAKER_CONTRACT = "0xcE3be5204017BB1bD279937f92dF09Fd7F539B92"
    PEAR_TOKEN_ARBITRUM = "0x3212dc0F8c834e4DE893532d27CC9B6001684DB0"
    PEAR_STAKING_WALLETS = ["0xc7ae23316b47f7e75f455f53ad37873a18351505"]
    STPEAR_TO_PEAR_RATIO = 1.0
    PEAR_PRICE_DEFILLAMA = (
        "https://coins.llama.fi/prices/current/arbitrum:" + PEAR_TOKEN_ARBITRUM
    )
    PEAR_PRICE_COINGECKO = (
        "https://api.coingecko.com/api/v3/simple/price"
        "?ids=pear-protocol&vs_currencies=usd"
    )
    PEAR_PRICE_COINGECKO_ID = "pear-protocol"
    PEAR_STAKING_TTL_SEC = 90.0

# balanceOf(address) selector.
_BALANCE_OF_SELECTOR = "0x70a08231"
# stPEAR / PEAR both use 18 decimals (verified on-chain via PEAR.decimals()).
_TOKEN_DECIMALS = 18
_HTTP_TIMEOUT_SEC = 10.0
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# In-memory cache only (never browser storage). {"ts": epoch, "result": PearStaked}
_cache: dict[str, Any] = {"ts": 0.0, "result": None}


@dataclass(frozen=True)
class PearStaked:
    """Live valuation of the fund's stPEAR (2nd fund asset).

    ``ok`` is True only when BOTH the on-chain balance AND a live price were
    obtained. When False, ``value_usd``/``price_usd``/``balance`` are None and
    renderers MUST show "n/d" — never a fabricated or stale number.
    """

    ok: bool
    balance: float | None  # stPEAR units (== underlying PEAR at 1:1)
    price_usd: float | None  # live PEAR/USD
    value_usd: float | None  # balance × price × ratio
    price_source: str | None  # "defillama" | "coingecko"
    error: str | None = None

    @property
    def known(self) -> bool:
        return self.ok and self.value_usd is not None


def _eth_call_balance_of(rpc: str, contract: str, wallet: str) -> int:
    """Single ``balanceOf(wallet)`` eth_call. Raises on transport/RPC error."""
    addr = wallet.lower().replace("0x", "").rjust(64, "0")
    data = _BALANCE_OF_SELECTOR + addr
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": contract, "data": data}, "latest"],
        }
    ).encode()
    req = urllib.request.Request(
        rpc,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": _UA},
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SEC) as r:
        payload = json.load(r)
    if not isinstance(payload, dict):
        raise ValueError("RPC payload not a dict")
    if payload.get("error"):
        raise ValueError(f"RPC error: {payload['error']}")
    result = payload.get("result")
    if not result or result == "0x":
        return 0
    return int(result, 16)


def _fetch_stpear_balance() -> float:
    """Sum stPEAR ``balanceOf`` across all configured fund wallets.

    Raises on TOTAL failure (every wallet read errored). A wallet that simply
    holds 0 stPEAR is fine (contributes 0). Returns balance in token units.
    """
    wallets = [w for w in (PEAR_STAKING_WALLETS or []) if w]
    if not wallets:
        raise ValueError("no PEAR_STAKING_WALLETS configured")
    total_raw = 0
    errors = 0
    last_err: Exception | None = None
    for w in wallets:
        try:
            total_raw += _eth_call_balance_of(ARBITRUM_RPC, PEAR_STAKER_CONTRACT, w)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            last_err = exc
            log.warning("pear_staking: balanceOf failed for %s: %s", w, exc)
    # If EVERY wallet read failed we have no idea of the balance → fail hard so
    # the caller surfaces n/d. A partial success (some wallets read, others 0
    # stPEAR) is still a valid total.
    if errors == len(wallets):
        raise last_err or ValueError("all stPEAR balance reads failed")
    return total_raw / (10 ** _TOKEN_DECIMALS)


def _fetch_pear_price() -> tuple[float, str]:
    """Live PEAR/USD price. DefiLlama primary, CoinGecko fallback.

    Returns ``(price, source)``. Raises only when BOTH feeds fail.
    """
    # ── DefiLlama coins (primary) ──────────────────────────────────────────
    try:
        req = urllib.request.Request(
            PEAR_PRICE_DEFILLAMA,
            headers={"User-Agent": _UA, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SEC) as r:
            data = json.load(r)
        coins = (data or {}).get("coins") or {}
        for _key, entry in coins.items():
            px = entry.get("price")
            if px and float(px) > 0:
                return float(px), "defillama"
    except Exception as exc:  # noqa: BLE001
        log.warning("pear_staking: DefiLlama price failed: %s", exc)

    # ── CoinGecko (fallback) ───────────────────────────────────────────────
    try:
        req = urllib.request.Request(
            PEAR_PRICE_COINGECKO,
            headers={"User-Agent": _UA, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SEC) as r:
            data = json.load(r)
        entry = (data or {}).get(PEAR_PRICE_COINGECKO_ID) or {}
        px = entry.get("usd")
        if px and float(px) > 0:
            return float(px), "coingecko"
    except Exception as exc:  # noqa: BLE001
        log.warning("pear_staking: CoinGecko price failed: %s", exc)

    raise ValueError("all PEAR price feeds failed")


def get_pear_staked(*, force: bool = False) -> PearStaked:
    """Live stPEAR valuation. NEVER raises. n/d on any failure.

    Cached in-memory for ``PEAR_STAKING_TTL_SEC``. Pass ``force=True`` to
    bypass the cache (used by tests / manual refresh).
    """
    now = time.time()
    cached = _cache.get("result")
    if (
        not force
        and isinstance(cached, PearStaked)
        and (now - float(_cache.get("ts") or 0.0)) < float(PEAR_STAKING_TTL_SEC)
    ):
        return cached

    balance: float | None = None
    price: float | None = None
    source: str | None = None
    err: str | None = None
    try:
        balance = _fetch_stpear_balance()
    except Exception as exc:  # noqa: BLE001
        err = f"balance read failed: {exc}"
        log.warning("pear_staking: %s", err)
    if balance is not None:
        try:
            price, source = _fetch_pear_price()
        except Exception as exc:  # noqa: BLE001
            err = f"price read failed: {exc}"
            log.warning("pear_staking: %s", err)

    if balance is not None and price is not None:
        try:
            ratio = float(STPEAR_TO_PEAR_RATIO) or 1.0
        except (TypeError, ValueError):
            ratio = 1.0
        value = balance * price * ratio
        result = PearStaked(
            ok=True,
            balance=balance,
            price_usd=price,
            value_usd=value,
            price_source=source,
            error=None,
        )
        log.info(
            "pear_staking: %.4f stPEAR × $%.6f (%s) = $%.2f",
            balance,
            price,
            source,
            value,
        )
    else:
        # n/d — never fabricate, never fall back to the stale static value.
        result = PearStaked(
            ok=False,
            balance=balance,
            price_usd=price,
            value_usd=None,
            price_source=source,
            error=err or "pear staking read failed",
        )

    _cache["ts"] = now
    _cache["result"] = result
    return result


def pear_staked_capital_fields(*, force: bool = False) -> dict[str, Any]:
    """Dict fragment to merge into the ``compute_net_capital`` input.

    Keys match the canonical capital-dict contract consumed by
    ``auto.capital_calc.compute_net_capital``. On failure ``pear_staked_total``
    is 0.0 (contributes nothing to equity) and ``pear_staked_known`` is False
    so renderers print "n/d" and the equity headline flags PEAR as excluded.
    """
    ps = get_pear_staked(force=force)
    return {
        "pear_staked_total": float(ps.value_usd) if ps.known else 0.0,
        "pear_staked_balance": float(ps.balance) if ps.balance is not None else 0.0,
        "pear_staked_price": float(ps.price_usd) if ps.price_usd is not None else 0.0,
        "pear_staked_known": bool(ps.known),
    }
