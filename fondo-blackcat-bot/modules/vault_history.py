"""R-VAULTDEP evolution — lightweight daily SQLite snapshots of vault equity.

So BCD can watch the HL vault deposit grow over the rebote, we persist one
row per (vault, UTC day) and surface a compact evolution line in ``/reporte``
and on the ``/dashboard``::

    HyperGrowth: $5,073 (+$73 / +1.47% all-time | +$12 vs ayer)

Design contract
---------------
* **Lightweight.** One row per ``(vault_address, snap_date)``. Repeated writes
  the same UTC day ``INSERT OR REPLACE`` (keep the latest equity of the day),
  so the table grows by at most one row per vault per day regardless of how
  often ``/reporte`` / the dashboard / the scheduler run.
* **"vs previous" compares to a PRIOR day**, never to an earlier write of the
  same day — same-day replacements never pollute the delta.
* **Never raises.** Every public function swallows persistence/IO errors and
  degrades to "no evolution data" (the all-time vs cost-basis line still
  renders from live values). A broken DB must never crash a report.
* Keyless / local only: a SQLite file under ``DATA_DIR`` (the Railway
  ``/app/data`` volume). No browser storage, no network, no secrets.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

try:
    from config import DATA_DIR
except Exception:  # noqa: BLE001 — stay importable in isolated tests
    DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
    os.makedirs(DATA_DIR, exist_ok=True)

log = logging.getLogger(__name__)

DB_PATH = os.path.join(DATA_DIR, "vault_history.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vault_snapshots (
    vault_address    TEXT NOT NULL,
    snap_date        TEXT NOT NULL,   -- YYYY-MM-DD (UTC)
    label            TEXT,
    equity_usd       REAL NOT NULL,
    cost_basis_usd   REAL NOT NULL,
    pnl_vs_cost_usd  REAL NOT NULL,
    ts_iso           TEXT NOT NULL,   -- full UTC timestamp of last write that day
    PRIMARY KEY (vault_address, snap_date)
);
"""


