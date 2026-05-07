"""TraderMap schema validator — R-BOT-LMEC-AUTOFEED (2026-05-07).

Tasks 1 & 3 of the round: detect when ``modules.tradermap.fetch_tradermap_btc``
returns a payload that no longer matches the expected schema (likely
because tradermap.io changed its HTML layout) and fall back to the
LMEC_* env vars without breaking /reporte.

Why this lives in its own module
--------------------------------
* ``modules.tradermap`` is the I/O layer. Keeping validation separate
  means the LMEC evaluator can decide *trust*, not just consume data.
* The validator records each scrape outcome to ``modules.lmec_state``
  so the failure streak survives restarts (Railway Volume).

Public API
----------
* ``validate_tradermap_payload(payload) -> dict``
    Returns ``{ok, errors, normalized}`` with normalized indicator dict.
    ``ok`` is False if any required field has the wrong type — caller
    should fall back to env vars.
* ``record_outcome(payload) -> dict``
    Wraps validate + persistence: validates, records outcome on
    ``lmec_state``, and returns the validation result.
* ``get_indicator_overrides_safely() -> dict``
    Sync helper that reads the env-var overrides only (no scrape) so
    the LMEC evaluator can run without an event loop. Used as the
    final fallback when scraper is unhealthy.
* ``EXPECTED_TYPES`` — dict mapping field → tuple of valid Python types.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# Field → tuple of acceptable Python types (post-coercion).
# ``None`` is allowed for every field — schema requires the *correct*
# type when present, not presence itself.
EXPECTED_TYPES: dict[str, tuple[type, ...]] = {
    "price_usd": (float, int),
    "rsi_weekly": (float, int),
    "macd_weekly_positive": (bool,),
    "ma50w": (float, int),
    "ma200w": (float, int),
    "support": (float, int),
    "resistance": (float, int),
    "trend": (str,),
    "scrape_ok": (bool,),
    "indicator_source": (str,),
}


# Sane numeric ranges so we catch HTML drift (regex grabbing the wrong
# field) before it poisons LMEC. Floats outside the band → schema error.
NUMERIC_RANGES: dict[str, tuple[float, float]] = {
    "price_usd": (1_000.0, 1_000_000.0),
    "rsi_weekly": (0.0, 100.0),
    "ma50w": (1_000.0, 1_000_000.0),
    "ma200w": (1_000.0, 1_000_000.0),
    "support": (1_000.0, 1_000_000.0),
    "resistance": (1_000.0, 1_000_000.0),
}


def validate_tradermap_payload(payload: Any) -> dict[str, Any]:
    """Validate the payload returned by ``fetch_tradermap_btc``.

    Returns::

        {
            "ok": bool,
            "errors": list[str],
            "normalized": dict,        # only the fields that passed
            "had_scrape": bool,        # did scrape return *anything*?
        }

    The function never raises. Caller decides whether to use ``normalized``
    or fall back to env vars based on ``ok``.
    """
    errors: list[str] = []
    normalized: dict[str, Any] = {}

    if not isinstance(payload, dict):
        return {
            "ok": False,
            "errors": ["payload not a dict"],
            "normalized": {},
            "had_scrape": False,
        }

    if payload.get("status") != "ok":
        errors.append(f"status={payload.get('status')!r}")
        # Empty payload is not a *schema* error — it's a network error.
        # We still mark ok=False so caller falls back to env vars.
        return {
            "ok": False,
            "errors": errors,
            "normalized": {},
            "had_scrape": False,
        }

    data = payload.get("data")
    if not isinstance(data, dict):
        return {
            "ok": False,
            "errors": ["data field is not a dict"],
            "normalized": {},
            "had_scrape": False,
        }

    had_scrape = bool(data.get("scrape_ok"))

    for field, types in EXPECTED_TYPES.items():
        if field not in data:
            continue  # absence is OK
        v = data[field]
        if v is None:
            continue
        if not isinstance(v, types):
            errors.append(
                f"{field} has wrong type {type(v).__name__}, expected one of "
                f"{[t.__name__ for t in types]}"
            )
            continue
        # Range check for numeric fields
        if field in NUMERIC_RANGES and isinstance(v, (int, float)):
            lo, hi = NUMERIC_RANGES[field]
            if not (lo <= float(v) <= hi):
                errors.append(
                    f"{field}={v} outside expected range [{lo}, {hi}]"
                )
                continue
        normalized[field] = v

    # Cross-field sanity: if both ma50w and price_usd present, ma200w
    # should generally be < price_usd * 2 (catches obvious unit confusion).
    p = normalized.get("price_usd")
    m200 = normalized.get("ma200w")
    if isinstance(p, (int, float)) and isinstance(m200, (int, float)):
        if m200 > float(p) * 5 or m200 < float(p) / 5:
            errors.append(
                f"ma200w={m200} suspicious vs price_usd={p} (5x divergence)"
            )

    ok = len(errors) == 0
    return {
        "ok": ok,
        "errors": errors,
        "normalized": normalized,
        "had_scrape": had_scrape,
    }


def record_outcome(
    payload: Any, *, persist: bool = True
) -> dict[str, Any]:
    """Validate + record on lmec_state. Returns full validation dict.

    Adds ``failure_streak`` to the result.
    """
    result = validate_tradermap_payload(payload)
    try:
        from modules.lmec_state import record_tradermap_outcome

        streak = record_tradermap_outcome(success=result["ok"], persist=persist)
    except Exception:  # noqa: BLE001
        log.exception("tradermap_validator: state record failed (non-fatal)")
        streak = -1
    result["failure_streak"] = streak
    return result


def get_indicator_overrides_safely() -> dict[str, Any]:
    """Sync env-var-only override fetch. Never raises.

    Mirrors ``modules.tradermap.tradermap_indicator_overrides`` but
    decoupled so we can call it in the LMEC evaluator without pulling in
    httpx if tradermap is hard-failing.
    """
    try:
        from modules.tradermap import tradermap_indicator_overrides

        return tradermap_indicator_overrides() or {}
    except Exception:  # noqa: BLE001
        log.warning("tradermap_validator: env-overrides import failed")
        return {}
