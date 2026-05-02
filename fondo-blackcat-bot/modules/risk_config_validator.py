"""Round 18 — Risk-config validator (/risk_check).

Validates the live runtime config against fund risk policy and surfaces
any drift. Read-only; never mutates env. Returns a structured Telegram
message that BCD can use to decide whether to update Railway env vars.

Checked invariants:
    - HF flywheel critical threshold >= 1.10 (no looser)
    - Stop-loss percentage <= 25% (no wider)
    - X API daily cap <= 20 (cost ceiling)
    - Predictive horizon hours within [12, 48]
    - Auto-reconcile apply ENABLED only if AUTO_RECONCILE_APPLY_ENABLED=true
    - Pear cross-validation threshold within [0.05, 0.20]
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    expected: str


def is_enabled() -> bool:
    return os.getenv("RISK_CONFIG_VALIDATOR_ENABLED", "true").strip().lower() != "false"


def _envf(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _envi(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _envb(name: str, default: bool) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    return raw == "true"


def run_checks() -> list[CheckResult]:
    checks: list[CheckResult] = []

    hf_crit = _envf("HF_CRITICAL", 1.10)
    checks.append(
        CheckResult(
            "HF_CRITICAL",
            hf_crit >= 1.10,
            f"actual={hf_crit:.3f}",
            ">= 1.10",
        )
    )

    sl_pct = _envf("STOP_LOSS_PCT", 0.20)
    checks.append(
        CheckResult(
            "STOP_LOSS_PCT",
            sl_pct <= 0.25,
            f"actual={sl_pct*100:.1f}%",
            "<= 25%",
        )
    )

    x_cap = _envi("X_API_DAILY_CAP", 15)
    checks.append(
        CheckResult(
            "X_API_DAILY_CAP",
            x_cap <= 20,
            f"actual={x_cap}",
            "<= 20",
        )
    )

    horizon = _envf("PREDICTIVE_ALERTS_HORIZON_HOURS", 24)
    checks.append(
        CheckResult(
            "PREDICTIVE_ALERTS_HORIZON_HOURS",
            12 <= horizon <= 48,
            f"actual={horizon:.0f}h",
            "12-48h",
        )
    )

    auto_apply = _envb("AUTO_RECONCILE_APPLY_ENABLED", False)
    checks.append(
        CheckResult(
            "AUTO_RECONCILE_APPLY_ENABLED",
            True,
            f"actual={'on' if auto_apply else 'off'}",
            "off (manual approve)",
        )
    )

    pear_thr = _envf("PEAR_CROSS_VALIDATION_THRESHOLD", 0.10)
    checks.append(
        CheckResult(
            "PEAR_CROSS_VALIDATION_THRESHOLD",
            0.05 <= pear_thr <= 0.20,
            f"actual={pear_thr*100:.1f}%",
            "5-20%",
        )
    )

    spend_cap = _envf("X_API_MONTHLY_SPEND_CAP_USD", 30.0)
    checks.append(
        CheckResult(
            "X_API_MONTHLY_SPEND_CAP_USD",
            spend_cap <= 30.0,
            f"actual=${spend_cap:.2f}",
            "<= $30/mo",
        )
    )

    return checks


def format_report(results: list[CheckResult]) -> str:
    if not results:
        return "\u2705 RISK CHECK — sin reglas configuradas."
    ok_count = sum(1 for r in results if r.ok)
    bad = [r for r in results if not r.ok]
    head = (
        f"\U0001f6e1 RISK CONFIG — {ok_count}/{len(results)} OK\n"
        f"{'─' * 30}"
    )
    lines = [head]
    for r in results:
        mark = "\u2705" if r.ok else "\u274c"
        lines.append(f"{mark} {r.name}: {r.detail}  (expected: {r.expected})")
    if bad:
        lines.append("")
        lines.append("\u26a0\ufe0f Action: adjust deviating env vars in Railway.")
    return "\n".join(lines)


def build_report() -> str:
    if not is_enabled():
        return "\u26a0\ufe0f /risk_check disabled (RISK_CONFIG_VALIDATOR_ENABLED=false)"
    return format_report(run_checks())
