"""R-SIGNAL — per-name short-signal alerting (2026-06-01).

ORTHOGONAL to the R-UNLOCK-PRECISION ladder. The ladder (WATCH/APPROACHING/
UNLOCK) is a *book-level* trigger that only fires when >=NAMES_REQUIRED names
clear all five sub-gates across >=MIN_SECTORS sectors. R-SIGNAL is the
*per-name* trigger: it alerts on ANY individual watchlist name that passes the
full 5-gate short filter, even if it is the only one. The fund then confirms
each name 5/5 with AiPear before executing and adds it to the short book one at
a time.

CRITICAL — NO FORK OF THE GATE ENGINE
    This module owns ZERO gate math. It consumes the per-name verdicts already
    computed by ``unlock_monitor.compute_snapshot`` (each ``AltGate.counts`` is
    True iff that name passed ALL five sub-gates: data-quality >=90% candles,
    z >= +1.00 persistent >=2 readings, Hurst <= 0.47, squeeze/momentum CLEAR,
    funding >= 0). Cointegration stays a context-only proxy (never gates).
    The same estimators, the same thresholds, the same engine — reused, not
    duplicated. R-SIGNAL adds only: a per-name SQLite edge-trigger + debounce,
    a distinct alert format ("🎯 R-SIGNAL"), and the /signals readout.

EDGE-TRIGGER + DEBOUNCE (SQLite, R-SILENT-aware, breaks silence on a state change)
    * A name is QUALIFYING when ``counts`` is True (passes all 5 right now).
    * Before being ANNOUNCED it must hold all 5 for >= SIGNAL_PERSIST_READINGS
      consecutive readings (debounce — kills 1-cycle transients; mirrors the
      z-persistence debounce). Per-name ``qualify_streak`` is persisted.
    * A name is announced ONCE on the transition into qualified (``announced``
      flag), never re-spammed while it stays qualified.
    * If a name drops out (``counts`` goes False) its streak + announced flag
      reset, so a later re-qualify can fire again (edge re-arm).
    * The alert lists ALL currently-qualifying names and flags which are NEW.

R-SILENT
    The signal is actionable, so it BREAKS silent mode — but ONLY on a genuine
    state change (>=1 newly-announced name). Stable cycles emit nothing.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from modules.unlock_monitor import (  # reuse — DO NOT re-implement
    DB_PATH,
    AltGate,
    UnlockSnapshot,
    _fmt_corr,
    _fmt_funding,
    _fmt_hurst,
    _fmt_z,
    constants,
)

log = logging.getLogger(__name__)

SIGNAL_PREFIX = "🎯 R-SIGNAL"


# ─── Tunable (default baked in — NO new Railway env var required to ship) ────
def _envi(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(float(raw))
    except ValueError:
        log.warning("bad %s=%r → %s", name, raw, default)
        return default


def signal_persist_readings() -> int:
    """Consecutive readings a name must hold ALL 5 gates before being announced.

    Default 2 — same debounce horizon as the z-persistence gate, so a single
    transient 4h bar never arms a signal. Overridable via env but NOT required.
    """
    return max(1, _envi("SIGNAL_PERSIST_READINGS", 2))


# ─── SQLite per-name signal state (own table, shared DB; engine untouched) ───
def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS signal_alt_state (
            ticker         TEXT PRIMARY KEY,
            qualify_streak INTEGER NOT NULL DEFAULT 0,
            announced      INTEGER NOT NULL DEFAULT 0,
            updated_at     TEXT
        )
        """
    )
    c.commit()
    return c


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_signal_state() -> dict[str, dict[str, Any]]:
    """{TICKER: {qualify_streak, announced}} from SQLite ({} when empty)."""
    c = _conn()
    try:
        rows = c.execute("SELECT * FROM signal_alt_state").fetchall()
    finally:
        c.close()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        out[str(r["ticker"]).upper()] = {
            "qualify_streak": int(r["qualify_streak"] or 0),
            "announced": bool(r["announced"]),
        }
    return out


