"""R-BOT-DEFINITIVE-2 T5 — /setppc manual PPC override (SQLite, timestamped).

Mission spec coverage: set, render, clear, invalid input.
"""
from __future__ import annotations

import sqlite3

import pytest

import modules.hype_acquisition as ha
from modules.hype_acquisition import (
    HypeAcquisition,
    clear_ppc_override,
    format_hype_acquisition_line,
    get_ppc_override,
    set_ppc_override,
)


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    """Route the intel_memory connection to a throwaway SQLite file."""
    db = str(tmp_path / "ppc.db")

    def _conn():
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr("modules.intel_memory._get_conn", _conn)
    yield


ND_ACQ = HypeAcquisition(
    known=False, ppc_usd=None, net_acq_usd=None, buy_qty=0.0, sell_qty=0.0,
    onchain_balance=1049.07, reason="saldo migrado/bridged — PPC no confiable",
)


def test_set_and_get_override():
    assert set_ppc_override(53.5, 41.5) is True
    ov = get_ppc_override()
    assert ov is not None
    assert ov["ppc_usd"] == pytest.approx(53.5)
    assert ov["net_acq_usd"] == pytest.approx(41.5)
    assert len(ov["set_date"]) == 10  # YYYY-MM-DD


def test_render_manual_line_with_secondary_note():
    set_ppc_override(53.5, 41.5)
    ov = get_ppc_override()
    line = format_hype_acquisition_line(ND_ACQ)
    assert f"PPC contable: $53.50 (manual, set {ov['set_date']})" in line
    assert f"adq. neta: $41.50 (manual, set {ov['set_date']})" in line
    # Reconciliation-gap note kept as SECONDARY line.
    assert "PPC no confiable" in line
    assert line.index("manual") < line.index("no confiable")


def test_clear_reverts_to_nd():
    set_ppc_override(53.5, 41.5)
    assert clear_ppc_override() is True
    assert get_ppc_override() is None
    line = format_hype_acquisition_line(ND_ACQ)
    assert "PPC contable: n/d" in line and "manual" not in line
    # Clearing again reports nothing to clear.
    assert clear_ppc_override() is False


def test_invalid_values_rejected():
    assert set_ppc_override(0.0, 41.5) is False
    assert set_ppc_override(53.5, -1.0) is False
    assert set_ppc_override("garbage", 41.5) is False
    assert get_ppc_override() is None


def test_override_does_not_mask_known_auto_when_absent():
    """No override → the known/auto render path is byte-identical."""
    acq = HypeAcquisition(
        known=True, ppc_usd=50.0, net_acq_usd=45.0, buy_qty=100.0,
        sell_qty=10.0, onchain_balance=90.0,
    )
    line = format_hype_acquisition_line(acq)
    assert "PPC contable (avg buy): $50.00" in line
    assert "manual" not in line
