"""R-PMALERT — edge-triggered Portfolio Margin ratio alerting (SQLite, R-SILENT-aware).

WHAT THIS IS
    An ALERTING layer on top of R-PMCORE's ``PMState`` (modules.portfolio_margin).
    R-PMCORE already computes the live PM state — HYPE collateral (oracle-valued),
    USDC/USDH debt drawn, borrow capacity (0.50 LTV), the borrow-capacity
    utilisation ratio (``debt / capacity``) and the naked-long guard. This module
    decides WHEN to fire an alert as that ratio climbs toward liquidation, with
    proper edge-triggering so the bot never spams the same band.

THE FOUR BANDS  (on ``portfolio_margin_ratio`` — 0.95 = liquidation in the
                 R-PMCORE model)
    🟢 CALM      ratio <  0.40   — silent baseline, NO alert
    🟡 WARN      ratio >= 0.40   — alert: debt rising vs HYPE collateral
    🟠 STRESS    ratio >= 0.70   — stronger alert: liquidation path forming
    🔴 LIQ-RISK  ratio >= 0.85   — CRITICAL, highest priority, breaks R-SILENT
                                   UNCONDITIONALLY (pre-liq; 0.95 is liquidation)

    The 0.40 / 0.70 / 0.95 thresholds are the SAME constants R-PMCORE already
    defines (``config.PM_WARN_RATIO`` / ``PM_STRESS_RATIO`` / ``PM_LIQ_RATIO``).
    R-PMALERT adds the 0.85 pre-liquidation tier (``config.PM_CRITICAL_RATIO``)
    so we get one escalation step BEFORE the 0.95 liquidation point. All four are
    overridable via env (see the constants block below).

EDGE-TRIGGERING (SQLite, mirrors R-UNLOCK / R-SILENT discipline)
    State lives in a single SQLite row (``pm_alert_state`` in the shared
    ``intel_memory.db`` on the Railway ``/app/data`` volume). An alert fires ONLY
    when the band CROSSES UP to a higher rank than the one last fired. The same
    band NEVER re-alerts (no spam). If the ratio drops back to a lower band the
    stored level RESETS SILENTLY, so the next genuine upward cross re-fires. The
    naked-long guard (debt drawn, no shorts) is tracked on its OWN edge and fires
    regardless of band — preserving R-PMCORE's hard-rule behaviour intact.

R-SILENT
    Only LIQ-RISK (CRITICAL) breaks silence unconditionally. WARN/STRESS are
    suppressed while silent mode is on (the state still advances silently so the
    next escalation keeps tracking). Configurable via
    ``PM_ALERT_BREAKS_SILENCE_LEVEL`` (default CRITICAL). The naked-long alert
    always breaks silence (hedge missing is a hard-rule violation).

HARD BOUNDARY
    This module NEVER trades, sizes, or moves money. It emits a Telegram alert so
    BCD acts manually (add collateral / cut shorts). Robustness: NEVER raises — a
    data gap degrades to a confidence/staleness note, never a crash or a fabricated
    alert.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

# ─── Persistent DATA_DIR (Railway Volume at /app/data in prod) ────────────────
try:
    from config import DATA_DIR  # type: ignore
except Exception:  # noqa: BLE001
    DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
    os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "intel_memory.db")


# ─── Env helpers (read live so Railway overrides take effect) ────────────────
def _envf(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("bad %s=%r → using %s", name, raw, default)
        return default


# ─── Threshold constants (ALL configurable; documented defaults) ─────────────
# These mirror R-PMCORE's config constants so the alert bands and the /pm /
# /reporte display stay in lock-step. Defaults:
#   PM_WARN_RATIO      0.40  — debt = 40% of borrow capacity (first warning)
#   PM_STRESS_RATIO    0.70  — 70% utilised, liquidation path forming
#   PM_CRITICAL_RATIO  0.85  — NEW pre-liq tier: one escalation BEFORE 0.95
#   PM_LIQ_RATIO       0.95  — the actual liquidation point in the PM model
def warn_ratio() -> float:
    return _envf("PM_WARN_RATIO", 0.40)


def stress_ratio() -> float:
    return _envf("PM_STRESS_RATIO", 0.70)


def critical_ratio() -> float:
    return _envf("PM_CRITICAL_RATIO", 0.85)


def liq_ratio() -> float:
    return _envf("PM_LIQ_RATIO", 0.95)


# ─── Alert-level ladder (CALM < WARN < STRESS < CRITICAL) ────────────────────
CALM = "CALM"
WARN = "WARN"
STRESS = "STRESS"
CRITICAL = "CRITICAL"  # the 🔴 LIQ-RISK band (>= 0.85)

_LEVEL_RANK = {CALM: 0, WARN: 1, STRESS: 2, CRITICAL: 3}
_LEVEL_EMOJI = {CALM: "🟢", WARN: "🟡", STRESS: "🟠", CRITICAL: "🔴"}
_LEVEL_LABEL = {CALM: "CALM", WARN: "WARN", STRESS: "STRESS", CRITICAL: "LIQ-RISK"}


def classify_alert_level(ratio: float) -> str:
    """Map a borrow-capacity utilisation ratio to one of the 4 alert bands.

    CRITICAL covers ``[0.85, ∞)`` — i.e. the approaching-liquidation band, which
    includes the 0.95 liquidation point itself. NEVER raises.
    """
    try:
        r = float(ratio)
    except (TypeError, ValueError):
        return CALM
    if r >= critical_ratio():
        return CRITICAL
    if r >= stress_ratio():
        return STRESS
    if r >= warn_ratio():
        return WARN
    return CALM


def alert_breaks_silence_level() -> str:
    """Minimum band allowed to BREAK R-SILENT (default CRITICAL).

    Below this band, alerts are suppressed while silent mode is on; the state is
    still advanced silently so escalations keep tracking. The naked-long alert is
    handled separately and ALWAYS breaks silence.
    """
    lv = os.getenv("PM_ALERT_BREAKS_SILENCE_LEVEL", CRITICAL).strip().upper()
    return lv if lv in _LEVEL_RANK else CRITICAL


def breaks_silence(level: str) -> bool:
    """True if ``level`` is at or above the configured break-silence band."""
    return _LEVEL_RANK.get(level, 0) >= _LEVEL_RANK.get(alert_breaks_silence_level(), 3)


# ─── SQLite edge-trigger state (single row, mirrors R-UNLOCK) ────────────────
def _conn(db_path: Optional[str] = None) -> sqlite3.Connection:
    c = sqlite3.connect(db_path or DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS pm_alert_state (
            key         TEXT PRIMARY KEY,
            level       TEXT NOT NULL DEFAULT 'CALM',
            naked       INTEGER NOT NULL DEFAULT 0,
            updated_at  TEXT
        )
        """
    )
    return c


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state(db_path: Optional[str] = None) -> dict[str, Any]:
    c = _conn(db_path)
    try:
        r = c.execute("SELECT * FROM pm_alert_state WHERE key='singleton'").fetchone()
    finally:
        c.close()
    if r is None:
        return {"level": CALM, "naked": False}
    return {"level": r["level"] or CALM, "naked": bool(r["naked"])}


