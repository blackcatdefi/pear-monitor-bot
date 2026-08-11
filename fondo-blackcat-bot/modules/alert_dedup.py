"""R-LTV65-QUIET 2.4 — generic alert dedup guard (shared, SQLite-persisted).

Contract: an alert identified by (type, entity, state) must NOT repeat within
its cooldown UNLESS a material field changed. Material fields are passed as a
dict; change detection is per-field with an optional relative tolerance for
numeric fields (default 0 → any change is material).

Usage (pure gate — caller still sends the message):

    from modules.alert_dedup import should_emit
    if should_emit("pm_hf", "PM", "below_1.30", cooldown_hours=6,
                   material={"hf": 1.28}, tolerance=0.02):
        await send(...)

State persists in ``DATA_DIR/alert_dedup.db`` so restarts don't re-fire.
``clear(type, entity)`` re-arms when a condition resolves.
NEVER raises from public functions.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Any

log = logging.getLogger(__name__)

try:
    from config import DATA_DIR
except Exception:  # noqa: BLE001
    DATA_DIR = os.getenv("DATA_DIR", "/tmp")

DB_PATH = os.path.join(DATA_DIR, "alert_dedup.db")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS alert_dedup (
            akey TEXT PRIMARY KEY,
            state TEXT,
            material_json TEXT,
            last_sent REAL
        )
        """
    )
    return conn


def _key(atype: str, entity: str) -> str:
    return f"{(atype or '?').lower()}|{(entity or '?').upper()}"


def _material_changed(
    old: dict[str, Any], new: dict[str, Any], tolerance: float
) -> bool:
    """True if any material field changed beyond ``tolerance`` (relative)."""
    keys = set(old) | set(new)
    for k in keys:
        a, b = old.get(k), new.get(k)
        if a is None or b is None:
            if a != b:
                return True
            continue
        try:
            fa, fb = float(a), float(b)
            base = max(abs(fa), abs(fb), 1e-9)
            if abs(fa - fb) / base > max(0.0, float(tolerance)):
                return True
        except (TypeError, ValueError):
            if str(a) != str(b):
                return True
    return False


def should_emit(
    atype: str,
    entity: str,
    state: str,
    *,
    cooldown_hours: float,
    material: dict[str, Any] | None = None,
    tolerance: float = 0.0,
    now: float | None = None,
) -> bool:
    """Gate: emit iff state changed, material field changed, or cooldown lapsed.

    Records the emission when returning True. NEVER raises (fail-open: on
    storage error returns True so a real alert is never swallowed silently).
    """
    material = dict(material or {})
    t = time.time() if now is None else float(now)
    try:
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT state, material_json, last_sent FROM alert_dedup "
                "WHERE akey=?",
                (_key(atype, entity),),
            ).fetchone()
            emit = True
            if row is not None:
                old_state, old_mat_json, last_sent = row
                try:
                    old_mat = json.loads(old_mat_json or "{}")
                except (TypeError, ValueError):
                    old_mat = {}
                same_state = (old_state or "") == (state or "")
                cooled = (t - float(last_sent or 0.0)) >= cooldown_hours * 3600.0
                changed = _material_changed(old_mat, material, tolerance)
                emit = (not same_state) or changed or cooled
            if emit:
                conn.execute(
                    "INSERT INTO alert_dedup (akey, state, material_json, last_sent) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(akey) DO UPDATE SET state=excluded.state, "
                    "material_json=excluded.material_json, "
                    "last_sent=excluded.last_sent",
                    (_key(atype, entity), state, json.dumps(material), t),
                )
                conn.commit()
            return emit
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        log.exception("alert_dedup should_emit failed (%s/%s)", atype, entity)
        return True


def clear(atype: str, entity: str) -> None:
    """Condition resolved — forget state so the next occurrence fires fresh."""
    try:
        conn = _conn()
        try:
            conn.execute(
                "DELETE FROM alert_dedup WHERE akey=?", (_key(atype, entity),)
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass
