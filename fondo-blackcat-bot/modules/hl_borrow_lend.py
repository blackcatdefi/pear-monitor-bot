"""R-PM-LIQ — keyless HyperLiquid borrow-lend reserve LTV source (sync, cached).

WHY THIS EXISTS
---------------
The Portfolio Margin borrow capacity depends on each collateral asset's live
max-borrow LTV. R-LTV65-QUIET: the MAINTENANCE threshold is a SEPARATE
parameter (``PM_MAINT_LTV`` = 0.75) and is NO LONGER derived from the borrow
LTV — HL raised HYPE max-borrow 0.50 → 0.65 (2026-08-11) WITHOUT changing
maint. LTVs are NOT constants the bot should hardcode — HyperLiquid can
re-risk a reserve. The ``borrowLendReserveState`` info endpoint is keyless and
read-only and reports each reserve's live ``ltv``, so we pull it best-effort
and fall back to the config default (``PM_MAX_BORROW_LTV`` = 0.65) only when
the API is unreachable.

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

from modules import health_registry

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
    # R-BOT-DEFINITIVE WI-4: shared rate-limited + cached HL client first.
    # R-BOT-DEFINITIVE clase C2: el fallback urllib de abajo NO pasa por el
    # rate limiter compartido de hl_client. Activarlo en silencio lleva a 429,
    # y un 429 tragado es lo que produjo funding 0.00. Se conserva el fallback
    # pero se declara la degradacion en vez de pasar de largo.
    try:
        from modules.hl_client import post_info_sync
    except ImportError:  # pragma: no cover
        health_registry.swallowed(
            "pm_state", "hl_client no importable; urllib SIN rate limiter")
    else:
        return post_info_sync(payload)
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


# ------------------------------------------------------------------------
# R-LTV65-QUIET — maintenance-LTV live verification (best-effort).
# ------------------------------------------------------------------------
_maint_cache: dict[str, Any] = {"ts": 0.0, "maint": {}}


def _extract_maint_map(data: Any) -> dict[str, float]:
    """Parse maintenance/liquidation LTV per reserve, if the payload carries it.

    Looks for ``maintenanceLtv``/``maintLtv``/``liquidationLtv``/
    ``liqThreshold`` (top-level or nested under ``state``). NEVER raises.
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
    keys = ("maintenanceLtv", "maintLtv", "liquidationLtv", "liqThreshold",
            "maintenanceLTV", "liquidationThreshold")
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(
            row.get("coin") or row.get("name") or row.get("token")
            or row.get("symbol") or ""
        ).upper().strip()
        if not name:
            continue
        m = None
        for k in keys:
            m = _safe_float(row.get(k))
            if m is not None:
                break
        if m is None and isinstance(row.get("state"), dict):
            for k in keys:
                m = _safe_float(row["state"].get(k))
                if m is not None:
                    break
        if m is not None and 0.0 < m < 1.0:
            out[name] = m
    return out


def get_maintenance_ltv_map(force: bool = False) -> dict[str, float]:
    """Return ``{COIN: maintenance_ltv}`` from HL, or ``{}`` if unverifiable.

    Keyless, cached 5 min, NEVER raises. When empty, callers keep the
    configured ``PM_MAINT_LTV`` default (0.75) and /health surfaces a WARNING
    that live maint verification is unavailable.
    """
    now = time.time()
    if (
        not force
        and _maint_cache["maint"]
        and (now - _maint_cache["ts"]) < _CACHE_TTL_SEC
    ):
        return dict(_maint_cache["maint"])
    try:
        data = _post({"type": "borrowLendReserveState"})
        out = _extract_maint_map(data)
        if out:
            _maint_cache.update(ts=now, maint=out)
            return dict(out)
        return dict(_maint_cache["maint"])
    except Exception as e:  # noqa: BLE001 — robustness contract
        log.warning("hl_borrow_lend.get_maintenance_ltv_map failed: %s", e)
        return dict(_maint_cache["maint"])


def maint_ltv_verification_status() -> dict[str, Any]:
    """Status blob for /health: was live maint LTV verifiable for HYPE?

    ``{"verified": bool, "live_maint": float|None, "config_maint": float}``.
    NEVER raises.
    """
    # R-UNIFIED-LIQ: the config threshold is now DERIVED from the borrow LTV
    # (partial factor LTV+(1−LTV)/2) unless PM_MAINT_LTV manually overrides it.
    try:
        from config import PM_MAX_BORROW_LTV as _cfg_ltv
        from modules.portfolio_margin import _liq_threshold_for_ltv
        _cfg_maint = _liq_threshold_for_ltv(_cfg_ltv)
    except Exception:  # noqa: BLE001
        _cfg_maint = 0.825
    try:
        live = get_maintenance_ltv_map().get("HYPE")
    except Exception:  # noqa: BLE001
        live = None
    return {
        "verified": live is not None,
        "live_maint": live,
        "config_maint": float(_cfg_maint),
    }