def save_state(level: str, naked: bool, db_path: Optional[str] = None) -> None:
    c = _conn(db_path)
    try:
        c.execute(
            """
            INSERT INTO pm_alert_state (key, level, naked, updated_at)
            VALUES ('singleton', ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                level=excluded.level, naked=excluded.naked,
                updated_at=excluded.updated_at
            """,
            (level, 1 if naked else 0, _now_iso()),
        )
        c.commit()
    finally:
        c.close()


def should_fire(new_level: str, last_level: str) -> bool:
    """Edge-trigger: fire only on an ESCALATION to a higher band. A retreat
    updates state silently so the next genuine upward cross can fire."""
    return _LEVEL_RANK.get(new_level, 0) > _LEVEL_RANK.get(last_level, 0)


def _reset_for_tests(db_path: Optional[str] = None) -> None:
    try:
        c = _conn(db_path)
        c.execute("DELETE FROM pm_alert_state")
        c.commit()
        c.close()
    except Exception:  # noqa: BLE001
        pass


# ─── Money formatting (matches portfolio_margin._fmt_usd) ────────────────────
def _fmt_usd(v: float) -> str:
    av = abs(v)
    if av >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if av >= 1_000:
        return f"${v / 1_000:.1f}K"
    return f"${v:,.0f}"


