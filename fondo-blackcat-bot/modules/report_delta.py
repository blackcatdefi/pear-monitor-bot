"""R-BOT-DEFINITIVE-2 T6 — DELTA BLOCK vs the previous /reporte.

Deterministic ($0, zero LLM) per-run KPI snapshot + diff rendered right after
the DESTACADO header: TOTAL EQUITY, aave_HF, HYPE oracle price, BTC mark,
PM debt and total perp UPnL. Arrows + absolute + % where meaningful.

* KPIs are persisted in SQLite (shared intel_memory connection) at the end of
  each /reporte run.
* No previous snapshot → the block is OMITTED silently ("" return).
* Everything here NEVER raises — a delta must never break /reporte.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

_KPI_KEYS = ("total_equity", "aave_hf", "hype_px", "btc_mark", "pm_debt", "perp_upnl")

_EQUITY_RE = re.compile(r"TOTAL EQUITY:\s*(-?\$[\d.,]+\s?[KM]?)")


def _f(v: Any) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_usd(txt: str) -> float | None:
    """'$106.2K' / '-$1.05M' / '$923.00' → float USD. None on '—'/garbage."""
    try:
        t = str(txt or "").strip().replace(",", "").replace(" ", "")
        if not t or t == "—":
            return None
        mult = 1.0
        if t.endswith("K"):
            mult, t = 1_000.0, t[:-1]
        elif t.endswith("M"):
            mult, t = 1_000_000.0, t[:-1]
        return float(t.replace("$", "")) * mult
    except (TypeError, ValueError):
        return None


def collect_report_kpis(
    wallets: list[dict[str, Any]] | None,
    market: dict[str, Any] | None,
    header_text: str | None = None,
) -> dict[str, Any]:
    """Snapshot the 6 delta KPIs from the SAME objects /reporte already has.

    TOTAL EQUITY is read back from the rendered DESTACADO header (single
    source of truth — no re-derivation drift). NEVER raises; missing KPIs
    are None and simply skip their delta line.
    """
    kpis: dict[str, Any] = {k: None for k in _KPI_KEYS}
    try:
        m = _EQUITY_RE.search(header_text or "")
        if m:
            kpis["total_equity"] = _parse_usd(m.group(1))
    except Exception:  # noqa: BLE001
        pass
    try:
        from modules.pm_context import select_primary_pm_state
        pm = select_primary_pm_state(wallets, market)
        if pm is not None:
            kpis["aave_hf"] = _f(getattr(pm, "aave_hf", None)) or None
            kpis["hype_px"] = _f(getattr(pm, "hype_px", None)) or None
            kpis["pm_debt"] = _f(getattr(pm, "debt_usd", None))
    except Exception:  # noqa: BLE001
        log.debug("report_delta: pm state unavailable", exc_info=True)
    try:
        from templates.formatters import _build_price_map
        btc = _build_price_map(market).get("BTC")
        kpis["btc_mark"] = _f(btc) or None
    except Exception:  # noqa: BLE001
        pass
    try:
        from templates.formatters import _perp_upnl_split
        kpis["perp_upnl"] = _f(_perp_upnl_split(wallets or [])[4])
    except Exception:  # noqa: BLE001
        pass
    kpis["ts"] = datetime.now(timezone.utc).isoformat()
    return kpis


# ── Persistence ───────────────────────────────────────────────────────────────
def _conn():
    from modules.intel_memory import _get_conn
    conn = _get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS report_kpis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            total_equity REAL, aave_hf REAL, hype_px REAL,
            btc_mark REAL, pm_debt REAL, perp_upnl REAL
        )
        """
    )
    conn.commit()
    return conn


def save_report_kpis(kpis: dict[str, Any]) -> bool:
    """Persist this run's KPIs (called at the end of /reporte). NEVER raises."""
    try:
        conn = _conn()
        conn.execute(
            "INSERT INTO report_kpis "
            "(ts, total_equity, aave_hf, hype_px, btc_mark, pm_debt, perp_upnl) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                str(kpis.get("ts") or datetime.now(timezone.utc).isoformat()),
                *(kpis.get(k) for k in _KPI_KEYS),
            ),
        )
        # Keep the table lean — 60 snapshots is weeks of history.
        conn.execute(
            "DELETE FROM report_kpis WHERE id NOT IN "
            "(SELECT id FROM report_kpis ORDER BY id DESC LIMIT 60)"
        )
        conn.commit()
        conn.close()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("save_report_kpis failed: %s", exc)
        return False


