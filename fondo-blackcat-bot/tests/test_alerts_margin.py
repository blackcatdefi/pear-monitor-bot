"""R-BOT-DEFINITIVE WI-3 — margin alert redesign tests (anti-spam + copy)."""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def am(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import config
    importlib.reload(config)
    import modules.alerts_margin as alerts_margin
    importlib.reload(alerts_margin)
    return alerts_margin


def test_bands(am):
    assert am.margin_used_band(50.0) == 0
    assert am.margin_used_band(95.0) == 1
    assert am.margin_used_band(104.6) == 2
    assert am.margin_used_band(115.0) == 3


def test_copy_is_honest(am):
    msg = am.format_margin_used_alert("Trading (0xc7…1505)", 104.6, 21000, 20000)
    assert "PERP MARGIN USED vs PERP EQUITY" in msg
    assert "bloquea ABRIR posiciones nuevas" in msg
    assert "NO es proximidad de liquidación" in msg
    low = msg.lower()
    assert "buffer to liquidation" not in low
    assert "buffer a liquidación" not in low


def test_replay_same_value_fires_exactly_once(am):
    """WI-3 acceptance: replay current values → exactly one message."""
    t0 = 1_000_000.0
    fired = 0
    for i in range(6):  # six 30-min cycles at the same 104.6%
        should, _ = am.evaluate_margin_used("w1", 104.6, now=t0 + i * 1800)
        fired += 1 if should else 0
    assert fired == 1


def test_band_transition_fires(am):
    t0 = 1_000_000.0
    s1, _ = am.evaluate_margin_used("w2", 95.0, now=t0)
    assert s1 is True  # first observation in 90-100 band
    s2, _ = am.evaluate_margin_used("w2", 104.0, now=t0 + 60)
    assert s2 is True  # 90-100 → 100-110 transition (no cooldown gate on band jump? cooled=False)


def test_worsening_over_5pp_fires_after_cooldown(am):
    t0 = 1_000_000.0
    am.evaluate_margin_used("w3", 92.0, now=t0)
    # +6pp within the same band but inside cooldown → silent.
    s_in, _ = am.evaluate_margin_used("w3", 98.5, now=t0 + 600)
    assert s_in is False
    # Same worsening after the 6h cooldown → fires.
    s_out, _ = am.evaluate_margin_used("w3", 98.5, now=t0 + am.COOLDOWN_SEC + 700)
    assert s_out is True


def test_improvement_is_silent_and_rearms(am):
    t0 = 1_000_000.0
    am.evaluate_margin_used("w4", 104.0, now=t0)
    s_down, _ = am.evaluate_margin_used("w4", 85.0, now=t0 + 60)
    assert s_down is False  # improving transition never alerts
    # New breach after recovery + cooldown → fires again.
    s_again, _ = am.evaluate_margin_used("w4", 104.0, now=t0 + am.COOLDOWN_SEC + 120)
    assert s_again is True


def test_below_90_never_fires(am):
    for i, v in enumerate((10.0, 50.0, 89.9)):
        should, _ = am.evaluate_margin_used("w5", v, now=1_000_000.0 + i * 3600)
        assert should is False


def test_hf_crossings(am):
    t0 = 1_000_000.0
    s0, _ = am.evaluate_pm_hf(1.50, now=t0)
    assert s0 is False
    s1, m1 = am.evaluate_pm_hf(1.25, now=t0 + 60)
    assert s1 is True and "1.30" in m1  # crossed 1.30 → info tier
    s1b, _ = am.evaluate_pm_hf(1.24, now=t0 + 120)
    assert s1b is False  # same band, no re-fire
    s2, m2 = am.evaluate_pm_hf(1.15, now=t0 + 180)
    assert s2 is True and "1.20" in m2  # crossed 1.20 → observación
    s3, m3 = am.evaluate_pm_hf(1.05, now=t0 + 240)
    assert s3 is True and "1.10" in m3  # crossed 1.10 → acción
    # The real-risk playbook NEVER suggests selling HYPE.
    assert "NUNCA vender HYPE" in m3


def test_liq_distance_crossings(am):
    t0 = 1_000_000.0
    s0, _ = am.evaluate_position_liq_distance("HOOD", 20.0, now=t0)
    assert s0 is False
    s1, m1 = am.evaluate_position_liq_distance("HOOD", 11.0, now=t0 + 60)
    assert s1 is True and "<12%" in m1
    s1b, _ = am.evaluate_position_liq_distance("HOOD", 10.5, now=t0 + 120)
    assert s1b is False  # same band
    s2, m2 = am.evaluate_position_liq_distance("HOOD", 7.0, now=t0 + 180)
    assert s2 is True and "<8%" in m2


def test_legacy_alert_copy_cleaned():
    """The legacy alerts path must not carry liquidation wording either."""
    import inspect
    import modules.alerts as alerts
    src = inspect.getsource(alerts._run_margin_stress_alerts)
    assert "Buffer to liquidation" not in src
    assert "bloquea ABRIR posiciones nuevas" in src