def _conn(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    return conn


def _utc_today(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _safe_float(v: Any) -> float:
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def record_vault_snapshot(
    deposits: Iterable[Any],
    *,
    now: datetime | None = None,
    db_path: str | None = None,
) -> int:
    """Persist today's equity for each *found* deposit. NEVER raises.

    Accepts any objects exposing ``vault_address``, ``label``, ``equity_usd``,
    ``cost_basis_usd``, ``pnl_usd`` and ``found`` (e.g. ``VaultDeposit``).
    Same-day writes dedupe (INSERT OR REPLACE on ``(vault_address, snap_date)``).
    Returns the number of rows written (0 on any failure).
    """
    today = _utc_today(now)
    ts_iso = (now or datetime.now(timezone.utc)).astimezone(
        timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    written = 0
    try:
        with _conn(db_path) as conn:
            for d in deposits or []:
                if not getattr(d, "found", False):
                    continue
                va = str(getattr(d, "vault_address", "")).lower()
                if not va:
                    continue
                equity = _safe_float(getattr(d, "equity_usd", 0.0))
                if equity <= 0.0:
                    continue
                cost = _safe_float(getattr(d, "cost_basis_usd", 0.0))
                pnl = _safe_float(
                    getattr(d, "pnl_usd", equity - cost)
                )
                label = str(getattr(d, "label", "") or "Vault deposit")
                conn.execute(
                    "INSERT OR REPLACE INTO vault_snapshots "
                    "(vault_address, snap_date, label, equity_usd, "
                    " cost_basis_usd, pnl_vs_cost_usd, ts_iso) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (va, today, label, equity, cost, pnl, ts_iso),
                )
                written += 1
            conn.commit()
    except Exception as e:  # noqa: BLE001 — robustness contract
        log.warning("vault_history.record_vault_snapshot failed: %s", e)
        return 0
    return written


def get_previous_snapshot(
    vault_address: str,
    *,
    now: datetime | None = None,
    db_path: str | None = None,
) -> dict | None:
    """Most recent snapshot for ``vault_address`` from a PRIOR day (< today).

    Returns a dict (``snap_date``, ``equity_usd``, ``cost_basis_usd``, …) or
    ``None`` if no earlier-day snapshot exists. NEVER raises.
    """
    today = _utc_today(now)
    va = str(vault_address or "").lower()
    if not va:
        return None
    try:
        with _conn(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM vault_snapshots "
                "WHERE vault_address = ? AND snap_date < ? "
                "ORDER BY snap_date DESC LIMIT 1",
                (va, today),
            ).fetchone()
            return dict(row) if row is not None else None
    except Exception as e:  # noqa: BLE001
        log.warning("vault_history.get_previous_snapshot failed: %s", e)
        return None


def _fmt_usd(v: float) -> str:
    return f"${v:,.0f}"


def _fmt_signed(v: float) -> str:
    if v > 0:
        return f"+${v:,.0f}"
    if v < 0:
        return f"-${abs(v):,.0f}"
    return "$0"


def _fmt_signed_pct(v: float) -> str:
    if v > 0:
        return f"+{v:.2f}%"
    if v < 0:
        return f"{v:.2f}%"
    return "0.00%"


def _prev_day_label(prev_date: str, today: str) -> str:
    """'ayer' when the prior snapshot is exactly one UTC day before today,
    otherwise the ISO date (e.g. 'vs 2026-05-28')."""
    try:
        t = date.fromisoformat(today)
        p = date.fromisoformat(prev_date)
        if p == t - timedelta(days=1):
            return "ayer"
        return prev_date
    except (ValueError, TypeError):
        return prev_date or "snapshot previo"


def compute_vault_evolution(
    deposit: Any,
    *,
    now: datetime | None = None,
    db_path: str | None = None,
) -> dict:
    """Return evolution metrics for one deposit (all-time + vs prior snapshot).

    Reads the prior-day snapshot for the delta. Does NOT write. NEVER raises.
    Keys: label, equity_usd, cost_basis_usd, pnl_all_usd, pnl_all_pct,
    has_prev (bool), prev_date, prev_label, delta_prev_usd, delta_prev_pct.
    """
    equity = _safe_float(getattr(deposit, "equity_usd", 0.0))
    cost = _safe_float(getattr(deposit, "cost_basis_usd", 0.0))
    pnl_all = _safe_float(getattr(deposit, "pnl_usd", equity - cost))
    pnl_all_pct = (pnl_all / cost * 100.0) if cost > 0 else 0.0
    out: dict[str, Any] = {
        "label": str(getattr(deposit, "label", "") or "Vault deposit"),
        "equity_usd": equity,
        "cost_basis_usd": cost,
        "pnl_all_usd": pnl_all,
        "pnl_all_pct": pnl_all_pct,
        "has_prev": False,
        "prev_date": None,
        "prev_label": None,
        "delta_prev_usd": 0.0,
        "delta_prev_pct": 0.0,
    }
    today = _utc_today(now)
    prev = get_previous_snapshot(
        str(getattr(deposit, "vault_address", "")), now=now, db_path=db_path
    )
    if prev is not None:
        prev_eq = _safe_float(prev.get("equity_usd"))
        if prev_eq > 0:
            out["has_prev"] = True
            out["prev_date"] = prev.get("snap_date")
            out["prev_label"] = _prev_day_label(str(prev.get("snap_date")), today)
            out["delta_prev_usd"] = equity - prev_eq
            out["delta_prev_pct"] = (equity - prev_eq) / prev_eq * 100.0
    return out


def format_vault_evolution_line(
    deposit: Any,
    *,
    now: datetime | None = None,
    db_path: str | None = None,
) -> str:
    """Compact one-line evolution string for one deposit. NEVER raises.

    With a prior snapshot::
        📈 HyperGrowth: $5,073 (+$73 / +1.47% all-time | +$12 vs ayer)
    Without one (first run)::
        📈 HyperGrowth: $5,073 (+$73 / +1.47% all-time)
    """
    try:
        ev = compute_vault_evolution(deposit, now=now, db_path=db_path)
    except Exception as e:  # noqa: BLE001
        log.warning("format_vault_evolution_line failed: %s", e)
        return ""
    base = (
        f"📈 {ev['label']}: {_fmt_usd(ev['equity_usd'])} "
        f"({_fmt_signed(ev['pnl_all_usd'])} / "
        f"{_fmt_signed_pct(ev['pnl_all_pct'])} all-time"
    )
    if ev["has_prev"]:
        base += (
            f" | {_fmt_signed(ev['delta_prev_usd'])} vs {ev['prev_label']}"
        )
    return base + ")"


def format_vault_evolution_block(
    result: Any,
    *,
    record: bool = True,
    now: datetime | None = None,
    db_path: str | None = None,
) -> str:
    """Telegram evolution block for all found deposits in a result.

    When ``record`` is True (default) it first persists today's snapshot
    (so tomorrow has a baseline), then renders the evolution lines using the
    PRIOR-day snapshot for each vault. Returns "" when nothing to show.
    NEVER raises.
    """
    try:
        deposits = list(getattr(result, "deposits", []) or [])
    except Exception:  # noqa: BLE001
        return ""
    found = [d for d in deposits if getattr(d, "found", False)
             and _safe_float(getattr(d, "equity_usd", 0.0)) > 0]
    if not found:
        return ""
    lines: list[str] = []
    for d in found:
        # Compute evolution FIRST (uses prior-day snapshot), then record so the
        # same-day write doesn't overwrite the baseline we just read.
        line = format_vault_evolution_line(d, now=now, db_path=db_path)
        if line:
            lines.append(line)
    if record:
        record_vault_snapshot(found, now=now, db_path=db_path)
    if not lines:
        return ""
    header = "📈 EVOLUCIÓN VAULT (equity vs costo vs último snapshot)"
    return "\n".join([header, *lines])
