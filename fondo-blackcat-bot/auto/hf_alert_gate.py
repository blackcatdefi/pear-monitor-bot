"""R-SILENT — HF alert gate.

Replaces the noisy ``HF_WARN=1.20`` legacy threshold with BCD's preferred
operative policy:

    HF >= HF_ALERT_THRESHOLD (default 1.10) ........ SILENT
    HF_ALERT_CRITICAL <= HF < HF_ALERT_THRESHOLD ... single alert (dedup window)
    HF_ALERT_PRELIQ   <= HF < HF_ALERT_CRITICAL  ... single CRITICAL alert (no dedup)
    HF < HF_ALERT_PRELIQ ........................... PRE-LIQUIDATION, fires every 5 min

Dedup logic for the 1.05–1.10 band:
    * If we already alerted this wallet within the last
      HF_ALERT_DEDUP_MIN minutes (default 120) AND the HF moved less than
      HF_ALERT_DEDUP_DELTA (default 0.05) since the last send → skip.
    * If the wallet's HF crossed *below* HF_ALERT_CRITICAL → ALWAYS send
      (ignore dedup, the band is critical).
    * If the wallet's HF crossed *below* HF_ALERT_PRELIQ → fire every 5 min
      until HF recovers.
    * If HF recovers above HF_ALERT_THRESHOLD → clear state so the next
      drop becomes a fresh first-cross alert.

Public API
----------
``decide(wallet, hf, *, now=None) -> Decision``
    Pure function: given the latest HF for a wallet, decides whether to
    emit and at what severity. ``Decision.should_emit`` plus
    ``Decision.severity`` ('warn' | 'critical' | 'preliq' | None) drives
    the consumer.

``record_emit(wallet, hf, severity, *, now=None) -> None``
    Mark that we just emitted, persisted in SQLite under DATA_DIR.

``last_state(wallet) -> dict | None``
    Diagnostic helper for /silent status, /metrics.

Persistence: ``$DATA_DIR/hf_alerts.db`` (SQLite). Sobrevive cold restarts
si el Volume Railway está montado en /app/data.

Kill switch: ``HF_ALERT_GATE_ENABLED=false`` → bypass completo (alerta
todo, con la lógica vieja).
"""
from __future__ import annotations

import logging
import math
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

ENABLED = os.getenv("HF_ALERT_GATE_ENABLED", "true").strip().lower() != "false"


def _f(env: str, default: float) -> float:
    try:
        return float(os.getenv(env, str(default)) or default)
    except Exception:  # noqa: BLE001
        return default


def _i(env: str, default: int) -> int:
    try:
        return int(os.getenv(env, str(default)) or default)
    except Exception:  # noqa: BLE001
        return default


THRESHOLD = _f("HF_ALERT_THRESHOLD", 1.10)
CRITICAL = _f("HF_ALERT_CRITICAL", 1.05)
PRELIQ = _f("HF_ALERT_PRELIQ", 1.02)
DEDUP_MIN = _i("HF_ALERT_DEDUP_MIN", 120)
DEDUP_DELTA = _f("HF_ALERT_DEDUP_DELTA", 0.05)
PRELIQ_REPEAT_MIN = _i("HF_ALERT_PRELIQ_REPEAT_MIN", 5)


def _db_path() -> str:
    try:
        from config import DATA_DIR  # type: ignore

        base = DATA_DIR
    except Exception:  # noqa: BLE001
        base = os.path.join(os.path.dirname(__file__), "..", "data")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "hf_alerts.db")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_db_path())
    c.execute(
        """CREATE TABLE IF NOT EXISTS hf_alert_state (
            wallet TEXT PRIMARY KEY,
            last_hf REAL NOT NULL,
            last_severity TEXT NOT NULL,
            last_ts_epoch REAL NOT NULL
        )"""
    )
    return c


@dataclass(frozen=True)
class Decision:
    should_emit: bool
    severity: str | None  # 'warn' | 'critical' | 'preliq' | None
    reason: str
    hf: float


def _classify(hf: float) -> str | None:
    if hf < PRELIQ:
        return "preliq"
    if hf < CRITICAL:
        return "critical"
    if hf < THRESHOLD:
        return "warn"
    return None


def _read_state(wallet: str) -> dict[str, Any] | None:
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT last_hf, last_severity, last_ts_epoch FROM hf_alert_state WHERE wallet=?",
                (wallet,),
            ).fetchone()
    except Exception:  # noqa: BLE001
        log.exception("hf_alert_gate: read failed for %s", wallet)
        return None
    if not row:
        return None
    return {
        "last_hf": float(row[0]),
        "last_severity": str(row[1]),
        "last_ts_epoch": float(row[2]),
    }


def last_state(wallet: str) -> dict[str, Any] | None:
    s = _read_state(wallet)
    if not s:
        return None
    age = max(0, int(time.time() - s["last_ts_epoch"]))
    return {**s, "age_s": age}


