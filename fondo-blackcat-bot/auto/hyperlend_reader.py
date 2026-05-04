"""R-FINAL — Bug #2 fix: HyperLend HF=∞ rate-limit false positive.

Symptom (apr-30 2026):
    /reporte showed `HF: ∞ (collateral=0, debt=0)` for the flywheel wallet
    (0xa44e). Wrong. Real state: 1,750 WHYPE collateral / 19.27 UETH debt
    → HF ~1.214.

Root cause:
    modules.hyperlend.fetch_all_hyperlend() handles per-wallet RPC rate-
    limit by retrying 3× and then returning {"status":"error", ...}. When
    *every* wallet errors out, the filter loop produces an empty list and
    a hardcoded placeholder dict whose health_factor = float("inf") and
    collateral/debt = 0.0 is substituted. Downstream consumers (formatter
    + LLM) receive sane-looking-but-wrong values and report "HF ∞".

Fix:
    Wrap fetch_all_hyperlend() with a graceful-degradation reader:
      - Persist last successful HF read per wallet into a JSON cache
        (TTL configurable, default 1h).
      - On total RPC failure, return the cached state with a
        ``status='UNKNOWN'`` flag and ``last_known_hf`` + ``age_seconds``.
      - Format helper renders the unknown state with a clear message
        instead of "HF ∞".

Public API:
    read_all_with_cache() -> list[dict]
        Same shape as fetch_all_hyperlend() result, but every entry has a
        ``hf_status`` field: 'OK' | 'UNKNOWN' | 'ZERO' (collateral=0).
        On UNKNOWN, ``last_known_hf`` and ``age_seconds`` are populated.

    format_hf_line(entry) -> str
        Helper to render the HF block for /flywheel /reporte /risk_check.

Kill switch: HYPERLEND_AUTOREADER=false (default true) → falls back to raw fetch.

Cache: $DATA_DIR/hyperlend_hf_cache.json — persists across restarts.

Env vars:
    HYPERLEND_RETRY_MAX=3            (additional inner retries; legacy already retries)
    HYPERLEND_RETRY_BASE_SEC=2       (base delay)
    HYPERLEND_CACHE_TTL_SEC=3600     (1h, but UNKNOWN status persists last_known
                                      regardless of TTL — TTL only gates 'fresh')
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from typing import Any

log = logging.getLogger(__name__)

ENABLED = os.getenv("HYPERLEND_AUTOREADER", "true").strip().lower() != "false"

# R-DASHBOARD-DEBT-SYMBOL: authoritative asset-address → symbol map for the
# most common HyperLend reserves. Mirrors modules/hyperlend.py
# KNOWN_RESERVE_ADDRESSES (lowercase keys; no import to avoid circular deps).
# Used to recover debt_symbol when per-reserve balanceOf() calls fail or
# return 0, leaving primary_debt=None and debt_symbol=None in the live fetch.
_KNOWN_RESERVE_SYMBOLS: dict[str, str] = {
    "0x5555555555555555555555555555555555555555": "WHYPE",
    "0x94e8396e0869c9f2200760af0621afd240e1cf38": "wstHYPE",
    "0x9fdbda0a5e284c32744d2f17ee5c74b284993463": "UBTC",
    "0xbe6727b535545c67d5caa73dea54865b92cf7907": "UETH",
    "0x5d3a1ff2b6bab83b63cd9ad0787074081a52ef34": "USDe",
    "0xb8ce59fc3717ada4c02eadf9682a9e934f625ebb": "USDT0",
    "0xb88339cb7199b77e23db6e890353e22632ba630f": "USDC",
    "0x111111a1a0667d36bd57c0a9f569b98057111111": "USDH",
    "0xfd739d4e423301ce9385c1fb8850539d657c296d": "kHYPE",
    "0xd8fc8f0b03eba61f64d08b0bef69d80916e5dda9": "beHYPE",
    "0x068f321fa8fb9f0d135f290ef6a3e2813e1c8a29": "USOL",
}


def _sym_from_asset(asset: str | None) -> str | None:
    """Return canonical symbol for a reserve asset address, or None."""
    if not asset:
        return None
    return _KNOWN_RESERVE_SYMBOLS.get(asset.lower())
RETRY_MAX = max(1, int(os.getenv("HYPERLEND_RETRY_MAX", "3") or 3))
RETRY_BASE_SEC = float(os.getenv("HYPERLEND_RETRY_BASE_SEC", "2") or 2)
CACHE_TTL_SEC = int(os.getenv("HYPERLEND_CACHE_TTL_SEC", "3600") or 3600)


def _cache_path() -> str:
    """Return $DATA_DIR/hyperlend_hf_cache.json (auto-created)."""
    try:
        from config import DATA_DIR  # type: ignore

        base = DATA_DIR
    except Exception:  # noqa: BLE001
        base = os.path.join(os.path.dirname(__file__), "..", "data")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "hyperlend_hf_cache.json")


def _load_cache() -> dict[str, Any]:
    path = _cache_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        log.warning("hyperlend_reader: cache read failed, starting fresh")
        return {}


def _save_cache(cache: dict[str, Any]) -> None:
    path = _cache_path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        log.exception("hyperlend_reader: cache write failed")


def _is_finite_hf(hf: Any) -> bool:
    try:
        return math.isfinite(float(hf))
    except Exception:  # noqa: BLE001
        return False


def _classify_entry(entry: dict[str, Any]) -> str:
    """Tag the entry with hf_status: 'OK' | 'ZERO' | 'UNKNOWN'."""
    if entry.get("status") != "ok":
        return "UNKNOWN"
    data = entry.get("data") or {}
    collateral = float(data.get("total_collateral_usd") or 0.0)
    debt = float(data.get("total_debt_usd") or 0.0)
    hf = data.get("health_factor")

    if collateral <= 0.01 and debt <= 0.01:
        # Truly zero — empty wallet. Acceptable.
        return "ZERO"
    if collateral > 0.01 and debt <= 0.01:
        # Has collateral, zero debt — HF mathematically infinite, OK to render as ∞.
        return "OK"
    if _is_finite_hf(hf):
        return "OK"
    return "UNKNOWN"


def _maybe_recover_from_cache(
    entry: dict[str, Any], cache: dict[str, Any]
) -> dict[str, Any]:
    """If entry classifies UNKNOWN, look up cached HF and decorate.

    Mutates a *copy* of the entry to add:
      hf_status, last_known_hf, last_known_at_iso, age_seconds, recovered_from_cache.
    """
    out = dict(entry)
    data = dict(out.get("data") or {})

    addr = (data.get("wallet") or "").lower()
    cached = cache.get(addr) if addr else None
    if cached and isinstance(cached, dict):
        last_hf = cached.get("hf")
        last_at = cached.get("ts_utc")
        last_collat = cached.get("collateral_usd")
        last_debt = cached.get("debt_usd")
        age = None
        try:
            age = max(0, int(time.time() - float(cached.get("ts_epoch") or 0)))
        except Exception:  # noqa: BLE001
            age = None
        updates: dict[str, Any] = {
            "last_known_hf": last_hf,
            "last_known_at_iso": last_at,
            "last_known_collateral_usd": last_collat,
            "last_known_debt_usd": last_debt,
            "age_seconds": age,
            "recovered_from_cache": True,
        }
        # R-DASH-FIX Bug 3: restore cached collateral/debt symbols+balances so
        # the flywheel card shows "kHYPE / UETH" even on per-reserve RPC failure.
        if cached.get("collateral_symbol"):
            updates["collateral_symbol"] = cached["collateral_symbol"]
            updates["collateral_balance"] = float(cached.get("collateral_balance") or 0.0)
        if cached.get("debt_symbol"):
            updates["debt_symbol"] = cached["debt_symbol"]
            updates["debt_balance"] = float(cached.get("debt_balance") or 0.0)
        data.update(updates)
    out["data"] = data
    out["hf_status"] = "UNKNOWN"
    return out


def _persist_ok(entry: dict[str, Any], cache: dict[str, Any]) -> None:
    """If entry is OK, write its HF + balances + symbols into the cache."""
    if entry.get("status") != "ok":
        return
    data = entry.get("data")
    if not isinstance(data, dict):
        return
    addr = (data.get("wallet") or "").lower()
    if not addr:
        return
    hf = data.get("health_factor")
    # R-DASH-FIX Bug 3: also cache collateral/debt symbol and balance so the
    # flywheel card can show "kHYPE" / "UETH" even when per-reserve RPC fails.
    coll_sym = data.get("collateral_symbol")
    coll_bal = float(data.get("collateral_balance") or 0.0)
    debt_sym = data.get("debt_symbol")
    debt_bal = float(data.get("debt_balance") or 0.0)

    # R-DASHBOARD-DEBT-SYMBOL: when per-reserve RPC failed, primary_debt may
    # be None → debt_symbol=None even though total_debt_usd > 0. Try to
    # recover via the known-reserve address map first.
    if not debt_sym:
        primary_debt = data.get("primary_debt")
        if isinstance(primary_debt, dict):
            debt_sym = _sym_from_asset(primary_debt.get("asset"))

    # Protect the cache from null-overwrite: if the newly-fetched data still
    # has no symbol (total enumeration failure), preserve the previously-known
    # good value so future UNKNOWN recoveries can still show "UETH" / "USDH".
    existing = cache.get(addr) or {}
    if not debt_sym and existing.get("debt_symbol"):
        debt_sym = existing["debt_symbol"]
        if not debt_bal:
            debt_bal = float(existing.get("debt_balance") or 0.0)

    # Write the resolved values back into the live entry so that downstream
    # consumers (portfolio_snapshot → dashboard) see the correct symbol/balance
    # without requiring another fetch round-trip.
    if debt_sym and not data.get("debt_symbol"):
        data["debt_symbol"] = debt_sym
    if debt_bal and not data.get("debt_balance"):
        data["debt_balance"] = debt_bal
    if not _is_finite_hf(hf):
        # Skip writing infinite HF (it carries no signal for recovery).
        # But preserve a marker so we know the wallet was last seen healthy.
        cache[addr] = {
            "hf": "inf",
            "collateral_usd": float(data.get("total_collateral_usd") or 0.0),
            "debt_usd": float(data.get("total_debt_usd") or 0.0),
            "collateral_symbol": coll_sym,
            "collateral_balance": coll_bal,
            "debt_symbol": debt_sym,
            "debt_balance": debt_bal,
            "ts_epoch": time.time(),
            "ts_utc": _utc_now_iso(),
        }
        return
    cache[addr] = {
        "hf": float(hf),
        "collateral_usd": float(data.get("total_collateral_usd") or 0.0),
        "debt_usd": float(data.get("total_debt_usd") or 0.0),
        "collateral_symbol": coll_sym,
        "collateral_balance": coll_bal,
        "debt_symbol": debt_sym,
        "debt_balance": debt_bal,
        "ts_epoch": time.time(),
        "ts_utc": _utc_now_iso(),
    }


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


async def read_all_with_cache(fetch_fn=None) -> list[dict[str, Any]]:
    """Wrapped fetch_all_hyperlend() with persisted last-known cache.

    Parameters
    ----------
    fetch_fn :
        Async callable returning the same shape as
        ``modules.hyperlend.fetch_all_hyperlend``. Defaults to the
        production import; injectable for tests.
    """
    if not ENABLED:
        # Kill switch — return raw fetch unchanged.
        if fetch_fn is None:
            from modules.hyperlend import fetch_all_hyperlend  # type: ignore

            fetch_fn = fetch_all_hyperlend
        try:
            raw = await fetch_fn()
        except Exception:  # noqa: BLE001
            log.exception("hyperlend_reader (disabled): underlying fetch failed")
            return []
        for e in raw or []:
            e["hf_status"] = _classify_entry(e)
        return raw or []

    if fetch_fn is None:
        from modules.hyperlend import fetch_all_hyperlend  # type: ignore

        fetch_fn = fetch_all_hyperlend

    cache = _load_cache()

    # Outer retry loop on the whole fetch (above the legacy 3× retry inside).
    last_exc: Exception | None = None
    raw: list[dict[str, Any]] = []
    for attempt in range(RETRY_MAX):
        try:
            raw = await fetch_fn() or []
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log.warning(
                "hyperlend_reader: fetch attempt %d/%d failed: %s",
                attempt + 1,
                RETRY_MAX,
                exc,
            )
            if attempt < RETRY_MAX - 1:
                await asyncio.sleep(RETRY_BASE_SEC * (attempt + 1))
    if not raw and last_exc is not None:
        # Total failure — emit purely-cached entries so callers can render
        # the last-known state instead of "HF ∞".
        return _entries_from_cache_only(cache)

    out: list[dict[str, Any]] = []
    has_any_real_data = False
    for entry in raw:
        cls = _classify_entry(entry)
        if cls == "OK":
            _persist_ok(entry, cache)
            entry["hf_status"] = "OK"
            out.append(entry)
            has_any_real_data = True
        elif cls == "ZERO":
            # Empty wallet — could legitimately be empty (e.g. 0xa44e was
            # empty for a while), but if cache shows it had non-zero
            # collateral recently, treat as UNKNOWN (don't believe a
            # transient "everything went to 0" frame).
            data = entry.get("data") or {}
            addr = (data.get("wallet") or "").lower()
            cached = cache.get(addr) if addr else None
            cached_collat = (
                float(cached.get("collateral_usd") or 0.0) if cached else 0.0
            )
            if cached_collat > 1.0:
                # Very recently it had real collateral → suspicious zero.
                # Recover from cache.
                out.append(_maybe_recover_from_cache(entry, cache))
            else:
                entry["hf_status"] = "ZERO"
                out.append(entry)
        else:
            out.append(_maybe_recover_from_cache(entry, cache))

    if has_any_real_data:
        _save_cache(cache)

    # If the legacy fetch returned the synthetic empty placeholder, override
    # with cached entries (any non-empty cache wins).
    if not has_any_real_data and cache:
        return _entries_from_cache_only(cache)
    return out


def _entries_from_cache_only(cache: dict[str, Any]) -> list[dict[str, Any]]:
    """Synthesise UNKNOWN entries from the cache when live fetch failed."""
    out: list[dict[str, Any]] = []
    for addr, c in cache.items():
        try:
            age = max(0, int(time.time() - float(c.get("ts_epoch") or 0)))
        except Exception:  # noqa: BLE001
            age = None
        last_hf = c.get("hf")
        # Coerce 'inf' string → infinity sentinel only when no debt
        if last_hf == "inf":
            last_hf_num: Any = "inf"
        else:
            try:
                last_hf_num = float(last_hf)
            except Exception:  # noqa: BLE001
                last_hf_num = None
        out.append(
            {
                "status": "ok",
                "hf_status": "UNKNOWN",
                "label": addr[:10],
                "data": {
                    "wallet": addr,
                    "label": addr[:10],
                    "total_collateral_usd": float(c.get("collateral_usd") or 0.0),
                    "total_debt_usd": float(c.get("debt_usd") or 0.0),
                    "available_borrows_usd": 0.0,
                    "current_liquidation_threshold": 0.0,
                    "ltv": 0.0,
                    "health_factor": float("nan"),
                    "last_known_hf": last_hf_num,
                    "last_known_at_iso": c.get("ts_utc"),
                    "last_known_collateral_usd": float(
                        c.get("collateral_usd") or 0.0
                    ),
                    "last_known_debt_usd": float(c.get("debt_usd") or 0.0),
                    "age_seconds": age,
                    "recovered_from_cache": True,
                    "collateral_assets": [],
                    "debt_assets": [],
                    "primary_collateral": None,
                    "primary_debt": None,
                    # R-DASH-FIX Bug 3: restore cached symbols so flywheel shows
                    # "kHYPE / UETH" when live per-reserve fetch failed completely.
                    "collateral_symbol": c.get("collateral_symbol"),
                    "collateral_balance": float(c.get("collateral_balance") or 0.0),
                    "debt_symbol": c.get("debt_symbol"),
                    "debt_balance": float(c.get("debt_balance") or 0.0),
                },
            }
        )
    return out


def format_hf_line(entry: dict[str, Any]) -> str:
    """Render a single line for /flywheel /reporte /risk_check.

    Examples:
      "HyperLend Principal — HF 1.214 (collateral $4,018 / debt $881)"
      "HyperLend Principal — ⚠️ rate-limited, last known HF 1.214 hace 23min"
      "HyperLend Principal — sin posiciones (collateral=0, debt=0)"
    """
    data = entry.get("data") or {}
    label = data.get("label") or entry.get("label") or "wallet"
    cls = entry.get("hf_status") or _classify_entry(entry)
    if cls == "OK":
        hf = data.get("health_factor")
        coll = data.get("total_collateral_usd") or 0.0
        debt = data.get("total_debt_usd") or 0.0
        if hf == float("inf") or hf is None:
            return f"{label} — HF ∞ (collateral ${coll:,.0f} / debt $0)"
        return (
            f"{label} — HF {float(hf):.3f} (collateral ${coll:,.0f} / "
            f"debt ${debt:,.0f})"
        )
    if cls == "ZERO":
        return f"{label} — no positions (collateral=0, debt=0)"
    # UNKNOWN
    last_hf = data.get("last_known_hf")
    last_age = data.get("age_seconds")
    if last_hf is None:
        return f"⚠️ {label} — RPC rate-limited (no prior read)"
    if isinstance(last_hf, str):
        # 'inf' marker
        return f"⚠️ {label} — RPC rate-limited (last HF ∞ {_age_label(last_age)} ago)"
    return (
        f"⚠️ {label} — RPC rate-limited "
        f"(last HF {float(last_hf):.3f} {_age_label(last_age)} ago)"
    )


def _age_label(age_seconds: int | None) -> str:
    if age_seconds is None:
        return "?"
    if age_seconds < 60:
        return f"{age_seconds}s"
    if age_seconds < 3600:
        return f"{age_seconds // 60}min"
    return f"{age_seconds // 3600}h"
