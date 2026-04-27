"""Round 16: bot version + uptime info for /version and /health."""
from __future__ import annotations

import logging
import os
import subprocess
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

START_TIME = time.time()
START_TIME_UTC = datetime.now(timezone.utc).isoformat()


def _git_sha() -> str:
    """Best-effort retrieval of the deployed commit SHA."""
    for name in ("GIT_COMMIT_SHA", "RAILWAY_GIT_COMMIT_SHA"):
        val = os.getenv(name, "").strip()
        if val:
            return val[:40]
    try:
        bot_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.check_output(
            ["git", "-C", bot_dir, "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return out.decode().strip()
    except Exception:  # noqa: BLE001
        return "(unknown)"


GIT_COMMIT_SHA = _git_sha()
DEPLOY_ID = os.getenv("RAILWAY_DEPLOYMENT_ID", "(local)")
SERVICE_NAME = os.getenv("RAILWAY_SERVICE_NAME", os.getenv("RAILWAY_PROJECT_NAME", "(local)"))


def uptime_seconds() -> int:
    return int(time.time() - START_TIME)


def format_uptime() -> str:
    sec = uptime_seconds()
    days, rem = divmod(sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _x_status_short() -> str:
    try:
        from modules.x_intel import get_api_stats, get_cache_state, X_LIVE_ENABLED
        stats = get_api_stats()
        cs = get_cache_state()
        live = "ON" if X_LIVE_ENABLED else "OFF"
        calls = stats.get("count", 0) if isinstance(stats, dict) else 0
        cache_ok = "✓" if (cs.get("last_success_at") if isinstance(cs, dict) else None) else "—"
        return f"live={live} calls_today={calls} cache={cache_ok}"
    except Exception:  # noqa: BLE001
        return "(unavailable)"


def _llm_short() -> str:
    """Tiny one-liner for /version. Best effort, never raises."""
    try:
        from config import ANTHROPIC_API_KEY, GEMINI_API_KEY, ANTHROPIC_MODEL
        bits = []
        if ANTHROPIC_API_KEY:
            bits.append(f"anthropic({ANTHROPIC_MODEL.split('-')[-1]})")
        if GEMINI_API_KEY:
            bits.append("gemini(free)")
        return " + ".join(bits) if bits else "no providers"
    except Exception:  # noqa: BLE001
        return "(unavailable)"


def format_version_block(commands_count: int) -> str:
    return (
        "🤖 BCDDU Bot — Round 16\n"
        f"Commit:  {GIT_COMMIT_SHA[:7]}\n"
        f"Deploy:  {DEPLOY_ID}\n"
        f"Service: {SERVICE_NAME}\n"
        f"Started: {START_TIME_UTC}\n"
        f"Uptime:  {format_uptime()}\n"
        f"Comandos: {commands_count}\n"
        f"\n"
        f"X API: {_x_status_short()}\n"
        f"LLM:   {_llm_short()}\n"
    )


def health_payload(commands_count: int) -> dict:
    """JSON payload for /health endpoint (Railway probe)."""
    return {
        "status": "ok",
        "commit": GIT_COMMIT_SHA[:7],
        "deploy_id": DEPLOY_ID,
        "service": SERVICE_NAME,
        "started_utc": START_TIME_UTC,
        "uptime_seconds": uptime_seconds(),
        "commands_registered": commands_count,
        "x_api": _x_status_short(),
        "llm": _llm_short(),
    }
