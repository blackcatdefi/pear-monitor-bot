"""Round 18.3.5 — Scheduler self-healing wrapper.

Wraps APScheduler jobs so:
  * each run is timed
  * exceptions are caught + counted (don't kill scheduler)
  * after N consecutive failures (default 3), an escalation alert fires once
  * after a successful run, the failure counter resets
  * /scheduler_health renders a table of (job_name, last_ok, fails_in_a_row, last_error)

Kill switch: ``SELF_HEALING_ENABLED=false`` — falls back to plain wrap (logs only).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from config import DATA_DIR, TELEGRAM_CHAT_ID

log = logging.getLogger(__name__)

DB_PATH = os.path.join(DATA_DIR, "scheduler_health.db")

DEFAULT_MAX_FAILS = 3
ESCALATION_COOLDOWN_HOURS = 6


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute(
        """CREATE TABLE IF NOT EXISTS scheduler_state (
            job_name TEXT PRIMARY KEY,
            last_run_ts TEXT,
            last_ok_ts TEXT,
            last_error_ts TEXT,
            last_error_msg TEXT,
            consecutive_failures INTEGER DEFAULT 0,
            total_runs INTEGER DEFAULT 0,
            total_failures INTEGER DEFAULT 0,
            last_duration_ms INTEGER,
            last_escalation_ts TEXT
        )"""
    )
    return c


def _get(job_name: str) -> dict[str, Any] | None:
    c = _conn()
    try:
        row = c.execute(
            "SELECT job_name, last_run_ts, last_ok_ts, last_error_ts, last_error_msg, "
            "consecutive_failures, total_runs, total_failures, last_duration_ms, last_escalation_ts "
            "FROM scheduler_state WHERE job_name=?",
            (job_name,),
        ).fetchone()
        if not row:
            return None
        keys = ["job_name", "last_run_ts", "last_ok_ts", "last_error_ts",
                "last_error_msg", "consecutive_failures", "total_runs",
                "total_failures", "last_duration_ms", "last_escalation_ts"]
        return dict(zip(keys, row))
    finally:
        c.close()


def _upsert(state: dict[str, Any]) -> None:
    c = _conn()
    try:
        c.execute(
            """INSERT INTO scheduler_state(
                job_name, last_run_ts, last_ok_ts, last_error_ts, last_error_msg,
                consecutive_failures, total_runs, total_failures,
                last_duration_ms, last_escalation_ts)
              VALUES(?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(job_name) DO UPDATE SET
                last_run_ts=excluded.last_run_ts,
                last_ok_ts=excluded.last_ok_ts,
                last_error_ts=excluded.last_error_ts,
                last_error_msg=excluded.last_error_msg,
                consecutive_failures=excluded.consecutive_failures,
                total_runs=excluded.total_runs,
                total_failures=excluded.total_failures,
                last_duration_ms=excluded.last_duration_ms,
                last_escalation_ts=excluded.last_escalation_ts
            """,
            (
                state["job_name"], state.get("last_run_ts"),
                state.get("last_ok_ts"), state.get("last_error_ts"),
                state.get("last_error_msg"),
                int(state.get("consecutive_failures") or 0),
                int(state.get("total_runs") or 0),
                int(state.get("total_failures") or 0),
                state.get("last_duration_ms"),
                state.get("last_escalation_ts"),
            ),
        )
        c.commit()
    finally:
        c.close()


def _is_enabled() -> bool:
    return os.getenv("SELF_HEALING_ENABLED", "true").strip().lower() != "false"


def _max_fails() -> int:
    try:
        return int(os.getenv("SCHED_MAX_FAILS", str(DEFAULT_MAX_FAILS)))
    except ValueError:
        return DEFAULT_MAX_FAILS


async def _maybe_escalate(bot, state: dict[str, Any]) -> None:
    if not bot or not TELEGRAM_CHAT_ID:
        return
    last_esc = state.get("last_escalation_ts")
    now = datetime.now(timezone.utc)
    if last_esc:
        try:
            esc_dt = datetime.fromisoformat(last_esc)
            if (now - esc_dt).total_seconds() < ESCALATION_COOLDOWN_HOURS * 3600:
                return
        except Exception:
            pass
    try:
        from utils.telegram import send_bot_message
        msg = (
            f"🚨 SCHEDULER UNHEALTHY — {state['job_name']}\n"
            f"Fallos consecutivos: {state['consecutive_failures']}\n"
            f"Último error: {(state.get('last_error_msg') or '')[:300]}\n"
            f"Total runs: {state['total_runs']} (fails: {state['total_failures']})\n\n"
            "Acción: revisar /errors y logs Railway. /scheduler_health para tabla completa."
        )
        await send_bot_message(bot, TELEGRAM_CHAT_ID, msg)
        state["last_escalation_ts"] = now.isoformat()
        _upsert(state)
    except Exception:  # noqa: BLE001
        log.exception("scheduler_self_healing: escalation alert failed")


def wrap(job_name: str, coro_factory: Callable[..., Awaitable[Any]], bot=None):
    """Return an async function that runs `coro_factory` with self-healing instrumentation.

    `coro_factory` is a callable that returns an awaitable when invoked with no
    arguments. (We don't pass args through — wire your job to call coro_factory()
    however it needs to.)
    """
    async def runner(*args, **kwargs):
        if not _is_enabled():
            return await coro_factory(*args, **kwargs)
        state = _get(job_name) or {
            "job_name": job_name, "last_run_ts": None, "last_ok_ts": None,
            "last_error_ts": None, "last_error_msg": None,
            "consecutive_failures": 0, "total_runs": 0, "total_failures": 0,
            "last_duration_ms": None, "last_escalation_ts": None,
        }
        start = time.monotonic()
        now_iso = datetime.now(timezone.utc).isoformat()
        state["last_run_ts"] = now_iso
        state["total_runs"] = int(state.get("total_runs") or 0) + 1
        try:
            res = await coro_factory(*args, **kwargs)
            duration_ms = int((time.monotonic() - start) * 1000)
            state["last_ok_ts"] = now_iso
            state["last_duration_ms"] = duration_ms
            state["consecutive_failures"] = 0
            _upsert(state)
            return res
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            duration_ms = int((time.monotonic() - start) * 1000)
            state["last_error_ts"] = now_iso
            state["last_error_msg"] = f"{type(e).__name__}: {e}"
            state["last_duration_ms"] = duration_ms
            state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
            state["total_failures"] = int(state.get("total_failures") or 0) + 1
            _upsert(state)
            log.exception("scheduler[%s] failed", job_name)
            log.debug("scheduler[%s] traceback: %s", job_name, traceback.format_exc())
            if state["consecutive_failures"] >= _max_fails():
                await _maybe_escalate(bot, state)
            return None
    return runner


def all_states() -> list[dict[str, Any]]:
    c = _conn()
    try:
        rows = c.execute(
            "SELECT job_name, last_run_ts, last_ok_ts, last_error_ts, last_error_msg, "
            "consecutive_failures, total_runs, total_failures, last_duration_ms, "
            "last_escalation_ts FROM scheduler_state ORDER BY job_name ASC"
        ).fetchall()
        keys = ["job_name", "last_run_ts", "last_ok_ts", "last_error_ts",
                "last_error_msg", "consecutive_failures", "total_runs",
                "total_failures", "last_duration_ms", "last_escalation_ts"]
        return [dict(zip(keys, r)) for r in rows]
    finally:
        c.close()


def format_health() -> str:
    states = all_states()
    if not states:
        return (
            "🩺 SCHEDULER HEALTH\n" + "─" * 30 + "\n"
            "Sin runs aún registrados. Esperá un ciclo de scheduler."
        )
    lines = ["🩺 SCHEDULER HEALTH", "─" * 36]
    for s in states:
        cf = int(s.get("consecutive_failures") or 0)
        tot = int(s.get("total_runs") or 0)
        fails = int(s.get("total_failures") or 0)
        dur = s.get("last_duration_ms")
        ok = s.get("last_ok_ts") or "—"
        if cf == 0:
            emoji = "✅"
        elif cf < _max_fails():
            emoji = "⚠️"
        else:
            emoji = "🚨"
        lines.append(f"{emoji} {s['job_name']}")
        lines.append(f"   Runs: {tot} (fails: {fails}) · Consecutive: {cf}")
        lines.append(f"   Last OK: {ok[:19].replace('T', ' ')} · Last duration: "
                     f"{dur if dur is not None else '?'} ms")
        if cf > 0 and s.get("last_error_msg"):
            lines.append(f"   Error: {s['last_error_msg'][:160]}")
        lines.append("")
    lines.append(f"Threshold escalación: {_max_fails()} fallos consecutivos · "
                 f"Cooldown alert: {ESCALATION_COOLDOWN_HOURS}h")
    return "\n".join(lines).rstrip()
