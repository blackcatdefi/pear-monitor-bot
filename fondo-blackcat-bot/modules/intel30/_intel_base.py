"""Shared helpers for intel30 modules — R-PERFECT Phase 2/3.

Centralises:
  • HTTP client config (User-Agent, timeout, retries)
  • Structured observability logger (Fase 3 hardening #1)
  • Rate-limit guard (Fase 3 hardening #2)
  • Source-status registry (LIVE / DEGRADED / UNAVAILABLE) — feeds /selftest

All intel30 modules SHOULD use these helpers so observability is uniform.
Existing Phase 1 modules continue to work unchanged (no breaking changes).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 15.0
DEFAULT_RETRIES = 2

# ── observability log path (Volume on Railway: /app/data) ────────────────
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except (PermissionError, OSError):
    DATA_DIR = Path("/tmp/intel_data")
    DATA_DIR.mkdir(parents=True, exist_ok=True)

INTEL_LOG_PATH = DATA_DIR / "intel.log"
RATE_DB_PATH = DATA_DIR / "intel_rate.db"
SOURCE_STATE_DB = DATA_DIR / "intel_source_state.db"


# ── source status enum (string-based, JSON-friendly) ─────────────────────
LIVE = "LIVE"
DEGRADED = "DEGRADED"
UNAVAILABLE = "UNAVAILABLE"
GRACEFUL_NO_KEY = "GRACEFUL_NO_KEY"


@dataclass(frozen=True)
class SourceCall:
    """Structured record of one external call. Written as JSON to intel.log."""
    source: str
    status: str
    latency_ms: int
    bytes: int
    http_code: int
    ts_utc: str
    reason: str = ""


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_call(source: str, status: str, latency_ms: int, bytes_: int = 0,
             http_code: int = 0, reason: str = "") -> None:
    """Append a JSON line to /app/data/intel.log. Best-effort, never raises."""
    rec = SourceCall(
        source=source,
        status=status,
        latency_ms=latency_ms,
        bytes=bytes_,
        http_code=http_code,
        ts_utc=now_utc_iso(),
        reason=reason[:200],
    )
    try:
        with INTEL_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec.__dict__, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        log.debug("intel_log write failed (non-fatal)")


# ── rate-limit guard (per-source, per-day) ────────────────────────────────
def _rate_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(RATE_DB_PATH), timeout=2.0)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS rate_calls("
        "source TEXT, day TEXT, count INTEGER, "
        "PRIMARY KEY(source, day))"
    )
    return conn


def daily_cap_for(source: str) -> int:
    """Read SOURCE_<UPPER>_DAILY_CAP env var; default 200."""
    key = f"SOURCE_{source.upper()}_DAILY_CAP"
    raw = os.getenv(key, "200").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 200


def under_cap(source: str) -> bool:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cap = daily_cap_for(source)
    try:
        with _rate_db() as conn:
            row = conn.execute(
                "SELECT count FROM rate_calls WHERE source=? AND day=?",
                (source, today),
            ).fetchone()
            count = row[0] if row else 0
    except Exception:
        return True  # fail-open if SQLite hiccups
    return count < cap


def bump_count(source: str) -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        with _rate_db() as conn:
            conn.execute(
                "INSERT INTO rate_calls(source, day, count) VALUES(?,?,1) "
                "ON CONFLICT(source, day) DO UPDATE SET count=count+1",
                (source, today),
            )
            row = conn.execute(
                "SELECT count FROM rate_calls WHERE source=? AND day=?",
                (source, today),
            ).fetchone()
            return row[0] if row else 1
    except Exception:
        return 0


# ── source state (LIVE/UNAVAILABLE transitions for flap alerts) ──────────
def _state_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(SOURCE_STATE_DB), timeout=2.0)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS source_state("
        "source TEXT PRIMARY KEY, status TEXT, last_change_utc TEXT, "
        "consecutive_fails INTEGER DEFAULT 0)"
    )
    return conn


def set_source_state(source: str, status: str) -> tuple[str, str]:
    """Update source status. Returns (prev_status, current_status). Best-effort."""
    now = now_utc_iso()
    try:
        with _state_db() as conn:
            row = conn.execute(
                "SELECT status, consecutive_fails FROM source_state WHERE source=?",
                (source,),
            ).fetchone()
            prev_status = row[0] if row else "UNKNOWN"
            prev_fails = row[1] if row else 0
            new_fails = prev_fails + 1 if status != LIVE else 0
            conn.execute(
                "INSERT INTO source_state(source, status, last_change_utc, consecutive_fails) "
                "VALUES(?,?,?,?) "
                "ON CONFLICT(source) DO UPDATE SET status=excluded.status, "
                "last_change_utc=excluded.last_change_utc, consecutive_fails=?",
                (source, status, now, new_fails, new_fails),
            )
            return prev_status, status
    except Exception:  # noqa: BLE001
        return "UNKNOWN", status


# ── HTTP helper with retries + observability ─────────────────────────────
async def get_json(
    source: str,
    url: str,
    *,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
) -> tuple[Optional[Any], dict[str, Any]]:
    """GET JSON with retry + structured logging. Returns (data, meta).

    meta = {status, http_code, latency_ms, bytes, reason}
    Data is None on failure. Caller decides degradation policy.
    """
    if not under_cap(source):
        log_call(source, "RATE_LIMITED", 0, 0, 0, "daily cap reached")
        return None, {"status": "RATE_LIMITED", "http_code": 0, "latency_ms": 0, "bytes": 0, "reason": "daily cap reached"}

    h = {"User-Agent": DEFAULT_UA, "Accept": "application/json"}
    if headers:
        h.update(headers)

    bump_count(source)
    last_err = ""
    last_code = 0

    for attempt in range(retries + 1):
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout, headers=h, follow_redirects=True) as client:
                r = await client.get(url, params=params)
                latency_ms = int((time.monotonic() - t0) * 1000)
                last_code = r.status_code
                if r.status_code == 200:
                    try:
                        data = r.json()
                    except Exception:
                        data = None
                        last_err = "non_json_body"
                        log_call(source, "DEGRADED", latency_ms, len(r.content), r.status_code, last_err)
                        return None, {"status": "DEGRADED", "http_code": r.status_code,
                                       "latency_ms": latency_ms, "bytes": len(r.content), "reason": last_err}
                    log_call(source, "LIVE", latency_ms, len(r.content), r.status_code, "")
                    set_source_state(source, LIVE)
                    return data, {"status": "LIVE", "http_code": r.status_code,
                                  "latency_ms": latency_ms, "bytes": len(r.content), "reason": ""}
                # non-200 — backoff before retry
                last_err = f"http_{r.status_code}"
                if r.status_code in (429, 502, 503, 504) and attempt < retries:
                    await asyncio.sleep(0.4 + random.random() * 0.6)
                    continue
                log_call(source, "DEGRADED", latency_ms, len(r.content), r.status_code, last_err)
                return None, {"status": "DEGRADED", "http_code": r.status_code,
                               "latency_ms": latency_ms, "bytes": len(r.content), "reason": last_err}
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
            last_err = type(e).__name__
            if attempt < retries:
                await asyncio.sleep(0.4 + random.random() * 0.6)
                continue
            latency_ms = int((time.monotonic() - t0) * 1000)
            log_call(source, "UNAVAILABLE", latency_ms, 0, 0, last_err)
            set_source_state(source, UNAVAILABLE)
            return None, {"status": "UNAVAILABLE", "http_code": 0, "latency_ms": latency_ms,
                          "bytes": 0, "reason": last_err}
        except Exception as e:  # noqa: BLE001
            last_err = type(e).__name__
            latency_ms = int((time.monotonic() - t0) * 1000)
            log_call(source, "UNAVAILABLE", latency_ms, 0, 0, last_err)
            set_source_state(source, UNAVAILABLE)
            return None, {"status": "UNAVAILABLE", "http_code": 0, "latency_ms": latency_ms,
                          "bytes": 0, "reason": last_err}
    return None, {"status": "UNAVAILABLE", "http_code": last_code, "latency_ms": 0, "bytes": 0, "reason": last_err}


async def get_text(
    source: str,
    url: str,
    *,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[Optional[str], dict[str, Any]]:
    """GET raw text (HTML/RSS/CSV) with retry + observability."""
    if not under_cap(source):
        log_call(source, "RATE_LIMITED", 0, 0, 0, "daily cap reached")
        return None, {"status": "RATE_LIMITED", "http_code": 0, "latency_ms": 0, "bytes": 0, "reason": "daily cap reached"}

    h = {"User-Agent": DEFAULT_UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    bump_count(source)
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=h, follow_redirects=True) as client:
            r = await client.get(url, params=params)
            latency_ms = int((time.monotonic() - t0) * 1000)
            if r.status_code == 200:
                log_call(source, "LIVE", latency_ms, len(r.content), r.status_code, "")
                set_source_state(source, LIVE)
                return r.text, {"status": "LIVE", "http_code": 200, "latency_ms": latency_ms,
                                "bytes": len(r.content), "reason": ""}
            log_call(source, "DEGRADED", latency_ms, len(r.content), r.status_code, f"http_{r.status_code}")
            return None, {"status": "DEGRADED", "http_code": r.status_code, "latency_ms": latency_ms,
                          "bytes": len(r.content), "reason": f"http_{r.status_code}"}
    except Exception as e:  # noqa: BLE001
        latency_ms = int((time.monotonic() - t0) * 1000)
        log_call(source, "UNAVAILABLE", latency_ms, 0, 0, type(e).__name__)
        set_source_state(source, UNAVAILABLE)
        return None, {"status": "UNAVAILABLE", "http_code": 0, "latency_ms": latency_ms, "bytes": 0,
                      "reason": type(e).__name__}


def graceful_no_key_payload(source: str, signup_url: str, env_var: str) -> dict:
    """Canonical payload when key is missing. Logged + state-set."""
    log_call(source, GRACEFUL_NO_KEY, 0, 0, 0, f"{env_var} not set")
    set_source_state(source, GRACEFUL_NO_KEY)
    return {
        "_global_error": f"{env_var} not set",
        "_signup_url": signup_url,
        "_status": GRACEFUL_NO_KEY,
        "series": [],
    }
