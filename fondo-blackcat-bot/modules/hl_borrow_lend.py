"""R-PM-LIQ — keyless HyperLiquid borrow-lend reserve LTV source (sync, cached).

WHY THIS EXISTS
---------------
The Portfolio Margin liquidation price depends on each collateral asset's
maintenance threshold, which is derived from its borrow LTV
(``liq_threshold = 0.5 + 0.5 × ltv``). Those LTVs are NOT constants the bot
should hardcode — HyperLiquid can re-risk a reserve. The ``borrowLendReserveState``
info endpoint is keyless and read-only and reports each reserve's live ``ltv``,
so we pull it best-effort and fall back to the conservative default
(``PM_HYPE_LTV`` = 0.50) only when the API is unreachable.

Contract
--------
* **Sync + keyless.** Mirrors ``modules.hl_prices`` (urllib POST, browser UA).
* **Never raises.** On any failure returns ``{}`` (callers default per-token).
* **Cached 5 min** in-memory (no browser storage, no disk, no secrets).
* Returns ``{COIN_UPPER: ltv_float}`` for every reserve that reports a usable
  LTV in ``[0, 1)``.
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
_CACHE_TTL_SEC = 300.0

_cache: dict[str, Any] = {"ts": 0.0, "ltv": {}}


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


def _extract_ltv_map(data: Any) -> dict[str, float]:
    """Best-effort parse of borrowLendReserveState into ``{COIN: ltv}``.

    The endpoint shape has shifted across HL versions, so we walk the payload
    defensively: accept a top-level list or a dict carrying a ``reserves``/
    ``tokens`` list, and for each entry read a token name (``coin``/``name``/
    ``token``) and an ``ltv`` (or ``maxLtv``/``maxLTV``) field. NEVER raises.
    """
    out: dict[str, float] = {}
    rows: list[Any] = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        for key in ("reserves", "tokens", "reserveStates", "data"):
            v = data.get(key)
            if isinstance(v, list):
                rows = v
                break
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = (
            row.get("coin")
            or row.get("name")
            or row.get("token")
            or row.get("symbol")
            or ""
        )
        name = str(name).upper().strip()
        if not name:
            continue
        ltv = None
        for k in ("ltv", "maxLtv", "maxLTV", "LTV"):
            ltv = _safe_float(row.get(k))
            if ltv is not None:
                break
        if ltv is None and isinstance(row.get("state"), dict):
            for k in ("ltv", "maxLtv", "maxLTV", "LTV"):
                ltv = _safe_float(row["state"].get(k))
                if ltv is not None:
                    break
        if ltv is not None and 0.0 < ltv < 1.0:
            out[name] = ltv
    return out


def get_collateral_ltv_map(force: bool = False) -> dict[str, float]:
    """Return ``{COIN: borrow_ltv}`` from HL borrowLendReserveState.

    Keyless, cached 5 min, NEVER raises. Returns ``{}`` (NOT a guess) on
    failure so callers apply their own conservative per-token default.
    """
    now = time.time()
    if (
        not force
        and _cache["ltv"]
        and (now - _cache["ts"]) < _CACHE_TTL_SEC
    ):
        return dict(_cache["ltv"])

    try:
        data = _post({"type": "borrowLendReserveState"})
        out = _extract_ltv_map(data)
        if out:
            _cache.update(ts=now, ltv=out)
            return dict(out)
        return dict(_cache["ltv"])
    except Exception as e:  # noqa: BLE001 — robustness contract
        log.warning("hl_borrow_lend.get_collateral_ltv_map failed: %s", e)
        return dict(_cache["ltv"])
