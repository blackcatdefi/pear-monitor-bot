"""R-ONDEMAND (2026-05-09) — coverage for cron_state gates + /health surface.

Verifies each on-demand gate honors its env var, infra/safety gates default
to ``true`` (so a fresh deploy doesn't go silent), the threshold clamp is
sane, and the /health payload includes a structured cron_state block.
"""
from __future__ import annotations

import importlib

import pytest


GATES = [
    ("REPORT_CRON_ENABLED", "report_cron_enabled"),
    ("TESIS_CRON_ENABLED", "tesis_cron_enabled"),
    ("INTEL_AUTOPULL_ENABLED", "intel_autopull_enabled"),
    ("CATALYST_NUDGE_ENABLED", "catalyst_nudge_enabled"),
    ("SELFTEST_CRON_ENABLED", "selftest_cron_enabled"),
    ("BACKUP_VOLUME_ENABLED", "backup_volume_enabled"),
    ("COST_ALERTS_ENABLED", "cost_alerts_enabled"),
    ("SOURCE_ALERTS_ENABLED", "source_alerts_enabled"),
    ("HF_PRELIQ_ENABLED", "hf_preliq_enabled"),
]


@pytest.fixture
def cron_state(monkeypatch):
    """Reload the module per test to pick up monkeypatched env vars."""
    # Clear all the env vars we care about so each test starts clean.
    for env, _ in GATES:
        monkeypatch.delenv(env, raising=False)
    monkeypatch.delenv("MARGIN_STRESS_ALERT_PCT", raising=False)
    monkeypatch.delenv("MARGIN_STRESS_ALERT_ENABLED", raising=False)
    import modules.cron_state as cs
    return importlib.reload(cs)


@pytest.mark.parametrize("env_name,fn_name", GATES)
def test_default_true(cron_state, env_name, fn_name):
    """All gates default to True so we never silently flip behavior on rollout."""
    assert getattr(cron_state, fn_name)() is True


@pytest.mark.parametrize("env_name,fn_name", GATES)
def test_explicit_false(cron_state, env_name, fn_name, monkeypatch):
    monkeypatch.setenv(env_name, "false")
    import modules.cron_state as cs
    importlib.reload(cs)
    assert getattr(cs, fn_name)() is False


@pytest.mark.parametrize("env_name,fn_name", GATES)
def test_case_insensitive(cron_state, env_name, fn_name, monkeypatch):
    monkeypatch.setenv(env_name, "FALSE")
    import modules.cron_state as cs
    importlib.reload(cs)
    assert getattr(cs, fn_name)() is False


def test_margin_stress_default_threshold(cron_state):
    assert cron_state.margin_stress_threshold_pct() == 90.0


def test_margin_stress_threshold_override(monkeypatch):
    monkeypatch.setenv("MARGIN_STRESS_ALERT_PCT", "85")
    import modules.cron_state as cs
    importlib.reload(cs)
    assert cs.margin_stress_threshold_pct() == 85.0


def test_margin_stress_threshold_clamps_low(monkeypatch):
    """A typo like 9 (probably meant 90) clamps to the floor instead of
    silently disabling the alert by setting it to 9%."""
    monkeypatch.setenv("MARGIN_STRESS_ALERT_PCT", "9")
    import modules.cron_state as cs
    importlib.reload(cs)
    assert cs.margin_stress_threshold_pct() == 50.0


def test_margin_stress_threshold_clamps_high(monkeypatch):
    monkeypatch.setenv("MARGIN_STRESS_ALERT_PCT", "200")
    import modules.cron_state as cs
    importlib.reload(cs)
    assert cs.margin_stress_threshold_pct() == 100.0


def test_margin_stress_threshold_garbage(monkeypatch):
    monkeypatch.setenv("MARGIN_STRESS_ALERT_PCT", "not-a-number")
    import modules.cron_state as cs
    importlib.reload(cs)
    assert cs.margin_stress_threshold_pct() == 90.0


def test_payload_shape(cron_state):
    payload = cron_state.cron_state_payload()
    assert set(payload.keys()) == {"on_demand_only", "infra", "safety"}
    assert set(payload["on_demand_only"].keys()) == {
        "report_cron", "tesis_cron", "intel_autopull", "catalyst_nudge",
    }
    assert set(payload["infra"].keys()) == {
        "selftest_cron", "backup_volume", "cost_alerts", "source_alerts",
    }
    assert set(payload["safety"].keys()) == {
        "hf_preliq", "margin_stress_alert", "margin_stress_threshold_pct",
    }


def test_payload_reflects_ondemand_flip(monkeypatch):
    """Spec smoke: when all four R-ONDEMAND vars are false, the payload's
    on_demand_only block goes all-false but infra/safety stays all-true."""
    for env in ("REPORT_CRON_ENABLED", "TESIS_CRON_ENABLED",
                "INTEL_AUTOPULL_ENABLED", "CATALYST_NUDGE_ENABLED"):
        monkeypatch.setenv(env, "false")
    import modules.cron_state as cs
    importlib.reload(cs)
    payload = cs.cron_state_payload()
    assert all(v is False for v in payload["on_demand_only"].values())
    assert all(payload["infra"][k] is True for k in payload["infra"])
    assert payload["safety"]["hf_preliq"] is True
    assert payload["safety"]["margin_stress_alert"] is True


def test_health_payload_includes_cron_state(monkeypatch):
    """Regression guard: /health must surface cron_state so BCD can verify
    post-deploy that the bot is silent without grepping Railway env vars."""
    import modules.version_info as vi
    importlib.reload(vi)
    payload = vi.health_payload(commands_count=42)
    assert "cron_state" in payload
    assert "on_demand_only" in payload["cron_state"]
