"""Round 18.3.2 — Performance attribution (alpha vs beta).

Given a closed basket event with absolute return %, decompose into:
  beta_component  = market_beta * btc_return_over_period
  alpha_component = total_return - beta_component
  market_beta     = covariance heuristic (default 1.0 for short_alts vs BTC inverse)

Stores each attribution in ``DATA_DIR/perf_attribution.db`` for trend study.

Kill switch: ``PERF_ATTRIBUTION_ENABLED=false``.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

from config import DATA_DIR

log = logging.getLogger(__name__)

DB_PATH = os.path.join(DATA_DIR, "perf_attribution.db")


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute(
        """CREATE TABLE IF NOT EXISTS perf_attribution (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            label TEXT,
            entry_value REAL,
            exit_value REAL,
            total_return_pct REAL,
            btc_entry REAL,
            btc_exit REAL,
            btc_return_pct REAL,
            beta REAL,
            beta_component_pct REAL,
            alpha_component_pct REAL,
            notes TEXT
        )"""
    )
    return c


def _is_enabled() -> bool:
    return os.getenv("PERF_ATTRIBUTION_ENABLED", "true").strip().lower() != "false"


def _get_default_beta(label: str | None) -> float:
    """Heuristic default beta. Short_alts basket ≈ -1 beta to BTC; long perps ≈ +1."""
    raw = os.getenv("PERF_ATTRIBUTION_DEFAULT_BETA", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    if not label:
        return 1.0
    L = label.lower()
    if "short" in L or "basket" in L or "alts" in L:
        return -1.0
    return 1.0


def compute_attribution(
    *,
    label: str | None,
    entry_value: float,
    exit_value: float,
    btc_entry: float | None,
    btc_exit: float | None,
    beta: float | None = None,
) -> dict[str, Any]:
    """Pure compute. Returns dict with keys total/beta/alpha (% terms)."""
    if entry_value <= 0:
        return {"ok": False, "error": "entry_value <= 0"}
    total_pct = (exit_value - entry_value) / entry_value * 100.0
    btc_pct: float | None = None
    if btc_entry and btc_exit and btc_entry > 0:
        btc_pct = (btc_exit - btc_entry) / btc_entry * 100.0
    b = beta if beta is not None else _get_default_beta(label)
    if btc_pct is None:
        return {
            "ok": True, "label": label, "total_return_pct": total_pct,
            "btc_return_pct": None, "beta": b, "beta_component_pct": None,
            "alpha_component_pct": total_pct, "notes": "no BTC reference; treating all as alpha",
        }
    beta_component = b * btc_pct
    alpha_component = total_pct - beta_component
    return {
        "ok": True, "label": label, "total_return_pct": total_pct,
        "btc_return_pct": btc_pct, "beta": b,
        "beta_component_pct": beta_component, "alpha_component_pct": alpha_component,
        "notes": "",
    }


def persist(attr: dict[str, Any], entry_value: float, exit_value: float,
            btc_entry: float | None, btc_exit: float | None) -> None:
    if not attr.get("ok"):
        return
    try:
        c = _conn()
        c.execute(
            "INSERT INTO perf_attribution(ts_utc, label, entry_value, exit_value, "
            "total_return_pct, btc_entry, btc_exit, btc_return_pct, beta, "
            "beta_component_pct, alpha_component_pct, notes) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                datetime.now(timezone.utc).isoformat(),
                attr.get("label"),
                float(entry_value), float(exit_value),
                float(attr["total_return_pct"]),
                btc_entry, btc_exit, attr.get("btc_return_pct"),
                float(attr["beta"]),
                attr.get("beta_component_pct"), attr.get("alpha_component_pct"),
                attr.get("notes") or "",
            ),
        )
        c.commit()
        c.close()
    except Exception:  # noqa: BLE001
        log.exception("perf_attribution: persist failed")


def format_attribution(attr: dict[str, Any]) -> str:
    if not attr.get("ok"):
        return f"⚠️ Attribution skipped: {attr.get('error')}"
    lines = [
        f"📊 ATTRIBUTION — {attr.get('label') or 'unknown'}",
        f"  Total return: {attr['total_return_pct']:+.2f}%",
    ]
    if attr.get("btc_return_pct") is not None:
        lines.append(f"  BTC return:   {attr['btc_return_pct']:+.2f}%")
        lines.append(f"  Beta usado:   {attr['beta']:+.2f}")
        lines.append(f"  └ Beta component:  {attr['beta_component_pct']:+.2f}%")
        lines.append(f"  └ Alpha component: {attr['alpha_component_pct']:+.2f}%")
    else:
        lines.append("  (Sin BTC ref — todo tratado como alpha)")
    if attr.get("notes"):
        lines.append(f"  Nota: {attr['notes']}")
    return "\n".join(lines)


async def attribute_basket_close(
    *, label: str, entry_value: float, exit_value: float,
    btc_entry: float | None = None, btc_exit: float | None = None,
    beta: float | None = None,
) -> str | None:
    """High-level entry: compute + persist + return formatted block (or None if disabled)."""
    if not _is_enabled():
        return None
    attr = compute_attribution(
        label=label, entry_value=entry_value, exit_value=exit_value,
        btc_entry=btc_entry, btc_exit=btc_exit, beta=beta,
    )
    persist(attr, entry_value, exit_value, btc_entry, btc_exit)
    return format_attribution(attr)


def recent_attributions(limit: int = 5) -> list[dict[str, Any]]:
    try:
        c = _conn()
        rows = c.execute(
            "SELECT ts_utc, label, total_return_pct, btc_return_pct, beta, "
            "beta_component_pct, alpha_component_pct "
            "FROM perf_attribution ORDER BY ts_utc DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        c.close()
        return [
            {"ts_utc": r[0], "label": r[1], "total_return_pct": r[2],
             "btc_return_pct": r[3], "beta": r[4],
             "beta_component_pct": r[5], "alpha_component_pct": r[6]}
            for r in rows
        ]
    except Exception:  # noqa: BLE001
        log.exception("perf_attribution: recent fetch failed")
        return []