def decide(wallet: str, hf: float, *, now: float | None = None) -> Decision:
    """Decide whether to alert for *wallet* at *hf*.

    ``now`` is injectable for tests.
    """
    wallet = (wallet or "").lower()
    if not ENABLED:
        # Legacy fallback: always emit warn under HF_WARN, critical under HF_CRITICAL.
        sev = _classify(hf)
        return Decision(
            should_emit=sev is not None,
            severity=sev,
            reason="gate_disabled_passthrough",
            hf=hf,
        )

    if hf is None or (isinstance(hf, float) and (math.isnan(hf) or math.isinf(hf))):
        return Decision(
            should_emit=False,
            severity=None,
            reason="hf_invalid",
            hf=float("nan"),
        )

    sev = _classify(hf)
    if sev is None:
        # HF is healthy → optionally clear state so next drop is a fresh first-cross
        return Decision(
            should_emit=False,
            severity=None,
            reason="hf_healthy",
            hf=hf,
        )

    state = _read_state(wallet)
    now = now if now is not None else time.time()

    # PRELIQ band → repeat every PRELIQ_REPEAT_MIN regardless
    if sev == "preliq":
        if state and state.get("last_severity") == "preliq":
            age_s = now - state["last_ts_epoch"]
            if age_s < PRELIQ_REPEAT_MIN * 60:
                return Decision(
                    should_emit=False,
                    severity="preliq",
                    reason="preliq_dedup_window",
                    hf=hf,
                )
        return Decision(
            should_emit=True,
            severity="preliq",
            reason="preliq_fire",
            hf=hf,
        )

    # CRITICAL band → always emit on first cross OR on severity escalation;
    # only suppress consecutive same-severity within dedup window if delta tiny.
    if sev == "critical":
        if state and state.get("last_severity") == "critical":
            age_s = now - state["last_ts_epoch"]
            delta = abs(hf - state["last_hf"])
            if age_s < DEDUP_MIN * 60 and delta < DEDUP_DELTA:
                return Decision(
                    should_emit=False,
                    severity="critical",
                    reason="critical_dedup_window_small_delta",
                    hf=hf,
                )
        return Decision(
            should_emit=True,
            severity="critical",
            reason="critical_fire",
            hf=hf,
        )

    # WARN band → first cross emits; consecutive warn within DEDUP_MIN with
    # delta < DEDUP_DELTA → skip. Severity escalation (was warn, now critical)
    # is handled above by the critical branch.
    if sev == "warn":
        if state and state.get("last_severity") == "warn":
            age_s = now - state["last_ts_epoch"]
            delta = abs(hf - state["last_hf"])
            if age_s < DEDUP_MIN * 60 and delta < DEDUP_DELTA:
                return Decision(
                    should_emit=False,
                    severity="warn",
                    reason="warn_dedup_window_small_delta",
                    hf=hf,
                )
        return Decision(
            should_emit=True,
            severity="warn",
            reason="warn_fire",
            hf=hf,
        )

    return Decision(
        should_emit=False,
        severity=None,
        reason="unclassified",
        hf=hf,
    )


def record_emit(
    wallet: str, hf: float, severity: str, *, now: float | None = None
) -> None:
    if not ENABLED:
        return
    if severity not in {"warn", "critical", "preliq"}:
        return
    wallet = (wallet or "").lower()
    if not wallet:
        return
    ts = now if now is not None else time.time()
    try:
        with _conn() as c:
            c.execute(
                """INSERT INTO hf_alert_state(wallet, last_hf, last_severity, last_ts_epoch)
                   VALUES(?,?,?,?)
                   ON CONFLICT(wallet) DO UPDATE SET
                     last_hf=excluded.last_hf,
                     last_severity=excluded.last_severity,
                     last_ts_epoch=excluded.last_ts_epoch""",
                (wallet, float(hf), severity, ts),
            )
    except Exception:  # noqa: BLE001
        log.exception("hf_alert_gate: record_emit failed for %s", wallet)


def clear_wallet(wallet: str) -> None:
    """Forget the dedup state for *wallet*. Used when HF recovered."""
    wallet = (wallet or "").lower()
    if not wallet:
        return
    try:
        with _conn() as c:
            c.execute("DELETE FROM hf_alert_state WHERE wallet=?", (wallet,))
    except Exception:  # noqa: BLE001
        log.exception("hf_alert_gate: clear_wallet failed for %s", wallet)


def _reset_for_tests() -> None:
    path = _db_path()
    if os.path.isfile(path):
        try:
            os.remove(path)
        except Exception:  # noqa: BLE001
            pass


def status_summary() -> dict[str, Any]:
    """Return current configuration + tracked wallets — for /silent status."""
    rows: list[dict[str, Any]] = []
    try:
        with _conn() as c:
            for wallet, hf, sev, ts in c.execute(
                "SELECT wallet, last_hf, last_severity, last_ts_epoch FROM hf_alert_state"
            ):
                rows.append(
                    {
                        "wallet": wallet,
                        "last_hf": float(hf),
                        "last_severity": str(sev),
                        "age_s": max(0, int(time.time() - float(ts))),
                    }
                )
    except Exception:  # noqa: BLE001
        log.exception("hf_alert_gate: status_summary failed")
    return {
        "enabled": ENABLED,
        "threshold": THRESHOLD,
        "critical": CRITICAL,
        "preliq": PRELIQ,
        "dedup_min": DEDUP_MIN,
        "dedup_delta": DEDUP_DELTA,
        "preliq_repeat_min": PRELIQ_REPEAT_MIN,
        "tracked_wallets": rows,
    }