def save_signal_state(ticker: str, qualify_streak: int, announced: bool) -> None:
    c = _conn()
    try:
        c.execute(
            """
            INSERT INTO signal_alt_state (ticker, qualify_streak, announced, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                qualify_streak=excluded.qualify_streak,
                announced=excluded.announced,
                updated_at=excluded.updated_at
            """,
            (ticker.upper(), int(qualify_streak), 1 if announced else 0, _now_iso()),
        )
        c.commit()
    finally:
        c.close()


def _reset_for_tests() -> None:
    try:
        c = _conn()
        c.execute("DELETE FROM signal_alt_state")
        c.commit()
        c.close()
    except Exception:  # noqa: BLE001
        pass


# ─── Core evaluation (pure given a snapshot — no network, no engine fork) ────
@dataclass
class SignalResult:
    """Outcome of one R-SIGNAL evaluation over a precomputed UnlockSnapshot."""
    qualifying: list[AltGate]      # debounced set (held all 5 >= persist) — the announced book
    current_counts: list[AltGate]  # raw set passing all 5 THIS reading (drives /signals readout)
    new_names: list[str]           # newly announced this cycle (edge) — empty => do not fire
    total_watch: int               # watchlist size (denominator: "N/total qualify")
    persist: int
    ts_utc: str
    confidence: list[str] = field(default_factory=list)

    @property
    def fire(self) -> bool:
        """Fire (break R-SILENT) only on a state change: >=1 newly-announced name."""
        return bool(self.new_names)


def evaluate_signals(snapshot: UnlockSnapshot, *, advance_state: bool) -> SignalResult:
    """Map the engine's per-name verdicts onto the per-name signal state machine.

    ``advance_state=True`` (scheduler) advances + persists each name's
    ``qualify_streak`` and ``announced`` flag and reports the edge (new_names).
    ``advance_state=False`` (the /signals command) is a PURE READ: it reflects
    "who passes all 5 right now" and "who WOULD be newly announced" without
    mutating SQLite, so a manual check never inflates the debounce counters nor
    burns the once-only announce edge.
    """
    persist = signal_persist_readings()
    prev = load_signal_state()
    alts = list(snapshot.alts)
    total = len(alts)

    current_counts: list[AltGate] = []
    qualifying: list[AltGate] = []
    new_names: list[str] = []

    for a in alts:
        p = prev.get(a.ticker.upper(), {"qualify_streak": 0, "announced": False})
        prev_streak = int(p.get("qualify_streak", 0) or 0)
        prev_announced = bool(p.get("announced", False))

        if a.counts:
            current_counts.append(a)
            streak = prev_streak + 1
            qualified = streak >= persist
            announced = prev_announced
            if qualified:
                qualifying.append(a)
                if not prev_announced:
                    new_names.append(a.ticker)
                    announced = True
        else:
            # Dropped out → re-arm (streak + announce flag cleared).
            streak = 0
            announced = False

        if advance_state:
            save_signal_state(a.ticker, streak, announced)

    confidence = list(snapshot.confidence)
    return SignalResult(
        qualifying=qualifying,
        current_counts=current_counts,
        new_names=new_names,
        total_watch=total,
        persist=persist,
        ts_utc=snapshot.ts_utc,
        confidence=confidence,
    )


# ─── Rendering (reuses unlock_monitor field formatters) ──────────────────────
def _signal_line(a: AltGate, *, is_new: bool) -> str:
    """One per-name signal row: ticker, sector, z, Hurst, funding sign, data-conf,
    plus the cointegration proxy as labelled context."""
    tag = " 🆕 NUEVO" if is_new else ""
    conf = f"{a.coverage * 100:.0f}%"
    return (
        f"  🎯 {a.ticker:<6} [{a.sector}] — "
        f"z {_fmt_z(a.z)} | Hurst {_fmt_hurst(a.hurst)} | "
        f"funding {_fmt_funding(a.funding_sign)} | data {conf} | "
        f"coint~{_fmt_corr(a.corr)}(ctx){tag}"
    )


