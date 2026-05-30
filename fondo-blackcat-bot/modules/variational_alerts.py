"""R-VARIATIONAL — persistent mean-reversion watches ("Farm the DUMP").

The user registers a ticker with /variationalalerts <TICKER>. We snapshot its
CURRENT annualized funding as the *baseline* and persist it (SQLite, in
DATA_DIR — survives Railway restarts). A scheduler job re-reads each active
watch's funding every ~30 min and fires ONE alert when funding has reverted to
roughly half (or less negative than half) of the baseline:

    current_funding >= funding_at_registration * VARIATIONAL_REVERSION_FRACTION

Both numbers are negative, so half-of-baseline is the *less negative* value
(e.g. baseline -600% → trigger at ≥ -300%). After firing, the watch is marked
``triggered`` so it never spams again.

Schema is created lazily and is idempotent. All writes are wrapped so a DB
hiccup can never crash the bot's scheduler.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from config import DATA_DIR

log = logging.getLogger(__name__)

DB_PATH = os.path.join(DATA_DIR, "intel_memory.db")


def reversion_fraction() -> float:
    """Fraction of the baseline at which the reversion alert fires (default 0.5)."""
    raw = os.getenv("VARIATIONAL_REVERSION_FRACTION", "0.5").strip()
    try:
        f = float(raw)
    except ValueError:
        log.warning("bad VARIATIONAL_REVERSION_FRACTION=%r → 0.5", raw)
        return 0.5
    if f <= 0:
        log.warning("VARIATIONAL_REVERSION_FRACTION=%r ≤ 0 → 0.5", raw)
        return 0.5
    return f


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class VariationalWatch:
    ticker: str
    baseline_funding: float          # annualized %, at registration
    registered_at: str               # ISO utc
    triggered: bool
    triggered_at: Optional[str]
    current_funding: Optional[float]  # last observed annualized %
    last_checked: Optional[str]


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS variational_alerts (
            ticker            TEXT PRIMARY KEY,
            baseline_funding  REAL NOT NULL,
            registered_at     TEXT NOT NULL,
            triggered         INTEGER NOT NULL DEFAULT 0,
            triggered_at      TEXT,
            current_funding   REAL,
            last_checked      TEXT
        )
        """
    )
    return conn


def _row_to_watch(r: sqlite3.Row) -> VariationalWatch:
    return VariationalWatch(
        ticker=r["ticker"],
        baseline_funding=r["baseline_funding"],
        registered_at=r["registered_at"],
        triggered=bool(r["triggered"]),
        triggered_at=r["triggered_at"],
        current_funding=r["current_funding"],
        last_checked=r["last_checked"],
    )


def register(ticker: str, baseline_funding: float) -> VariationalWatch:
    """Create (or RESET) a watch for ``ticker`` with the given baseline.

    Re-registering an existing ticker resets its baseline + clears the
    triggered flag, so the user can re-arm after a fire.
    """
    t = (ticker or "").strip().upper()
    if not t:
        raise ValueError("empty ticker")
    now = _now_iso()
    conn = _get_conn()
    try:
        conn.execute(
            """
            INSERT INTO variational_alerts
                (ticker, baseline_funding, registered_at, triggered,
                 triggered_at, current_funding, last_checked)
            VALUES (?, ?, ?, 0, NULL, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                baseline_funding = excluded.baseline_funding,
                registered_at    = excluded.registered_at,
                triggered        = 0,
                triggered_at     = NULL,
                current_funding  = excluded.current_funding,
                last_checked     = excluded.last_checked
            """,
            (t, float(baseline_funding), now, float(baseline_funding), now),
        )
        conn.commit()
    finally:
        conn.close()
    return VariationalWatch(t, float(baseline_funding), now, False, None,
                            float(baseline_funding), now)


def list_watches(include_triggered: bool = True) -> list[VariationalWatch]:
    conn = _get_conn()
    try:
        if include_triggered:
            rows = conn.execute(
                "SELECT * FROM variational_alerts ORDER BY registered_at ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM variational_alerts WHERE triggered = 0 "
                "ORDER BY registered_at ASC"
            ).fetchall()
    finally:
        conn.close()
    return [_row_to_watch(r) for r in rows]


def get_watch(ticker: str) -> Optional[VariationalWatch]:
    t = (ticker or "").strip().upper()
    if not t:
        return None
    conn = _get_conn()
    try:
        r = conn.execute(
            "SELECT * FROM variational_alerts WHERE ticker = ?", (t,)
        ).fetchone()
    finally:
        conn.close()
    return _row_to_watch(r) if r else None


