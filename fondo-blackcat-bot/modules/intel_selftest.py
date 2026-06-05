"""R-PERFECT Phase 3 #9 — /selftest command + source-status snapshotter.

Fans out fetch_all() across all 30 intel30 modules in parallel with a 10s
per-module timeout. Returns a status matrix: LIVE / GRACEFUL_NO_KEY /
DEGRADED / UNAVAILABLE / TIMEOUT.

Replaces the manual smoke_phase1.py runner. /selftest is also called by the
4x-daily scheduler (Fase 4 stress test) so the operator gets passive
liveness telemetry without re-running it manually.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PER_MODULE_TIMEOUT = 10.0
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
LAST_SELFTEST = DATA_DIR / "selftest_last.json"

# P1.9: sources that are OPTIONAL by design — they have no free public API or
# require a key the fund hasn't provisioned, so a non-LIVE result is EXPECTED
# and must NOT count as a selftest failure.
#   • arkham_intel — ARKHAM_API_KEY not provisioned (free signup optional)
#   • asxn_data    — no public API (dashboard is a client-rendered SPA)
#   • hypurrscan   — /api/auctions returns 404 (endpoint retired upstream)
OPTIONAL_SOURCES = frozenset({"arkham_intel", "asxn_data", "hypurrscan"})

# Statuses that mean "working as expected" for pass-count purposes. OPTIONAL
# sources are folded in via _is_healthy below regardless of their raw status.
HEALTHY_STATUSES = frozenset({"LIVE", "GRACEFUL_NO_KEY", "DEGRADED", "OPTIONAL"})


def _is_healthy(row: dict[str, Any]) -> bool:
    """A source counts toward the pass total if it is LIVE / gracefully
    keyless / degraded-but-handled, OR if it is an OPTIONAL-by-design source
    (any non-crashing status). Only hard failures (TIMEOUT / EXCEPTION /
    IMPORT_FAIL / BAD_SHAPE / UNAVAILABLE / EMPTY / UNKNOWN on a REQUIRED
    source) count against the total."""
    if row.get("optional"):
        return row.get("status") != "IMPORT_FAIL"  # import failure is always a fail
    return row.get("status") in HEALTHY_STATUSES


async def _fetch_one(name: str) -> dict[str, Any]:
    """Run one module's fetch_all under timeout. Always returns a dict."""
    t0 = time.monotonic()
    try:
        mod = __import__(f"modules.intel30.{name}", fromlist=["fetch_all"])
    except Exception as e:  # noqa: BLE001
        return {
            "name": name, "status": "IMPORT_FAIL",
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "reason": str(e)[:80],
        }
    try:
        data = await asyncio.wait_for(mod.fetch_all(), timeout=PER_MODULE_TIMEOUT)
    except asyncio.TimeoutError:
        return {
            "name": name, "status": "TIMEOUT",
            "latency_ms": int(PER_MODULE_TIMEOUT * 1000),
            "reason": f">{PER_MODULE_TIMEOUT}s",
        }
    except Exception as e:  # noqa: BLE001
        return {
            "name": name, "status": "EXCEPTION",
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "reason": type(e).__name__ + ": " + str(e)[:60],
        }
    latency_ms = int((time.monotonic() - t0) * 1000)
    return _classify(name, data, latency_ms)


def _classify(name: str, data: Any, latency_ms: int) -> dict[str, Any]:
    """Map a module's fetch_all return into a status entry."""
    entry = _classify_inner(name, data, latency_ms)
    # P1.9: tag optional-by-design sources and normalise their non-LIVE
    # statuses to OPTIONAL so they read clean and never count as failures.
    if name in OPTIONAL_SOURCES:
        entry["optional"] = True
        if entry.get("status") not in ("LIVE", "IMPORT_FAIL"):
            entry["status"] = "OPTIONAL"
    return entry