# ─── Staleness / confidence note ─────────────────────────────────────────────
def staleness_note(pm: Any) -> str:
    """Return a confidence/staleness caveat when the inputs were degraded.

    R-PMCORE never raises on missing data — it returns zeros. If the HYPE oracle
    price could not be resolved (``hype_px <= 0``) the collateral and therefore
    the ratio are unreliable; flag that so an alert is read with the right
    confidence. Empty string when inputs look healthy.
    """
    try:
        if pm is None or not getattr(pm, "has_data", False):
            return "⚠️ Datos PM degradados (sin balances) — confianza baja."
        if getattr(pm, "hype_qty", 0.0) > 0 and getattr(pm, "hype_px", 0.0) <= 0:
            return ("⚠️ Oracle HYPE no disponible — colateral/ratio estimados, "
                    "confianza baja.")
        if getattr(pm, "collateral_usd", 0.0) <= 0 and getattr(pm, "debt_usd", 0.0) > 0:
            return "⚠️ Colateral=0 con deuda>0 — dato de colateral degradado."
    except Exception:  # noqa: BLE001
        return ""
    return ""


# ─── Alert message templates (the 4 messages) ────────────────────────────────
_HEAD = {
    WARN: "🟡 PM RATIO — WARN",
    STRESS: "🟠 PM RATIO — STRESS",
    CRITICAL: "🔴 PM RATIO — CRÍTICO (LIQ-RISK)",
}
_ACTION = {
    WARN: "PM ratio WARN — debt rising vs HYPE collateral. Add USDC or trim shorts.",
    STRESS: ("PM ratio STRESS — liquidation path forming. Act now: add collateral "
             "or cut shorts."),
    CRITICAL: ("PM ratio CRITICAL — approaching liquidation (0.95). HYPE collateral "
               "at risk. Reduce immediately."),
}


def _detail_lines(pm: Any) -> list[str]:
    """The shared evidence block carried by every PM ratio alert."""
    shorts = (
        f"{_fmt_usd(pm.shorts_notional)} notional"
        if getattr(pm, "shorts_notional", 0.0) > 0
        else "sin shorts abiertos"
    )
    coll = _fmt_usd(getattr(pm, "collateral_usd", 0.0))
    if getattr(pm, "hype_qty", 0.0) > 0 and getattr(pm, "hype_px", 0.0) > 0:
        coll += f" ({pm.hype_qty:,.1f} HYPE × ${pm.hype_px:,.2f})"
    return [
        f"• Margin ratio: {getattr(pm, 'ratio', 0.0) * 100:.1f}%  "
        f"(🟡WARN {warn_ratio()*100:.0f}% · 🟠STRESS {stress_ratio()*100:.0f}% · "
        f"🔴CRÍTICO {critical_ratio()*100:.0f}% · LIQ {liq_ratio()*100:.0f}%)",
        f"• Colateral HYPE: {coll}",
        f"• Deuda (USDC/USDH borrowed): {_fmt_usd(getattr(pm, 'debt_usd', 0.0))}",
        f"• Capacidad borrow (LTV {_envf('PM_HYPE_LTV', 0.50):.2f}): "
        f"{_fmt_usd(getattr(pm, 'capacity_usd', 0.0))}  | disponible: "
        f"{_fmt_usd(getattr(pm, 'available_usd', 0.0))}",
        f"• Hedge (shorts basket): {shorts}",
    ]


