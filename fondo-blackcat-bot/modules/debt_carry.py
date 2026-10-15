"""R-RONDA-OCT #1a — carry of the PM USDC debt: live APR + accrued interest.

Why it exists: the PM debt moved 92.9K → 94.2K in 22 days and the bot could
not say why. Two causes have to be told apart — interest and personal
withdrawals (``modules.retiros_personales``). This module owns the interest
half:

  * **APR, never assumed.** HL publishes ``borrowYearlyRate`` per reserve via
    ``borrowLendReserveState`` + ``token``; USDC is token index 0 (verified
    2026-10-15: ``borrowYearlyRate "0.05"``, the 5% BCD reads in the Earn
    app). The number is cached 5 min with provenance (``HL live`` /
    ``HL cache Nmin`` / ``CONFIG — HL no leido (<error>)``). When HL cannot be
    read the panel prints the CONFIG fallback NEXT TO ITS LABEL and the
    ``debt_carry`` subsystem is marked degraded — a default is never printed
    as a measurement (lesson of the 422 that hid for weeks).
  * **Accrual, persisted.** Every observation of the debt (``observe``) adds
    ``debt × APR × Δt/365d`` to a running total in ``intel_memory.db``
    (``debt_carry_accrual``, single row). Marks (``debt_carry_marks``) store
    the total at the last /reporte and at a user-set cursor, so the panel can
    print "desde el ultimo reporte" and "desde el cursor" without ever
    re-deriving from prices. HL compounds continuously; this is a simple
    accrual on the observed balance and is labelled ``≈``.

Money path: every ``except`` either raises or calls ``health_registry.swallowed``.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from config import DATA_DIR
from modules import health_registry

log = logging.getLogger(__name__)

DB_PATH = os.path.join(DATA_DIR, "intel_memory.db")

USDC_TOKEN_INDEX = 0
SUBSYSTEM = "debt_carry"
_APR_TTL_SEC = float(os.getenv("DEBT_CARRY_APR_TTL_SEC", "300") or 300)
# Only a FALLBACK for the panel when HL is unreadable; always printed with the
# CONFIG label and never used silently.
_APR_FALLBACK = float(os.getenv("DEBT_CARRY_APR_FALLBACK", "0.05") or 0.05)
# Minimum gap between two accrual ticks (observe() is called from every PM
# read; accruing every few seconds is pointless churn on the DB).
_MIN_TICK_SEC = float(os.getenv("DEBT_CARRY_MIN_TICK_SEC", "300") or 300)
# A gap longer than this between observations (bot down) is still accrued on
# the current balance, but flagged in the snapshot so the reader knows.
_GAP_FLAG_SEC = 48 * 3600.0
_YEAR_SEC = 365.0 * 86400.0

MARK_LAST_REPORT = "last_report"
MARK_CURSOR = "cursor"

_apr_cache: dict[str, Any] = {"ts": 0.0, "apr": None, "source": "none", "error": None}


# ─── storage ────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10.0)
    con.row_factory = sqlite3.Row
    con.execute("""
        CREATE TABLE IF NOT EXISTS debt_carry_accrual (
            id            INTEGER PRIMARY KEY CHECK (id = 1),
            accrued_total REAL    NOT NULL DEFAULT 0,
            last_tick_ms  INTEGER NOT NULL DEFAULT 0,
            last_debt     REAL,
            last_apr      REAL,
            apr_source    TEXT,
            ticks         INTEGER NOT NULL DEFAULT 0,
            updated_at    TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS debt_carry_marks (
            name          TEXT PRIMARY KEY,
            accrued_total REAL    NOT NULL,
            debt_at_mark  REAL,
            ts_ms         INTEGER NOT NULL,
            updated_at    TEXT
        )
    """)
    con.commit()
    return con


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── APR ────────────────────────────────────────────────────────────────────

def _read_rate(state: dict[str, Any] | None) -> float | None:
    if not isinstance(state, dict):
        return None
    for k in ("borrowYearlyRate", "borrow_yearly_rate", "borrowApr", "borrowRate"):
        if k in state:
            try:
                v = float(state[k])
            except (TypeError, ValueError):
                return None
            # HL publishes a fraction ("0.05"); guard against a percent form.
            if v > 1.5:
                v = v / 100.0
            return v if 0.0 <= v <= 1.5 else None
    return None


def get_usdc_borrow_apr(force: bool = False) -> dict[str, Any]:
    """``{"apr", "source", "age_s", "error"}``. NEVER raises.

    ``source`` ∈ {"hl_live", "hl_cache", "config"}. ``apr`` is always a
    number: the live rate, the cached rate, or the CONFIG fallback — and the
    source says which, so the renderer can label it.
    """
    now = time.time()
    if (not force and _apr_cache["apr"] is not None
            and (now - _apr_cache["ts"]) < _APR_TTL_SEC):
        return {"apr": _apr_cache["apr"], "source": "hl_live",
                "age_s": now - _apr_cache["ts"], "error": None}
    try:
        from modules.hl_borrow_lend import get_reserve_state
        state = get_reserve_state(USDC_TOKEN_INDEX)
        apr = _read_rate(state)
        if apr is None:
            raise ValueError(
                f"borrowLendReserveState(token={USDC_TOKEN_INDEX}) sin borrowYearlyRate "
                f"(claves={sorted(state) if isinstance(state, dict) else type(state).__name__})")
        _apr_cache.update(ts=now, apr=apr, source="hl_live", error=None)
        return {"apr": apr, "source": "hl_live", "age_s": 0.0, "error": None}
    except Exception as e:  # noqa: BLE001 — robustness contract
        err = f"{type(e).__name__}: {e}"[:160]
        log.warning("debt_carry.get_usdc_borrow_apr failed: %s", err)
        health_registry.swallowed(SUBSYSTEM, f"get_usdc_borrow_apr: {err[:120]}")
        _apr_cache["error"] = err
        if _apr_cache["apr"] is not None:
            return {"apr": _apr_cache["apr"], "source": "hl_cache",
                    "age_s": now - _apr_cache["ts"], "error": err}
        return {"apr": _APR_FALLBACK, "source": "config", "age_s": None, "error": err}


def apr_source_label(info: dict[str, Any]) -> str:
    src = info.get("source")
    if src == "hl_live":
        return "HL live"
    if src == "hl_cache":
        age = info.get("age_s") or 0.0
        return f"HL cache {int(age // 60)}min"
    return f"CONFIG — HL no leido ({info.get('error') or '?'})"


# ─── accrual ────────────────────────────────────────────────────────────────

def _load_accrual(con: sqlite3.Connection) -> dict[str, Any]:
    row = con.execute("SELECT * FROM debt_carry_accrual WHERE id=1").fetchone()
    if row is None:
        return {"accrued_total": 0.0, "last_tick_ms": 0, "last_debt": None,
                "last_apr": None, "apr_source": None, "ticks": 0}
    return dict(row)


def observe(debt_usd: float | None, *, apr_info: dict[str, Any] | None = None,
            now_ms: int | None = None, force: bool = False) -> dict[str, Any] | None:
    """Accrue interest since the previous tick on the OBSERVED debt and
    persist. Returns the accrual row (dict) or None when nothing was done /
    on failure. NEVER raises. Called from every primary PM read.
    """
    try:
        d = float(debt_usd) if debt_usd is not None else None
    except (TypeError, ValueError):
        d = None
    if d is None or d < 0:
        return None
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    try:
        info = apr_info or get_usdc_borrow_apr()
        apr = float(info["apr"])
        con = _conn()
        try:
            acc = _load_accrual(con)
            last = int(acc.get("last_tick_ms") or 0)
            if last > 0 and not force and (now_ms - last) < _MIN_TICK_SEC * 1000:
                return acc
            delta = 0.0
            if last > 0 and now_ms > last:
                # Interest on the balance seen at the PREVIOUS tick over the
                # elapsed time; the rate used is the one just read.
                base = float(acc.get("last_debt") if acc.get("last_debt") is not None else d)
                delta = base * apr * ((now_ms - last) / 1000.0) / _YEAR_SEC
            total = float(acc.get("accrued_total") or 0.0) + delta
            con.execute(
                "INSERT INTO debt_carry_accrual (id, accrued_total, last_tick_ms, "
                "last_debt, last_apr, apr_source, ticks, updated_at) "
                "VALUES (1,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET accrued_total=excluded.accrued_total, "
                "last_tick_ms=excluded.last_tick_ms, last_debt=excluded.last_debt, "
                "last_apr=excluded.last_apr, apr_source=excluded.apr_source, "
                "ticks=debt_carry_accrual.ticks+1, updated_at=excluded.updated_at",
                (total, now_ms, d, apr, str(info.get("source")), 1, _now_iso()),
            )
            # First-run seeding: both marks start at the first observation so
            # "desde el ultimo reporte" / "desde el cursor" never print the
            # whole history as if it were today's.
            for name in (MARK_LAST_REPORT, MARK_CURSOR):
                con.execute(
                    "INSERT OR IGNORE INTO debt_carry_marks "
                    "(name, accrued_total, debt_at_mark, ts_ms, updated_at) VALUES (?,?,?,?,?)",
                    (name, total, d, now_ms, _now_iso()),
                )
            con.commit()
            acc = _load_accrual(con)
            return acc
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        health_registry.swallowed(SUBSYSTEM, f"observe: {type(e).__name__}")
        log.exception("debt_carry.observe failed")
        return None


def set_mark(name: str, *, debt_usd: float | None = None,
             now_ms: int | None = None) -> bool:
    """Store the current accrued total under ``name`` (last_report / cursor).
    NEVER raises; False on failure."""
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    try:
        con = _conn()
        try:
            acc = _load_accrual(con)
            con.execute(
                "INSERT INTO debt_carry_marks (name, accrued_total, debt_at_mark, ts_ms, updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                "accrued_total=excluded.accrued_total, debt_at_mark=excluded.debt_at_mark, "
                "ts_ms=excluded.ts_ms, updated_at=excluded.updated_at",
                (name, float(acc.get("accrued_total") or 0.0),
                 float(debt_usd) if debt_usd is not None else acc.get("last_debt"),
                 now_ms, _now_iso()),
            )
            con.commit()
        finally:
            con.close()
        return True
    except Exception as e:  # noqa: BLE001
        health_registry.swallowed(SUBSYSTEM, f"set_mark({name}): {type(e).__name__}")
        log.exception("debt_carry.set_mark failed")
        return False


def get_mark(name: str) -> dict[str, Any] | None:
    try:
        con = _conn()
        try:
            row = con.execute("SELECT * FROM debt_carry_marks WHERE name=?", (name,)).fetchone()
        finally:
            con.close()
        return dict(row) if row is not None else None
    except Exception as e:  # noqa: BLE001
        health_registry.swallowed(SUBSYSTEM, f"get_mark({name}): {type(e).__name__}")
        return None


def accrued_since(name: str) -> float | None:
    """Interest accrued since the mark ``name`` (None when the mark does not
    exist yet)."""
    try:
        con = _conn()
        try:
            acc = _load_accrual(con)
            mark = con.execute("SELECT accrued_total FROM debt_carry_marks WHERE name=?",
                               (name,)).fetchone()
        finally:
            con.close()
        if mark is None:
            return None
        return float(acc.get("accrued_total") or 0.0) - float(mark["accrued_total"] or 0.0)
    except Exception as e:  # noqa: BLE001
        health_registry.swallowed(SUBSYSTEM, f"accrued_since({name}): {type(e).__name__}")
        return None


# ─── snapshot for renderers ────────────────────────────────────────────────

@health_registry.tracked("debt_carry", "snapshot")
def snapshot(debt_usd: float | None, *, observe_first: bool = True) -> dict[str, Any]:
    """Everything the PM panel prints about carry, in one dict. NEVER raises.

    Keys: apr, apr_label, apr_source, per_day, since_report, since_cursor,
    cursor_ts_ms, report_ts_ms, accrued_total, gap_flag, ok.
    """
    out: dict[str, Any] = {"ok": False, "apr": None, "apr_label": "", "apr_source": None,
                           "per_day": None, "since_report": None, "since_cursor": None,
                           "cursor_ts_ms": None, "report_ts_ms": None,
                           "accrued_total": None, "gap_flag": False}
    try:
        d = float(debt_usd) if debt_usd is not None else None
    except (TypeError, ValueError):
        d = None
    info = get_usdc_borrow_apr()
    out["apr"] = float(info["apr"])
    out["apr_source"] = info["source"]
    out["apr_label"] = apr_source_label(info)
    if d is None:
        return out
    acc = observe(d, apr_info=info) if observe_first else None
    if acc is None:
        try:
            con = _conn()
            try:
                acc = _load_accrual(con)
            finally:
                con.close()
        except Exception as e:  # noqa: BLE001
            health_registry.swallowed(SUBSYSTEM, f"snapshot: {type(e).__name__}")
            acc = None
    out["per_day"] = d * out["apr"] / 365.0
    if acc is not None:
        out["accrued_total"] = float(acc.get("accrued_total") or 0.0)
        last = int(acc.get("last_tick_ms") or 0)
        if last and (time.time() * 1000 - last) > _GAP_FLAG_SEC * 1000:
            out["gap_flag"] = True
    out["since_report"] = accrued_since(MARK_LAST_REPORT)
    out["since_cursor"] = accrued_since(MARK_CURSOR)
    m_r = get_mark(MARK_LAST_REPORT)
    m_c = get_mark(MARK_CURSOR)
    out["report_ts_ms"] = int(m_r["ts_ms"]) if m_r else None
    out["cursor_ts_ms"] = int(m_c["ts_ms"]) if m_c else None
    out["ok"] = info["source"] != "config"
    return out


def _fmt_day(ts_ms: int | None) -> str:
    if not ts_ms:
        return "?"
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc).strftime("%d-%b")
    except (TypeError, ValueError, OSError, OverflowError):
        return "?"


def panel_line(snap: dict[str, Any] | None) -> str:
    """One PM-panel line. '' when there is nothing to print."""
    if not snap or snap.get("apr") is None:
        return ""
    apr = float(snap["apr"])
    parts = [f"├─ Carry deuda: APR {apr * 100:.2f}% ({snap.get('apr_label') or '?'})"]
    if snap.get("per_day") is not None:
        parts.append(f"≈ ${snap['per_day']:,.2f}/dia")
    sr = snap.get("since_report")
    if sr is not None:
        parts.append(f"desde ult. reporte ≈ +${sr:,.2f}")
    sc = snap.get("since_cursor")
    if sc is not None:
        parts.append(f"desde cursor {_fmt_day(snap.get('cursor_ts_ms'))} ≈ +${sc:,.2f}")
    line = " · ".join(parts)
    if snap.get("gap_flag"):
        line += " · ⚠️ hueco >48h sin observaciones (acumulado sobre el saldo actual)"
    return line


def reset_cursor(debt_usd: float | None = None) -> bool:
    """/carrycursor — restart the "desde cursor" accumulator now."""
    return set_mark(MARK_CURSOR, debt_usd=debt_usd)


def diagnostics() -> dict[str, Any]:
    """Cheap, no-network block for /diagnostico (reads cache + DB only)."""
    out: dict[str, Any] = {}
    try:
        now = time.time()
        if _apr_cache["apr"] is not None:
            out["apr"] = _apr_cache["apr"]
            out["apr_source"] = ("hl_live" if (now - _apr_cache["ts"]) < _APR_TTL_SEC
                                 else "hl_cache")
            out["apr_age_min"] = round((now - _apr_cache["ts"]) / 60.0, 1)
        else:
            out["apr"] = None
            out["apr_source"] = "none" if not _apr_cache["error"] else "config"
        out["apr_error"] = _apr_cache.get("error")
        con = _conn()
        try:
            acc = _load_accrual(con)
            marks = {r["name"]: dict(r) for r in con.execute("SELECT * FROM debt_carry_marks")}
        finally:
            con.close()
        out["accrued_total"] = float(acc.get("accrued_total") or 0.0)
        out["last_tick_ms"] = int(acc.get("last_tick_ms") or 0)
        out["last_debt"] = acc.get("last_debt")
        out["ticks"] = int(acc.get("ticks") or 0)
        out["marks"] = {k: {"accrued_total": v["accrued_total"], "ts_ms": v["ts_ms"]}
                        for k, v in marks.items()}
        out["ok"] = True
    except Exception as e:  # noqa: BLE001
        health_registry.swallowed(SUBSYSTEM, f"diagnostics: {type(e).__name__}")
        out["ok"] = False
        out["error"] = f"{type(e).__name__}: {e}"[:160]
    return out
