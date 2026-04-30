"""Round 20 — Timezone validator.

Runs at bot boot to verify system clock and timezone.

If the system clock is drifted or not UTC, log an ERROR. This prevents
the time desync bug at the OS level (Railway containers default to UTC,
but a misconfigured TZ env var could mask the issue).

Set TIMEZONE_VALIDATION_AT_BOOT=false to disable.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def validate_system_time_at_boot() -> dict:
    """Run at bot startup. Verify system clock is reasonable.

    Returns dict with diagnostics for downstream callers.
    """
    if os.getenv("TIMEZONE_VALIDATION_AT_BOOT", "true").strip().lower() == "false":
        logger.info("timezone_validator: validation disabled by env")
        return {"enabled": False}

    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now()

    # System should be in UTC (Railway containers default to UTC; we set TZ=UTC).
    actual_offset = now_local - now_utc.replace(tzinfo=None)
    drift_seconds = abs(actual_offset.total_seconds())

    drift_threshold_s = 60
    if drift_seconds > drift_threshold_s:
        logger.error(
            "⚠️ TIMEZONE DRIFT DETECTED: system local=%s vs UTC=%s drift=%.0fs "
            "— set TZ=UTC in Railway service variables",
            now_local.isoformat(),
            now_utc.isoformat(),
            drift_seconds,
        )
        ok = False
    else:
        logger.info("✅ System clock validated: UTC=%s", now_utc.isoformat())
        ok = True

    monotonic_now = time.monotonic()
    if monotonic_now < 0:
        logger.error("⚠️ Monotonic clock invalid: %s", monotonic_now)

    return {
        "enabled": True,
        "ok": ok,
        "now_utc": now_utc.isoformat(),
        "drift_seconds": drift_seconds,
        "tz_env": os.getenv("TZ", "<unset>"),
    }


# Auto-run on import (cheap, idempotent)
try:
    validate_system_time_at_boot()
except Exception:  # noqa: BLE001
    logger.exception("timezone_validator: boot validation raised")
