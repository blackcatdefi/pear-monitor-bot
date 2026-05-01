"""R-SILENT — tests for ``auto.hf_alert_gate``.

Cover the four-band severity classifier + dedup window:

  * HF >= 1.10 → silent (no emit, no record).
  * 1.05 <= HF < 1.10 → warn band; first cross emits, repeat with small
    delta within DEDUP_MIN suppressed, repeat with big delta or after
    window emits again.
  * 1.02 <= HF < 1.05 → critical band; same dedup logic but priority over
    warn.
  * HF < 1.02 → preliq; fires every PRELIQ_REPEAT_MIN minutes.
  * recovery clears state (caller responsibility — ``clear_wallet``).

Determinism: all decisions inject ``now``; SQLite path is monkeypatched
into ``tmp_path``.
"""
from __future__ import annotations

import os

import pytest

from auto import hf_alert_gate as hfg


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    db = tmp_path / "hf_alerts.db"
    monkeypatch.setattr(hfg, "_db_path", lambda: str(db))
    monkeypatch.setattr(hfg, "ENABLED", True)
    monkeypatch.setattr(hfg, "THRESHOLD", 1.10)
    monkeypatch.setattr(hfg, "CRITICAL", 1.05)
    monkeypatch.setattr(hfg, "PRELIQ", 1.02)
    monkeypatch.setattr(hfg, "DEDUP_MIN", 120)
    monkeypatch.setattr(hfg, "DEDUP_DELTA", 0.05)
    monkeypatch.setattr(hfg, "PRELIQ_REPEAT_MIN", 5)
    yield


def test_healthy_hf_does_not_emit():
    d = hfg.decide("0xabc", 1.50, now=1000.0)
    assert d.should_emit is False
    assert d.severity is None
    assert d.reason == "hf_healthy"


def test_at_threshold_exact_does_not_emit():
    """HF == THRESHOLD → still healthy (the band is < THRESHOLD strict)."""
    d = hfg.decide("0xabc", 1.10, now=1000.0)
    assert d.should_emit is False
    assert d.severity is None


def test_warn_band_first_cross_emits():
    d = hfg.decide("0xabc", 1.08, now=1000.0)
    assert d.should_emit is True
    assert d.severity == "warn"
    assert d.reason == "warn_fire"


def test_warn_band_repeat_small_delta_within_window_suppressed():
    hfg.record_emit("0xabc", 1.08, "warn", now=1000.0)
    # 60min later, HF moved +0.005 (tiny) → suppressed
    d = hfg.decide("0xabc", 1.085, now=1000.0 + 60 * 60)
    assert d.should_emit is False
    assert d.reason == "warn_dedup_window_small_delta"


def test_warn_band_repeat_big_delta_emits(monkeypatch):
    """Big delta within the warn band must escape dedup.

    The default warn band is [1.05, 1.10) so max-internal delta is <0.05;
    this test widens the threshold so the band can hold a delta >= 0.05.
    """
    monkeypatch.setattr(hfg, "THRESHOLD", 1.20)
    monkeypatch.setattr(hfg, "DEDUP_DELTA", 0.03)
    hfg.record_emit("0xabc", 1.18, "warn", now=1000.0)
    # 60min later, HF moved -0.05 (>= DEDUP_DELTA=0.03) → emit
    d = hfg.decide("0xabc", 1.13, now=1000.0 + 60 * 60)
    assert d.should_emit is True
    assert d.severity == "warn"


def test_warn_band_repeat_after_window_emits():
    hfg.record_emit("0xabc", 1.08, "warn", now=1000.0)
    # 121min later (> DEDUP_MIN=120), tiny delta → emit
    d = hfg.decide("0xabc", 1.082, now=1000.0 + 121 * 60)
    assert d.should_emit is True
    assert d.severity == "warn"


def test_escalation_warn_to_critical_emits_critical():
    """Was warn, now critical → must fire (severity changed)."""
    hfg.record_emit("0xabc", 1.08, "warn", now=1000.0)
    # 5min later, HF dropped to 1.04 → critical
    d = hfg.decide("0xabc", 1.04, now=1000.0 + 5 * 60)
    assert d.should_emit is True
    assert d.severity == "critical"


def test_critical_first_cross_emits():
    d = hfg.decide("0xabc", 1.04, now=1000.0)
    assert d.should_emit is True
    assert d.severity == "critical"
    assert d.reason == "critical_fire"


def test_critical_repeat_small_delta_window_suppressed():
    hfg.record_emit("0xabc", 1.04, "critical", now=1000.0)
    # 30min later, tiny move → suppressed
    d = hfg.decide("0xabc", 1.045, now=1000.0 + 30 * 60)
    assert d.should_emit is False


def test_preliq_first_cross_emits():
    d = hfg.decide("0xabc", 1.01, now=1000.0)
    assert d.should_emit is True
    assert d.severity == "preliq"
    assert d.reason == "preliq_fire"


def test_preliq_repeats_every_5_min():
    hfg.record_emit("0xabc", 1.01, "preliq", now=1000.0)
    # 4 min later → suppressed (within 5min window)
    d = hfg.decide("0xabc", 1.01, now=1000.0 + 4 * 60)
    assert d.should_emit is False
    # 6 min later → fire again
    d = hfg.decide("0xabc", 1.01, now=1000.0 + 6 * 60)
    assert d.should_emit is True


def test_invalid_hf_does_not_emit():
    d = hfg.decide("0xabc", float("nan"), now=1000.0)
    assert d.should_emit is False
    assert d.reason == "hf_invalid"


def test_clear_wallet_resets_dedup():
    hfg.record_emit("0xabc", 1.08, "warn", now=1000.0)
    assert hfg.last_state("0xabc") is not None
    hfg.clear_wallet("0xabc")
    assert hfg.last_state("0xabc") is None
    # next call now treated as fresh first-cross
    d = hfg.decide("0xabc", 1.08, now=1000.0 + 5)
    assert d.should_emit is True


def test_kill_switch_passthrough(monkeypatch):
    monkeypatch.setattr(hfg, "ENABLED", False)
    # warn band → emits
    d = hfg.decide("0xabc", 1.08)
    assert d.should_emit is True
    assert d.severity == "warn"
    assert d.reason == "gate_disabled_passthrough"
    # healthy → no emit
    d = hfg.decide("0xabc", 1.50)
    assert d.should_emit is False


def test_status_summary_shape():
    hfg.record_emit("0xabc", 1.08, "warn", now=1000.0)
    s = hfg.status_summary()
    assert s["enabled"] is True
    assert s["threshold"] == 1.10
    assert s["critical"] == 1.05
    assert s["preliq"] == 1.02
    assert isinstance(s["tracked_wallets"], list)
    assert any(w["wallet"] == "0xabc" for w in s["tracked_wallets"])


def test_wallet_address_normalized_lowercase():
    """Mixed-case addr must dedup with the same lowercase entry."""
    hfg.record_emit("0xABC", 1.08, "warn", now=1000.0)
    d = hfg.decide("0xabc", 1.082, now=1000.0 + 60 * 60)
    assert d.should_emit is False  # dedup hit by lowercase
