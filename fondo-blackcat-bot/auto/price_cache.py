"""R-SILENT — minimal price cache (BTC/ETH/HYPE) for the dashboard.

The dashboard previously showed ``Cargando precios… (cache vacío o API
timeout, se refresca en 60s)`` indefinitely whenever
``modules.market.fetch_market_data`` failed. The portfolio_snapshot SWR
layer already retains the *last successful* market block, but a cold-
start with no successful market fetch leaves the dashboard empty.

This module persists the last successful BTC/ETH/HYPE price triplet on
disk so a fresh container can render last-known prices on its very first
hit, with a ``(hace Nmin)`` staleness label.

Public API
----------
``record(btc, eth, hype) -> None``
    Persist non-None prices. Fields with ``None`` are kept from the
    previous record. Updates ``ts_epoch``.

``read() -> dict``
    Returns ``{"btc": float|None, "eth": float|None, "hype": float|None,
    "ts_epoch": float, "age_s": int}`` or ``{}`` if nothing cached.

Persistence: ``$DATA_DIR/price_cache.json``.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

log = logging.getLogger(__name__)


def _path() -> str:
    try:
        from config import DATA_DIR  # type: ignore

        base = DATA_DIR
    except Exception:  # noqa: BLE001
        base = os.path.join(os.path.dirname(__file__), "..", "data")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "price_cache.json")


def read() -> dict[str, Any]:
    p = _path()
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return {}
    except Exception:  # noqa: BLE001
        log.warning("price_cache: read failed, ignoring")
        return {}
    ts = float(data.get("ts_epoch") or 0.0)
    if ts > 0:
        data["age_s"] = max(0, int(time.time() - ts))
    return data


def record(btc: float | None, eth: float | None, hype: float | None) -> None:
    """Persist non-None price values. Preserves prior values for fields
    that come in as None.
    """
    prev = read()
    payload: dict[str, Any] = {
        "btc": btc if btc is not None else prev.get("btc"),
        "eth": eth if eth is not None else prev.get("eth"),
        "hype": hype if hype is not None else prev.get("hype"),
        "ts_epoch": time.time(),
    }
    p = _path()
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, p)
    except Exception:  # noqa: BLE001
        log.exception("price_cache: write failed")
        try:
            os.remove(tmp)
        except Exception:  # noqa: BLE001
            pass