def remove(ticker: str) -> bool:
    t = (ticker or "").strip().upper()
    if not t:
        return False
    conn = _get_conn()
    try:
        cur = conn.execute("DELETE FROM variational_alerts WHERE ticker = ?", (t,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def clear() -> int:
    conn = _get_conn()
    try:
        cur = conn.execute("DELETE FROM variational_alerts")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def update_current(ticker: str, current_funding: float) -> None:
    """Persist the latest observed funding + check timestamp (no trigger)."""
    t = (ticker or "").strip().upper()
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE variational_alerts SET current_funding = ?, last_checked = ? "
            "WHERE ticker = ?",
            (float(current_funding), _now_iso(), t),
        )
        conn.commit()
    finally:
        conn.close()


def mark_triggered(ticker: str, current_funding: float) -> None:
    t = (ticker or "").strip().upper()
    now = _now_iso()
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE variational_alerts SET triggered = 1, triggered_at = ?, "
            "current_funding = ?, last_checked = ? WHERE ticker = ?",
            (now, float(current_funding), now, t),
        )
        conn.commit()
    finally:
        conn.close()


# ─── Trigger logic (pure, unit-tested) ───────────────────────────────────────
def reversion_target(baseline_funding: float, fraction: float) -> float:
    """The funding level at which the watch fires (= baseline × fraction)."""
    return baseline_funding * fraction


def has_reverted(baseline_funding: float, current_funding: float, fraction: float) -> bool:
    """True when ``current_funding`` has reverted to ≥ baseline×fraction.

    Baseline is negative (extreme dump). Reversion = funding becoming *less
    negative*, so we fire when current ≥ baseline×fraction. Guards against a
    non-negative or zero baseline (no meaningful reversion target).
    """
    if baseline_funding >= 0:
        return False
    return current_funding >= reversion_target(baseline_funding, fraction)


def pct_reverted(baseline_funding: float, current_funding: float) -> Optional[float]:
    """How far funding has travelled from baseline toward 0, in percent.

    0% = still at baseline, 100% = fully reverted to 0. None if baseline is 0.
    """
    if baseline_funding == 0:
        return None
    return (1.0 - (current_funding / baseline_funding)) * 100.0


# ─── Formatting ──────────────────────────────────────────────────────────────
def _fmt_pct(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    return f"{v:,.1f}%"


def format_watch_list(watches: list[VariationalWatch], fraction: float) -> str:
    """Render /variationalalerts list."""
    lines = ["🐱‍⬛ VARIATIONAL — Watches (Farm the DUMP)"]
    if not watches:
        lines.append("")
        lines.append("No hay watches activos.")
        lines.append("Registrá uno: /variationalalerts <TICKER>")
        return "\n".join(lines)
    lines.append(f"Reversión target = baseline × {fraction:g}")
    lines.append("")
    for w in watches:
        target = reversion_target(w.baseline_funding, fraction)
        status = "✅ DISPARADO" if w.triggered else "👁 vigilando"
        cur = _fmt_pct(w.current_funding)
        lines.append(f"• {w.ticker} — {status}")
        lines.append(
            f"   baseline {_fmt_pct(w.baseline_funding)}  →  target ≥ {_fmt_pct(target)}"
        )
        lines.append(f"   actual {cur}" + (f"  (disparó {w.triggered_at[:16]})" if w.triggered and w.triggered_at else ""))
    return "\n".join(lines)


def format_reversion_alert(
    watch: VariationalWatch,
    current_funding: float,
    fraction: float,
    mark_price: Optional[float] = None,
) -> str:
    """Render the fired mean-reversion alert message."""
    reverted = pct_reverted(watch.baseline_funding, current_funding)
    reverted_str = f"{reverted:,.0f}%" if reverted is not None else "n/a"
    target = reversion_target(watch.baseline_funding, fraction)
    mp = "n/a"
    if mark_price is not None:
        mp = f"${mark_price:,.4f}".rstrip("0").rstrip(".") if mark_price >= 1 else f"${mark_price:.8f}".rstrip("0").rstrip(".")
    return "\n".join([
        f"🔔 VARIATIONAL REVERSION HIT — {watch.ticker}",
        "",
        f"Baseline (registro): {_fmt_pct(watch.baseline_funding)} anual",
        f"Target (×{fraction:g}): ≥ {_fmt_pct(target)}",
        f"Funding actual: {_fmt_pct(current_funding)}",
        f"Revertido: {reverted_str} del camino a 0",
        f"Mark price: {mp}",
        "",
        "REVERSION HIT — Farm the DUMP short setup per your rule.",
        "Apply your 5 checks before entering.",
    ])