def _classify_inner(name: str, data: Any, latency_ms: int) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {"name": name, "status": "BAD_SHAPE", "latency_ms": latency_ms, "reason": "non-dict return"}
    if data.get("_status") == "GRACEFUL_NO_KEY":
        return {"name": name, "status": "GRACEFUL_NO_KEY", "latency_ms": latency_ms,
                "reason": data.get("_global_error", "")}
    ge = data.get("_global_error")
    if isinstance(ge, str) and ge:
        # SPA / no JSON / unreachable
        if any(token in ge for token in ("spa_", "html_only", "moved", "404")):
            return {"name": name, "status": "DEGRADED", "latency_ms": latency_ms, "reason": ge[:80]}
        if "not set" in ge or "API_KEY" in ge:
            return {"name": name, "status": "GRACEFUL_NO_KEY", "latency_ms": latency_ms, "reason": ge[:80]}
        return {"name": name, "status": "UNAVAILABLE", "latency_ms": latency_ms, "reason": ge[:80]}
    series_keys = ("series", "variables", "data", "rows", "items", "results",
                   "etfs", "flows", "markets", "orgs")
    series = None
    for k in series_keys:
        if k in data:
            series = data[k]
            break
    if isinstance(series, list):
        ok_rows = 0
        for r in series:
            if not isinstance(r, dict):
                continue
            if r.get("_error"):
                continue
            # any non-underscore-prefixed key counts as a data row
            payload_keys = [k for k in r.keys() if not str(k).startswith("_")]
            if payload_keys:
                ok_rows += 1
        if ok_rows > 0:
            return {"name": name, "status": "LIVE", "latency_ms": latency_ms,
                    "reason": f"{ok_rows} ok rows"}
        return {"name": name, "status": "EMPTY", "latency_ms": latency_ms, "reason": "no parsed rows"}
    # any top-level scalar/dict payload (excluding _-prefixed) also counts
    non_meta = {k: v for k, v in data.items() if not str(k).startswith("_")}
    if non_meta:
        return {"name": name, "status": "LIVE", "latency_ms": latency_ms,
                "reason": f"{len(non_meta)} payload keys"}
    return {"name": name, "status": "UNKNOWN", "latency_ms": latency_ms, "reason": "no series"}


async def run_selftest() -> dict[str, Any]:
    """Run all 30 intel30 modules in parallel and return matrix dict."""
    from modules.intel30 import ALL_MODULES
    coros = [_fetch_one(n) for n in ALL_MODULES]
    rows = await asyncio.gather(*coros, return_exceptions=False)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    # P1.9: pass-count excludes optional-source non-failures. ``healthy`` is
    # the number reported as the selftest pass count (LIVE/keyless/degraded/
    # optional); ``failures`` are REQUIRED sources that genuinely broke.
    healthy = sum(1 for r in rows if _is_healthy(r))
    failures = [r["name"] for r in rows if not _is_healthy(r)]
    matrix = {
        "ts_utc": int(time.time()),
        "rows": rows,
        "counts": counts,
        "total": len(rows),
        "healthy": healthy,
        "failures": failures,
    }
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with LAST_SELFTEST.open("w", encoding="utf-8") as fh:
            json.dump(matrix, fh)
    except Exception:  # noqa: BLE001
        log.debug("selftest snapshot write failed (non-fatal)")
    return matrix


def format_matrix(matrix: dict[str, Any]) -> str:
    total = matrix.get("total", 0)
    healthy = matrix.get("healthy")
    head = f"🩺 *Selftest — {total} sources*"
    if healthy is not None:
        head += f" · sanos {healthy}/{total}"
    lines = [head]
    counts = matrix.get("counts", {})
    summary_bits = [f"{k}={v}" for k, v in sorted(counts.items())]
    lines.append("  · " + " · ".join(summary_bits))
    fails = matrix.get("failures") or []
    if fails:
        lines.append("  ⚠️ requeridas caídas: " + ", ".join(fails))
    else:
        lines.append("  ✅ sin fuentes requeridas caídas (opcionales no cuentan)")
    rows = matrix.get("rows", [])
    rows.sort(key=lambda r: (r.get("status", ""), r.get("name", "")))
    for r in rows:
        name = r.get("name", "?")
        status = r.get("status", "?")
        latency = r.get("latency_ms", 0)
        reason = (r.get("reason") or "")[:50]
        lines.append(f"  · `{name:18s}` {status:18s} {latency:5d}ms {reason}")
    return "\n".join(lines)


def format_source_status() -> str:
    """Read LAST_SELFTEST and render as Telegram message."""
    if not LAST_SELFTEST.exists():
        return "ℹ️ no selftest snapshot yet — run /selftest"
    try:
        with LAST_SELFTEST.open("r", encoding="utf-8") as fh:
            matrix = json.load(fh)
    except Exception as e:  # noqa: BLE001
        return f"⚠️ selftest snapshot unreadable: {e}"
    return format_matrix(matrix)


def last_24h_call_summary() -> dict[str, int]:
    """Read /app/data/intel.log, return per-source call count last 24h."""
    from modules.intel30._intel_base import INTEL_LOG_PATH
    if not INTEL_LOG_PATH.exists():
        return {}
    cutoff = time.time() - 86400
    counts: dict[str, int] = {}
    try:
        with INTEL_LOG_PATH.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts_str = rec.get("ts_utc", "")
                # parse iso → unix
                from datetime import datetime
                try:
                    ts = datetime.fromisoformat(ts_str).timestamp()
                except (TypeError, ValueError):
                    continue
                if ts < cutoff:
                    continue
                src = rec.get("source", "?")
                counts[src] = counts.get(src, 0) + 1
    except Exception as e:  # noqa: BLE001
        log.debug("intel log read failed: %s", e)
    return counts