def signal_aipear_block(alts: list[AltGate]) -> str:
    """Machine-readable AIPEAR_CONFIRM block for the qualifying names — pastes
    straight into AiPear's 5/5 confirmation screen. Same csv schema as the
    R-UNLOCK block: ticker,sector,z4h,hurst,funding,data_conf."""
    lines = ["```", "AIPEAR_CONFIRM v1 (R-SIGNAL pre-filtro — confirmar 5/5):",
             "ticker,sector,z4h,hurst,funding,data_conf"]
    for a in alts:
        fund = f"{a.funding:+.6f}" if a.funding is not None else "n/d"
        lines.append(
            f"{a.ticker},{a.sector},{_fmt_z(a.z)},{_fmt_hurst(a.hurst)},{fund},{a.coverage * 100:.0f}%"
        )
    lines.append("```")
    return "\n".join(lines)


def _header(n: int) -> str:
    return (
        f"{SIGNAL_PREFIX} — {n} nombre(s) pasó el filtro short de 5 gates. "
        "Confirmá cada uno con AiPear 5/5 antes de ejecutar. "
        "El bot NO selecciona tokens ni dimensiona."
    )


def format_signals(res: SignalResult) -> str:
    """Render the /signals on-demand readout (current qualifying set)."""
    n = len(res.current_counts)
    lines = [
        f"{SIGNAL_PREFIX} — filtro short por nombre  ·  {res.ts_utc}",
        "",
    ]
    if n == 0:
        lines.append(
            f"0/{res.total_watch} califican — nada pasa el filtro de 5 gates ahora mismo."
        )
        lines.append(
            "(z≥+1.00 persistente · Hurst≤0.47 · squeeze CLEAR · funding≥0 · data≥90%)"
        )
    else:
        new_set = set(res.new_names)
        lines.append(
            f"{n}/{res.total_watch} pasan los 5 gates "
            f"(debounce ≥{res.persist} lecturas para anuncio):"
        )
        lines.append("")
        for a in res.current_counts:
            lines.append(_signal_line(a, is_new=a.ticker in new_set))
        lines += ["", signal_aipear_block(res.current_counts)]
        lines.append("")
        lines.append(
            "PRE-FILTRO ONLY — confirmá con AiPear 5/5 antes de ejecutar. "
            "El bot no selecciona tokens."
        )
    if res.confidence:
        lines.append("")
        lines.append("Confianza / proxies:")
        for c in res.confidence:
            lines.append(f"  • {c}")
    return "\n".join(lines)


def format_alert(res: SignalResult) -> str:
    """Render the edge-triggered R-SIGNAL alert (fires on a state change only).

    Lists ALL currently-qualifying (debounced) names, flags the NEW ones, and
    appends the machine-readable AiPear block for the full qualifying set.
    """
    qual = res.qualifying
    n = len(qual)
    new_set = set(res.new_names)
    lines = [
        _header(n),
        f"{res.ts_utc}",
        "",
        f"Nuevos este ciclo: {', '.join(res.new_names) if res.new_names else '—'}",
        f"Set que califica ahora ({n}/{res.total_watch}):",
        "",
    ]
    for a in qual:
        lines.append(_signal_line(a, is_new=a.ticker in new_set))
    lines += [
        "",
        signal_aipear_block(qual),
        "",
        "Screen 5/5 = squeeze CLEAR + z+ sobre piso + Hurst<0.5 + funding≥0 + "
        "Bollinger/overbought. Agregá al short book de a uno tras confirmar AiPear.",
        "Ortogonal al ladder 🔓 R-UNLOCK (>=4 nombres): este avisa por nombre individual.",
    ]
    if res.confidence:
        lines.append("")
        lines.append("Nota de confianza: " + " ".join(res.confidence))
    return "\n".join(lines)
