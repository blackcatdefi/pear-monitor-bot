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


def _intel_24h_calls() -> dict:
    try:
        from modules.intel_selftest import last_24h_call_summary
        s = last_24h_call_summary()
        return {"per_source": s, "total": sum(s.values())}
    except Exception:  # noqa: BLE001
        return {"per_source": {}, "total": 0}


def _cost_24h_usd() -> float:
    try:
        from modules.cost_tracker import cost_last_24h
        return round(cost_last_24h(), 4)
    except Exception:  # noqa: BLE001
        return 0.0


def _backup_last_run() -> dict:
    try:
        from modules.backup_volume import (
            get_last_backup_status,
            hours_since_last_backup,
        )
        status = get_last_backup_status() or {}
        return {
            "iso": status.get("iso", ""),
            "ok": bool(status.get("ok")),
            "tarball": status.get("tarball", ""),
            "hours_ago": hours_since_last_backup(),
        }
    except Exception:  # noqa: BLE001
        return {"iso": "", "ok": False, "tarball": "", "hours_ago": None}


def _selftest_summary() -> dict:
    try:
        from modules.intel_selftest import LAST_SELFTEST
        import json
        if not LAST_SELFTEST.exists():
            return {"counts": {}, "total": 0, "ts_utc": 0}
        with LAST_SELFTEST.open("r", encoding="utf-8") as fh:
            m = json.load(fh)
        return {
            "counts": m.get("counts", {}),
            "total": m.get("total", 0),
            "ts_utc": m.get("ts_utc", 0),
        }
    except Exception:  # noqa: BLE001
        return {"counts": {}, "total": 0, "ts_utc": 0}


def _pat_status_safe() -> dict:
    """Best-effort PAT expiry snapshot for /health — cached-only, no network."""
    try:
        from modules.pat_status import health_pat_block
        return health_pat_block()
    except Exception:  # noqa: BLE001
        return {"_error": "pat_status unavailable"}


def _cron_state_safe() -> dict:
    """Best-effort cron-state snapshot — never raises into /health."""
    try:
        from modules.cron_state import cron_state_payload
        return cron_state_payload()
    except Exception:  # noqa: BLE001
        return {"_error": "cron_state unavailable"}


def _pm_ltv_status() -> dict:
    """R-UNIFIED-LIQ — PM LTV parameters + official liquidation factors.

    Both liquidation thresholds now DERIVE from the max-borrow LTV
    (partial = LTV+(1−LTV)/2, full = LTV+(1−LTV)×2/3). PM_MAINT_LTV is an
    optional manual override of the partial factor (None = formula).
    """
    try:
        from config import PM_MAX_BORROW_LTV, PM_MAINT_LTV
    except Exception:  # noqa: BLE001
        PM_MAX_BORROW_LTV, PM_MAINT_LTV = 0.65, None
    try:
        from modules.portfolio_margin import (
            borrow_partial_liq_factor, borrow_full_liq_factor,
            _liq_threshold_for_ltv,
        )
        partial = _liq_threshold_for_ltv(PM_MAX_BORROW_LTV)
        full = borrow_full_liq_factor(PM_MAX_BORROW_LTV)
        derived = borrow_partial_liq_factor(PM_MAX_BORROW_LTV)
    except Exception:  # noqa: BLE001
        partial = full = derived = 0.0
    out = {
        "max_borrow_ltv": float(PM_MAX_BORROW_LTV),
        "liq_model": "official_hl_pm_borrow",
        "partial_liq_factor": float(partial),
        "full_liq_factor": float(full),
        "maint_ltv_override": (
            float(PM_MAINT_LTV) if PM_MAINT_LTV is not None else None
        ),
        "partial_factor_derived": float(derived),
    }
    try:
        from modules.hl_borrow_lend import maint_ltv_verification_status
        st = maint_ltv_verification_status()
        out["maint_verified_live"] = bool(st.get("verified"))
        if st.get("live_maint") is not None:
            out["maint_live_value"] = st["live_maint"]
        if not st.get("verified"):
            out["warning"] = (
                "maint LTV NOT verificable live (HL API); liq math derivada "
                f"de la fórmula oficial (parcial={float(partial):.4f})"
            )
    except Exception:  # noqa: BLE001
        out["maint_verified_live"] = False
        out["warning"] = "maint LTV verification unavailable"
    return out


def _x_source_safe() -> str:
    """Effective X transport for /health — best effort, never raises."""
    try:
        from modules.x_provider import backend_name
        return backend_name()
    except Exception:  # noqa: BLE001
        return "(unavailable)"


def _ledger_status_safe() -> dict:
    """R-LEDGER-FIX post-deploy telemetry: schema migration, semantics
    version, leverage provenance and degraded wallets — the state that had
    to be verifiable from outside the box for a ledger bug to be caught
    before it reaches a report. Aggregates only, never per-leg detail."""
    try:
        from modules.trade_ledger import ledger_diagnostics
        return ledger_diagnostics()
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": "ledger diagnostics unavailable"}


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
        # R-UNIFIED-LIQ Phase B: effective X transport (twitterapi_io|official).
        "x_source": _x_source_safe(),
        "llm": _llm_short(),
        # R-PERFECT Phase 3 §EXIT
        "intel_24h_calls": _intel_24h_calls(),
        "cost_24h_usd": _cost_24h_usd(),
        "backup_last_run": _backup_last_run(),
        "selftest_last": _selftest_summary(),
        # R-ONDEMAND (2026-05-09): proactive cron gates + safety thresholds.
        "cron_state": _cron_state_safe(),
        # R-PAT-RENEW (2026-05-20): GitHub PAT expiry telemetry.
        "pat_status": _pat_status_safe(),
        # R-LTV65-QUIET (2026-08-11): PM LTV params + maint verification.
        "pm_ltv": _pm_ltv_status(),
        # R-LEDGER-FIX (2026-09-01): ledger schema/health telemetry.
        "ledger": _ledger_status_safe(),
    }
