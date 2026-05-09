"""R-ONDEMAND (2026-05-09) — runtime cron-state introspection.

Single source of truth for the on-demand-only gating environment variables.
Surfaced through ``/health`` so BCD can verify post-deploy that the bot is
actually silent (no proactive auto-fires) without grepping Railway env vars.

Spec:

    REPORT_CRON_ENABLED       gate weekly/morning brief auto-broadcasts
    TESIS_CRON_ENABLED        gate R18 thesis-style proactive pushes
                              (macro_convergence, predictive_alerts,
                              compounding_detector)
    INTEL_AUTOPULL_ENABLED    gate intel_processor + x_timeline_cache +
                              lmec_counter_refresh background fetchers
    CATALYST_NUDGE_ENABLED    gate macro_calendar T-24/T-2/T-30 nudges +
                              R18 pre_event_brief + cryexc_monitor
    SELFTEST_CRON_ENABLED     INFRA — keep on (4x/day source flap probe)
    BACKUP_VOLUME_ENABLED     INFRA — keep on (daily 04:00 UTC tarball)
    COST_ALERTS_ENABLED       SAFETY — keep on (hourly LLM cost ceiling)
    SOURCE_ALERTS_ENABLED     SAFETY — keep on (>6h flap detector)
    HF_PRELIQ_ENABLED         SAFETY — keep on; gates auto.hf_alert_gate

Defaults stay ``true`` so existing deploys do not silently change behavior
on rollout. The R-ONDEMAND switch happens at the env-var layer in Railway,
not in code defaults.
"""
from __future__ import annotations

import os
from typing import Any


def _flag(name: str, default: str = "true") -> bool:
    raw = os.getenv(name, default)
    return str(raw).strip().lower() != "false"


# ---------- Gates that the R-ONDEMAND round flips off ----------

def report_cron_enabled() -> bool:
    """Gate for /reporte-style proactive broadcasts.

    Disables the R21 morning_brief anchor message and the R17 weekly_summary
    Sunday-evening recap. Slash command ``/reporte`` keeps working unchanged
    when invoked by BCD directly — this only kills the cron emitters.
    """
    return _flag("REPORT_CRON_ENABLED", "true")


def tesis_cron_enabled() -> bool:
    """Gate for /tesis auto-update + R18 thesis-style proactive broadcasts.

    Disables R18 macro_convergence (60min), predictive_alerts (30min) and
    compounding_detector (5min). The slash command ``/tesis`` continues to
    return the latest ``thesis_state.json`` snapshot on demand.
    """
    return _flag("TESIS_CRON_ENABLED", "true")


def intel_autopull_enabled() -> bool:
    """Gate for background intel pull jobs.

    Disables ``_intel_processor_job`` (30min), ``_x_timeline_cache_job`` and
    ``_lmec_counter_refresh_job``. On-demand intel commands (``/intel``,
    ``/timeline``, ``/intel30_full`` …) continue to fetch live.
    """
    return _flag("INTEL_AUTOPULL_ENABLED", "true")


def catalyst_nudge_enabled() -> bool:
    """Gate for proactive catalyst nudges (T-24h / T-2h / T-30m / T+post15).

    Disables ``_macro_calendar_job`` (1min) and R18 pre_event_brief (5min).
    Critical-only catalyst alerts (e.g. dilución >50% < 72h) live in the
    kill_triggers/predictive paths — not affected here.
    """
    return _flag("CATALYST_NUDGE_ENABLED", "true")


# ---------- Gates that stay alive (infra + safety) ----------

def selftest_cron_enabled() -> bool:
    return _flag("SELFTEST_CRON_ENABLED", "true")


def backup_volume_enabled() -> bool:
    return _flag("BACKUP_VOLUME_ENABLED", "true")


def cost_alerts_enabled() -> bool:
    return _flag("COST_ALERTS_ENABLED", "true")


def source_alerts_enabled() -> bool:
    return _flag("SOURCE_ALERTS_ENABLED", "true")


def hf_preliq_enabled() -> bool:
    """Master kill-switch for the HF pre-liquidation alert path.

    The actual gate logic lives in ``auto.hf_alert_gate``. This flag is the
    operator-visible toggle so /health can show whether the safety net is
    armed. Default true; setting false should only happen during planned
    maintenance with BCD's explicit say-so.
    """
    return _flag("HF_PRELIQ_ENABLED", "true")


# ---------- /health surface ----------

def margin_stress_threshold_pct() -> float:
    """``MARGIN_STRESS_ALERT_PCT`` (default 90).

    Threshold at which ``modules.alerts`` raises a pre-margin-call alert
    when ``total_margin_used / account_value`` crosses on any active perp
    wallet. Edge-triggered: clears when ratio drops below threshold.
    """
    raw = os.getenv("MARGIN_STRESS_ALERT_PCT", "90").strip()
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 90.0
    # clamp to [50, 100] — anything outside is a typo, not a policy choice
    return max(50.0, min(100.0, v))


def margin_stress_enabled() -> bool:
    """Master enable for the new R-ONDEMAND margin-stress alert path."""
    return _flag("MARGIN_STRESS_ALERT_ENABLED", "true")


def cron_state_payload() -> dict[str, Any]:
    """Serializable snapshot of every cron gate, for /health.

    Splits gates into 'on_demand_only' (the four R-ONDEMAND flags), 'infra'
    (selftest/backup/cost/source) and 'safety' (HF preliq + margin stress).
    """
    return {
        "on_demand_only": {
            "report_cron": report_cron_enabled(),
            "tesis_cron": tesis_cron_enabled(),
            "intel_autopull": intel_autopull_enabled(),
            "catalyst_nudge": catalyst_nudge_enabled(),
        },
        "infra": {
            "selftest_cron": selftest_cron_enabled(),
            "backup_volume": backup_volume_enabled(),
            "cost_alerts": cost_alerts_enabled(),
            "source_alerts": source_alerts_enabled(),
        },
        "safety": {
            "hf_preliq": hf_preliq_enabled(),
            "margin_stress_alert": margin_stress_enabled(),
            "margin_stress_threshold_pct": margin_stress_threshold_pct(),
        },
    }