def load_last_kpis() -> dict[str, Any] | None:
    """Most recent persisted snapshot (the PREVIOUS report). NEVER raises."""
    try:
        conn = _conn()
        row = conn.execute(
            "SELECT ts, total_equity, aave_hf, hype_px, btc_mark, pm_debt, "
            "perp_upnl FROM report_kpis ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if not row:
            return None
        out = {k: row[k] for k in _KPI_KEYS}
        out["ts"] = row["ts"]
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("load_last_kpis failed: %s", exc)
        return None


# ── One-time baseline-correction note (R-EQUITY-DEDUP-DREAMCASH) ─────────────
# The first /reporte after the 2026-07-07 fix shows TOTAL EQUITY ~$14K lower
# than the previous snapshot — that drop is a CORRECTION (prior runs double
# counted DreamCash USDC reserve + UPnL on top of accountValue), not a loss.
# Env-gated (set in Railway, absent in tests) + SQLite consumed-flag so the
# note fires EXACTLY once in production and never in the test suite.
_BASELINE_NOTE_ENV = "EQUITY_BASELINE_CORRECTION_NOTE"
_BASELINE_NOTE_KEY = "equity_baseline_note_consumed_v1"


def _consume_baseline_note() -> str:
    """Return the one-time note (and mark it consumed), or ''. NEVER raises."""
    try:
        import os
        if not (os.getenv(_BASELINE_NOTE_ENV) or "").strip():
            return ""
        conn = _conn()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS report_delta_flags "
            "(key TEXT PRIMARY KEY, ts TEXT NOT NULL)"
        )
        row = conn.execute(
            "SELECT 1 FROM report_delta_flags WHERE key = ?",
            (_BASELINE_NOTE_KEY,),
        ).fetchone()
        if row:
            conn.close()
            return ""
        conn.execute(
            "INSERT OR IGNORE INTO report_delta_flags (key, ts) VALUES (?, ?)",
            (_BASELINE_NOTE_KEY, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()
        return (
            "ℹ️ baseline corregida: reportes previos doble-contaban DreamCash "
            "USDC+UPnL (~$14K). La caída de TOTAL EQUITY es la corrección, "
            "no una pérdida."
        )
    except Exception:  # noqa: BLE001
        log.debug("baseline note check failed", exc_info=True)
        return ""


# ── Rendering ─────────────────────────────────────────────────────────────────
def _age_text(prev_ts: Any) -> str:
    try:
        prev = datetime.fromisoformat(str(prev_ts))
        hrs = (datetime.now(timezone.utc) - prev).total_seconds() / 3600.0
        if hrs < 0:
            return ""
        if hrs < 1.0:
            return f" (hace {hrs*60:.0f}m)"
        return f" (hace {hrs:.1f}h)"
    except Exception:  # noqa: BLE001
        return ""


def _fmt_usd(v: float) -> str:
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1_000_000:
        return f"{sign}${a/1_000_000:.2f}M"
    if a >= 1_000:
        return f"{sign}${a/1_000:.1f}K"
    return f"{sign}${a:.2f}"


def _delta_line(label: str, prev: Any, curr: Any, *, kind: str) -> str | None:
    """One arrowed delta row. kind ∈ {'usd','px','hf'}. None when not renderable."""
    p, c = _f(prev), _f(curr)
    if p is None or c is None:
        return None
    d = c - p
    arrow = "▲" if d > 1e-9 else ("▼" if d < -1e-9 else "＝")
    if kind == "hf":
        return f"{label}: {p:.2f} → {c:.2f}  {arrow} {d:+.2f}"
    if kind == "px":
        pct = f" ({d/p*100.0:+.1f}%)" if abs(p) > 1e-9 else ""
        return f"{label}: ${p:,.2f} → ${c:,.2f}  {arrow} {d:+,.2f}{pct}"
    # USD aggregates — % only when the base is meaningful (avoids ±∞% noise
    # on near-zero UPnL / debt bases).
    pct = f" ({d/abs(p)*100.0:+.1f}%)" if abs(p) >= 100.0 else ""
    return f"{label}: {_fmt_usd(p)} → {_fmt_usd(c)}  {arrow} {d:+,.0f}{pct}"


def format_report_delta_block(
    current: dict[str, Any], previous: dict[str, Any] | None,
) -> str:
    """The DELTA block. '' when there is no previous snapshot. NEVER raises."""
    try:
        if not isinstance(previous, dict) or not previous:
            return ""
        rows = [
            _delta_line("💰 TOTAL EQUITY", previous.get("total_equity"),
                        current.get("total_equity"), kind="usd"),
            _delta_line("⚖️ aave-HF", previous.get("aave_hf"),
                        current.get("aave_hf"), kind="hf"),
            _delta_line("💠 HYPE oracle", previous.get("hype_px"),
                        current.get("hype_px"), kind="px"),
            _delta_line("₿ BTC mark", previous.get("btc_mark"),
                        current.get("btc_mark"), kind="px"),
            _delta_line("🏦 PM deuda", previous.get("pm_debt"),
                        current.get("pm_debt"), kind="usd"),
            _delta_line("📈 Σ PERP UPnL", previous.get("perp_upnl"),
                        current.get("perp_upnl"), kind="usd"),
        ]
        rows = [r for r in rows if r]
        if not rows:
            return ""
        header = (
            "🔀 DELTA vs REPORTE ANTERIOR" + _age_text(previous.get("ts"))
            + "\n" + ("─" * 30)
        )
        note = _consume_baseline_note()
        body = header + "\n" + "\n".join(rows)
        return (body + "\n" + note) if note else body
    except Exception:  # noqa: BLE001
        log.exception("format_report_delta_block failed")
        return ""
