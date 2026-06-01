"""R-PMCORE — keyless HyperLiquid oracle/mark price source (sync, cached).

WHY THIS EXISTS
---------------
The fund migrated 100% into HyperLiquid Portfolio Margin. Its core asset is
now a spot HYPE balance (~1,049 HYPE) held in the primary wallet as cross
collateral. The old ``/reporte`` valued non-stable spot using ``entryNtl``
(cost basis) — but for the migrated HYPE balance ``entryNtl`` comes back as
**0.0** from HyperCore, so the report valued ~$75K of HYPE at **$0** and
TOTAL EQUITY collapsed to ~$13K vs Rabby's ~$94K.

The fix is to value HYPE (and any non-stable spot) at the LIVE HL oracle
price. The HL ``metaAndAssetCtxs`` endpoint is keyless, read-only and always
available — far more reliable than CoinGecko (which rate-limits the free tier
and was the silent failure point that left the ``prices`` map empty). This
module is the single, robust price source the formatters consult before
falling back to CoinGecko or cost basis.

Contract
--------
* **Sync + keyless.** Mirrors ``modules.vault_deposits`` (urllib POST, browser
  UA to dodge CF 1010). Safe to call from the synchronous formatter hot path.
* **Never raises.** On any failure returns the last-good cache, or ``{}``.
* **Cached 45s** in-memory (no browser storage, no disk, no secrets).
* Returns ``{COIN_UPPER: price_usd_float}`` from the MAIN perp dex universe,
  preferring ``oraclePx`` (the value HL uses for collateral/PM valuation),
  then ``markPx``, then ``midPx``.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Any

try:
    from config import HYPERLIQUID_API
except Exception:  # noqa: BLE001 — keep importable in isolated tests
    HYPERLIQUID_API = "https://api.hyperliquid.xyz"

log = logging.getLogger(__name__)

_INFO_URL = f"{HYPERLIQUID_API}/info"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_HTTP_TIMEOUT_SEC = 8.0
_CACHE_TTL_SEC = 45.0

_cache: dict[str, Any] = {"ts": 0.0, "prices": {}}


def _post(payload: dict) -> Any:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        _INFO_URL,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": _UA},
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SEC) as r:
        return json.load(r)


def _safe_float(v: Any) -> float | None:
    try:
        f = float(v)
        return f if f == f else None  # reject NaN
    except (TypeError, ValueError):
        return None


def get_oracle_prices(force: bool = False) -> dict[str, float]:
    """Return ``{COIN: oracle_price_usd}`` from HL metaAndAssetCtxs.

    Keyless, cached 45s, NEVER raises. Returns the last-good cache (or ``{}``)
    on failure so a transient HL hiccup never zeroes out spot valuation.
    """
    now = time.time()
    if (
        not force
        and _cache["prices"]
        and (now - _cache["ts"]) < _CACHE_TTL_SEC
    ):
        return dict(_cache["prices"])  # copy — callers must not mutate cache

    try:
        data = _post({"type": "metaAndAssetCtxs"})
        if not isinstance(data, list) or len(data) < 2:
            return dict(_cache["prices"])
        meta, ctxs = data[0], data[1]
        universe = meta.get("universe", []) if isinstance(meta, dict) else []
        out: dict[str, float] = {}
        for idx, asset in enumerate(universe):
            name = (asset.get("name") or "").upper()
            if not name or idx >= len(ctxs):
                continue
            ctx = ctxs[idx] or {}
            for key in ("oraclePx", "markPx", "midPx"):
                px = _safe_float(ctx.get(key))
                if px and px > 0:
                    out[name] = px
                    break
        if out:
            _cache.update(ts=now, prices=out)
            return dict(out)
        return dict(_cache["prices"])
    except Exception as e:  # noqa: BLE001 — robustness contract
        log.warning("hl_prices.get_oracle_prices failed: %s", e)
        return dict(_cache["prices"])


def get_price(coin: str, force: bool = False) -> float | None:
    """Live oracle price for one coin (kHYPE→HYPE proxy). None if unknown."""
    if not coin:
        return None
    c = coin.upper()
    lookup = c[1:] if c.startswith("K") and len(c) > 1 else c
    prices = get_oracle_prices(force=force)
    return prices.get(lookup) or prices.get(c)


def get_hype_price(force: bool = False) -> float | None:
    """Convenience: live HYPE oracle price. None on failure."""
    return get_price("HYPE", force=force)