def build_alert_message(pm: Any, level: str, *, now: Optional[datetime] = None) -> str:
    """Render the alert for a given band. NEVER raises."""
    ts = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    head = _HEAD.get(level, "🟡 PM RATIO")
    action = _ACTION.get(level, "")
    lines = [head, action, ""]
    lines.extend(_detail_lines(pm))
    note = staleness_note(pm)
    if note:
        lines.append(note)
    lines.append(f"🕒 {ts}")
    return "\n".join(lines)


def build_naked_long_message(pm: Any, *, now: Optional[datetime] = None) -> str:
    """The naked-long (hedge-missing) alert — fires regardless of band."""
    ts = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "🚨 PM — HEDGE MISSING (naked leveraged long)",
        (f"Deuda {_fmt_usd(getattr(pm, 'debt_usd', 0.0))} contra HYPE "
         f"{_fmt_usd(getattr(pm, 'collateral_usd', 0.0))} SIN shorts abiertos. "
         "Falta el hedge del basket — regla dura del fondo."),
        "",
    ]
    lines.extend(_detail_lines(pm))
    note = staleness_note(pm)
    if note:
        lines.append(note)
    lines.append(f"🕒 {ts}")
    return "\n".join(lines)


# ─── Decision object + evaluate() ────────────────────────────────────────────
@dataclass(frozen=True)
class AlertDecision:
    should_alert: bool         # an alert is warranted this tick (edge crossed)
    breaks_silence: bool       # may break R-SILENT (CRITICAL or naked-long)
    level: str                 # current band (CALM/WARN/STRESS/CRITICAL)
    naked_long: bool           # current naked-long flag
    reason: str                # "level_cross" | "naked_long" | "none"
    message: str               # rendered alert ("" when should_alert is False)


def evaluate(
    pm: Any,
    *,
    db_path: Optional[str] = None,
    now: Optional[datetime] = None,
    persist: bool = True,
) -> AlertDecision:
    """Decide whether to alert for the current PMState, edge-triggered via SQLite.

    Returns an :class:`AlertDecision`. By default it PERSISTS the new state
    (so a retreat resets silently); pass ``persist=False`` for a dry-run. The
    naked-long edge takes priority over the band edge (hedge missing is the
    hardest rule). NEVER raises.
    """
    try:
        cur_level = classify_alert_level(getattr(pm, "ratio", 0.0)) \
            if (pm is not None and getattr(pm, "has_data", False)) else CALM
        cur_naked = bool(getattr(pm, "naked_long", False)) if pm is not None else False

        prev = load_state(db_path)
        prev_level = prev.get("level", CALM)
        prev_naked = bool(prev.get("naked", False))

        naked_edge = cur_naked and not prev_naked
        level_edge = should_fire(cur_level, prev_level)

        if persist:
            save_state(cur_level, cur_naked, db_path)

        if naked_edge:
            return AlertDecision(
                should_alert=True, breaks_silence=True, level=cur_level,
                naked_long=cur_naked, reason="naked_long",
                message=build_naked_long_message(pm, now=now),
            )
        if level_edge:
            return AlertDecision(
                should_alert=True, breaks_silence=breaks_silence(cur_level),
                level=cur_level, naked_long=cur_naked, reason="level_cross",
                message=build_alert_message(pm, cur_level, now=now),
            )
        return AlertDecision(
            should_alert=False, breaks_silence=False, level=cur_level,
            naked_long=cur_naked, reason="none", message="",
        )
    except Exception:  # noqa: BLE001
        log.exception("pm_alert_monitor.evaluate failed")
        return AlertDecision(False, False, CALM, False, "none", "")
