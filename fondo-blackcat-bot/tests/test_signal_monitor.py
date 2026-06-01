"""R-SIGNAL (2026-06-01) tests + regression guard.

Locks in the per-name short-signal alerting path that sits ORTHOGONAL to the
>=4 R-UNLOCK-PRECISION ladder:
  * single-name qualify fires (edge-triggered),
  * no re-spam while a name stays qualified,
  * drop-and-re-qualify re-fires (edge re-arm),
  * the >=2-reading debounce blocks a 1-cycle transient,
  * /signals on-demand readout (current set + zero-qualify path),
  * the alert format (header disclaimer, NEW flagging, AiPear block),
  * advance_state=False is a PURE READ (no SQLite mutation).

REGRESSION GUARD: asserts R-SIGNAL reuses the EXISTING gate engine (no fork)
and does not disturb R-UNLOCK-PRECISION (5-gate engine, >=4 ladder,
/unlockcheck), R-PMCORE, R-PMALERT (PM thresholds + alerts), /pm, /vaults,
the /reporte PM block, vault tracking, R-VARIATIONAL, or R-FARMDUMP.
"""
from __future__ import annotations

import inspect

import pytest

from modules import signal_monitor as s
from modules import unlock_monitor as u


# ─── Helpers: build AltGate verdicts + a snapshot WITHOUT touching the network ─
def _gate(ticker: str, sector: str, counts: bool, *, z=1.4, hurst=0.42,
          funding=0.00001, cov=0.97, corr=0.55) -> u.AltGate:
    """A fully-formed AltGate the way evaluate_name_gates would return it.

    ``counts`` is set explicitly so a test fixes the engine verdict; every other
    field is consistent with that verdict (this exercises the SIGNAL state
    machine, not the gate math — that math is owned + tested by unlock_monitor)."""
    fsign = None if funding is None else (1 if funding > 0 else (-1 if funding < 0 else 0))
    return u.AltGate(
        ticker=ticker, sector=sector, z=z, z_streak=2, hurst=hurst, rsi=55.0,
        pct_k=3.0, higher_highs=False, funding=funding, funding_sign=fsign,
        corr=corr, repairing=None, coverage=cov, data_ok=True, z_floor_ok=True,
        z_persistent=True, z_ok=True, hurst_ok=True, squeeze_flag=False,
        squeeze_reasons=[], funding_ok=True, counts=counts,
        reason="" if counts else "no cumple gates",
    )


def _snap(alts: list[u.AltGate]) -> u.UnlockSnapshot:
    return u.UnlockSnapshot(
        level=u.NONE,
        btc=u.BtcStab(z=None, z_prev_deep=False, z_recovered=False,
                      vol_compressing=None, band_hold=None, fully_met=False,
                      partial_met=False),
        alts=alts, breadth=u.BreadthState(None, False, None, None, False),
        n_counts=sum(1 for a in alts if a.counts), n_sectors=0, unlock_streak=0,
        ts_utc="2026-06-01 12:00 UTC", constants=u.constants(),
        confidence=["test"],
    )


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Each test gets its own SQLite file; the signal module shares the engine's
    DB_PATH, so patch it on both for full isolation."""
    db = str(tmp_path / "signal.db")
    monkeypatch.setattr(s, "DB_PATH", db)
    monkeypatch.setattr(u, "DB_PATH", db)
    s._reset_for_tests()
    yield
    s._reset_for_tests()


# ─── 1. Debounce blocks a 1-cycle transient ──────────────────────────────────
def test_debounce_blocks_single_cycle_transient(monkeypatch):
    monkeypatch.delenv("SIGNAL_PERSIST_READINGS", raising=False)  # default 2
    # Reading 1: WLD passes all 5 → streak 1 (< persist 2) → NOT announced.
    r1 = s.evaluate_signals(_snap([_gate("WLD", "AI", True)]), advance_state=True)
    assert r1.fire is False and r1.new_names == []
    assert [a.ticker for a in r1.current_counts] == ["WLD"]  # passes all 5 NOW
    # Reading 2: it drops out before the debounce completes → never fires.
    r2 = s.evaluate_signals(_snap([_gate("WLD", "AI", False)]), advance_state=True)
    assert r2.fire is False and r2.new_names == [] and r2.current_counts == []


# ─── 2. Single-name qualify fires once the debounce is met ───────────────────
def test_single_name_qualify_fires_after_debounce(monkeypatch):
    monkeypatch.delenv("SIGNAL_PERSIST_READINGS", raising=False)
    s.evaluate_signals(_snap([_gate("WLD", "AI", True)]), advance_state=True)   # streak 1
    r2 = s.evaluate_signals(_snap([_gate("WLD", "AI", True)]), advance_state=True)  # streak 2 → fire
    assert r2.fire is True
    assert r2.new_names == ["WLD"]
    assert [a.ticker for a in r2.qualifying] == ["WLD"]


# ─── 3. No re-spam while the name stays qualified ────────────────────────────
def test_no_respam_while_stable(monkeypatch):
    monkeypatch.delenv("SIGNAL_PERSIST_READINGS", raising=False)
    s.evaluate_signals(_snap([_gate("WLD", "AI", True)]), advance_state=True)
    fired = s.evaluate_signals(_snap([_gate("WLD", "AI", True)]), advance_state=True)
    assert fired.new_names == ["WLD"]
    # Subsequent stable cycles: still in qualifying set, but NOT re-announced.
    for _ in range(3):
        r = s.evaluate_signals(_snap([_gate("WLD", "AI", True)]), advance_state=True)
        assert r.fire is False
        assert r.new_names == []
        assert [a.ticker for a in r.qualifying] == ["WLD"]  # still listed


# ─── 4. Drop-and-re-qualify re-fires (edge re-arm) ───────────────────────────
def test_drop_and_requalify_refires(monkeypatch):
    monkeypatch.delenv("SIGNAL_PERSIST_READINGS", raising=False)
    s.evaluate_signals(_snap([_gate("WLD", "AI", True)]), advance_state=True)
    a = s.evaluate_signals(_snap([_gate("WLD", "AI", True)]), advance_state=True)
    assert a.new_names == ["WLD"]
    # Drops out → streak + announce flag reset.
    s.evaluate_signals(_snap([_gate("WLD", "AI", False)]), advance_state=True)
    # Re-qualifies: needs the full debounce again, then re-fires.
    b1 = s.evaluate_signals(_snap([_gate("WLD", "AI", True)]), advance_state=True)
    assert b1.fire is False  # streak back to 1
    b2 = s.evaluate_signals(_snap([_gate("WLD", "AI", True)]), advance_state=True)
    assert b2.fire is True and b2.new_names == ["WLD"]


# ─── 5. Multi-name: full set listed, only the NEW one flagged ────────────────
def test_full_set_listed_new_flagged(monkeypatch):
    monkeypatch.delenv("SIGNAL_PERSIST_READINGS", raising=False)
    # Get WLD fully announced first.
    s.evaluate_signals(_snap([_gate("WLD", "AI", True)]), advance_state=True)
    s.evaluate_signals(_snap([_gate("WLD", "AI", True)]), advance_state=True)
    # Now TAO joins; both pass. TAO debounces over 2 readings.
    pair = [_gate("WLD", "AI", True), _gate("TAO", "AI", True)]
    s.evaluate_signals(_snap(pair), advance_state=True)             # TAO streak 1
    r = s.evaluate_signals(_snap(pair), advance_state=True)         # TAO streak 2 → new
    assert r.fire is True
    assert r.new_names == ["TAO"]                                   # only TAO is NEW
    assert {a.ticker for a in r.qualifying} == {"WLD", "TAO"}       # full current set
    txt = s.format_alert(r)
    assert "🆕 NUEVO" in txt and "TAO" in txt and "WLD" in txt


# ─── 6. /signals on-demand readout — current set ─────────────────────────────
def test_signals_readout_current_set():
    r = s.evaluate_signals(_snap([_gate("INJ", "DeFi", True), _gate("UNI", "DeFi", False)]),
                           advance_state=False)
    out = s.format_signals(r)
    assert "🎯 R-SIGNAL" in out
    assert "1/2 pasan los 5 gates" in out
    assert "INJ" in out and "AIPEAR_CONFIRM" in out


# ─── 7. Zero-qualify path is explicit with the count ─────────────────────────
def test_signals_zero_qualify_explicit():
    alts = [_gate(t, "x", False) for t in
            ("MORPHO", "BNB", "XLM", "HBAR", "ALGO", "UNI", "MKR", "NEAR", "INJ", "TAO", "WLD")]
    r = s.evaluate_signals(_snap(alts), advance_state=False)
    out = s.format_signals(r)
    assert "0/11 califican" in out
    assert r.fire is False and r.qualifying == []


# ─── 8. advance_state=False is a PURE READ (no SQLite mutation, no edge burn) ─
def test_ondemand_does_not_advance_or_burn_edge(monkeypatch):
    monkeypatch.delenv("SIGNAL_PERSIST_READINGS", raising=False)
    snap = _snap([_gate("WLD", "AI", True)])
    # Many on-demand reads must never advance the streak nor announce.
    for _ in range(5):
        r = s.evaluate_signals(snap, advance_state=False)
        assert r.fire is False
    assert s.load_signal_state() == {}                       # nothing persisted
    # The scheduler (advance) still gets a clean 2-reading debounce afterwards.
    s.evaluate_signals(snap, advance_state=True)             # streak 1
    r2 = s.evaluate_signals(snap, advance_state=True)        # streak 2 → fire
    assert r2.new_names == ["WLD"]


# ─── 9. Header disclaimer + AiPear schema ────────────────────────────────────
def test_alert_header_and_aipear_schema(monkeypatch):
    monkeypatch.delenv("SIGNAL_PERSIST_READINGS", raising=False)
    s.evaluate_signals(_snap([_gate("WLD", "AI", True, z=1.6, hurst=0.40, funding=2e-5, cov=0.95)]),
                       advance_state=True)
    r = s.evaluate_signals(_snap([_gate("WLD", "AI", True, z=1.6, hurst=0.40, funding=2e-5, cov=0.95)]),
                           advance_state=True)
    txt = s.format_alert(r)
    assert "pasó el filtro short de 5 gates" in txt
    assert "Confirmá cada uno con AiPear 5/5" in txt
    assert "NO selecciona tokens" in txt
    # csv schema identical to the R-UNLOCK AiPear block.
    assert "ticker,sector,z4h,hurst,funding,data_conf" in txt
    assert "WLD,AI,+1.60,0.40,+0.000020,95%" in txt


# ─── 10. Distinct prefix vs the R-UNLOCK ladder ──────────────────────────────
def test_prefix_distinct_from_unlock_ladder():
    assert s.SIGNAL_PREFIX.startswith("🎯")
    assert "🔓" not in s.SIGNAL_PREFIX           # the ladder uses 🔓 R-UNLOCK


# ─── 11. NO FORK: R-SIGNAL imports the engine's verdicts, owns no gate math ───
def test_reuses_engine_no_fork():
    src = inspect.getsource(s)
    # The signal module must consume AltGate/UnlockSnapshot from unlock_monitor.
    assert "from modules.unlock_monitor import" in src
    # It must NOT re-implement any of the five gates or their estimators.
    for forbidden in ("def evaluate_name_gates", "def zscore", "def hurst_rs",
                      "def rsi(", "def coverage_fraction", "def aggregate_level"):
        assert forbidden not in src, f"R-SIGNAL must not fork: {forbidden}"


# ─── 12. REGRESSION GUARD — neighbours intact, engine untouched ──────────────
def test_regression_unlock_precision_engine_untouched():
    # The 5-gate engine + >=4 ladder thresholds are exactly as R-UNLOCK shipped.
    k = u.constants()
    assert k["z_floor"] == 1.00
    assert u.hurst_count_cutoff(k) == pytest.approx(0.47)       # 0.50 - 0.03 margin
    assert k["funding_min"] == 0.0
    assert k["data_min_coverage"] == 0.90
    assert int(k["names_required"]) == 4 and int(k["min_sectors"]) == 3
    assert int(k["z_persist_readings"]) == 2
    # Ladder + edge-trigger semantics intact.
    assert u.should_fire(u.UNLOCK, u.WATCH) is True
    assert u.should_fire(u.WATCH, u.UNLOCK) is False
    assert u._LEVEL_RANK == {"NONE": 0, "WATCH": 1, "APPROACHING": 2, "UNLOCK": 3}


def test_regression_unlockcheck_pure_read_and_signals_wired():
    import bot
    # /unlockcheck still a pure read; /signals registered alongside it.
    assert "advance_state=False" in inspect.getsource(bot.cmd_unlockcheck)
    assert "advance_state=False" in inspect.getsource(bot.cmd_signals)
    assert bot.HANDLER_MAP.get("signals") is bot.cmd_signals
    assert bot.HANDLER_MAP.get("unlockcheck") is bot.cmd_unlockcheck
    from commands_registry import COMMANDS
    names = {c.command for c in COMMANDS}
    assert {"signals", "unlockcheck", "pm", "vaults", "variationalcheck"} <= names


def test_regression_pm_thresholds_and_modules_present():
    # R-PMCORE / R-PMALERT thresholds + neighbour modules import cleanly.
    import importlib
    for mod in ("modules.portfolio_margin", "modules.pm_alert_monitor",
                "modules.vault_history", "modules.variational_alerts",
                "modules.farmdump_checks", "modules.unlock_monitor"):
        assert importlib.import_module(mod) is not None
    from modules import pm_alert_monitor as pm
    # The 4 PM bands 0.40/0.70/0.85/0.95 must be unchanged.
    src = inspect.getsource(pm)
    for band in ("0.40", "0.70", "0.85", "0.95"):
        assert band in src


def test_regression_signal_state_table_is_separate():
    # R-SIGNAL must NOT reuse/clobber the engine's unlock_alt_state table.
    assert "signal_alt_state" in inspect.getsource(s)
    assert "unlock_alt_state" not in inspect.getsource(s)
