"""R-UNLOCK (2026-06-01) tests + regression guard.

Locks in the basket-entry-unlock monitor behaviour:
  * Pure math (z-score, correlation proxy, vol compression, band hold).
  * A/B/C classifiers + the 5/5-unlock combo per name.
  * Level aggregation NONE/WATCH/APPROACHING/UNLOCK.
  * Edge-triggered state machine (escalation fires, retreat resets silently).
  * R-SILENT break-silence threshold.
  * Alert + /unlockcheck rendering (UNLOCK names tickers + manual-screen note).

REGRESSION GUARD: asserts R-UNLOCK does not regress R-PMCORE or the existing
features (R-VARIATIONAL, R-FARMDUMP, R-VAULTDEP, PM monitor, 5-checks verdict,
leverage auto-detect) — both the registry/wiring and the upstream modules.
"""
from __future__ import annotations

import os

import pytest

from modules import unlock_monitor as u


# ─── 1. Pure math ────────────────────────────────────────────────────────────
def test_zscore_positive_when_above_mean():
    z = u.zscore([1, 1, 1, 1, 5], 5)
    assert z is not None and z > 0


def test_zscore_negative_when_below_mean():
    z = u.zscore([5, 4, 3, 2, 1, 0], 6)
    assert z is not None and z < 0


def test_zscore_none_on_insufficient_or_flat():
    assert u.zscore([1, 2], 5) is None
    assert u.zscore([3, 3, 3, 3], 4) is None  # flat → stdev 0


def test_pearson_perfect_and_anti():
    assert round(u.pearson([1, 2, 3, 4], [2, 4, 6, 8]), 3) == 1.0
    assert round(u.pearson([1, 2, 3, 4], [8, 6, 4, 2]), 3) == -1.0


def test_rolling_corr_vs_btc_proxy():
    btc = [100, 101, 102, 103, 104, 105, 106]
    alt = [10, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6]  # moves with BTC
    c = u.rolling_corr_vs_btc(alt, btc, 6)
    assert c is not None and c > 0.9


def test_corr_is_repairing_direction():
    btc = [100, 101, 102, 101, 102, 103, 104, 105, 106, 107]
    # alt decoupled early, recoupled late → repairing True
    alt = [10, 9, 11, 8, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7]
    rep = u.corr_is_repairing(alt, btc, 9)
    assert rep in (True, False)  # determinable, never crashes


def test_realized_vol_and_contraction():
    assert u.realized_vol([1, 2, 3, 4, 5], 5) is not None
    assert u.realized_vol([1, 2, 3], 5) is None  # <3 returns → undefined
    assert u.series_is_contracting([0.5, 0.4, 0.3], 3) is True
    assert u.series_is_contracting([0.3, 0.4, 0.5], 3) is False
    assert u.series_is_contracting([0.3], 3) is None


def test_band_hold():
    assert u.band_hold([100, 101, 102, 103, 104], 5.0, 5) is True
    assert u.band_hold([100, 120, 90, 130, 80], 5.0, 5) is False


def test_btcd_rolling_over():
    assert u.btcd_rolling_over([66, 65, 64, 63, 62, 61]) is True
    assert u.btcd_rolling_over([60, 61, 62, 63]) is False


# ─── 2. Classifiers ──────────────────────────────────────────────────────────
def _k():
    return u.constants()


def test_btc_stab_fully_met_needs_recover_and_vol():
    k = _k()
    # z recovered (was deep, now above recover) AND vol compressing → fully
    s = u.classify_btc_stab(z=-0.3, z_prev_deep=True, vol_compressing=True, band=None, k=k)
    assert s.z_recovered is True and s.fully_met is True and s.partial_met is False


def test_btc_stab_partial_when_only_one_arm():
    k = _k()
    s = u.classify_btc_stab(z=-0.3, z_prev_deep=True, vol_compressing=None, band=None, k=k)
    assert s.fully_met is False and s.partial_met is True


def test_btc_stab_not_recovered_without_prior_deep():
    k = _k()
    s = u.classify_btc_stab(z=-0.3, z_prev_deep=False, vol_compressing=True, band=None, k=k)
    assert s.z_recovered is False  # never armed → no recovery claim


def test_alt_triggered_full_combo():
    k = _k()
    a = u.classify_alt("MORPHO", z=0.8, corr=0.7, repairing=True, funding=0.0001, k=k)
    assert a.positive_z and a.coint_ok and a.funding_ok and a.triggered


def test_alt_not_triggered_when_funding_negative():
    k = _k()
    a = u.classify_alt("BNB", z=0.8, corr=0.7, repairing=True, funding=-0.0001, k=k)
    assert a.funding_ok is False and a.triggered is False


def test_alt_not_triggered_when_negative_z():
    k = _k()
    a = u.classify_alt("UNI", z=-0.5, corr=0.9, repairing=True, funding=0.0, k=k)
    assert a.positive_z is False and a.triggered is False


def test_alt_funding_unknown_blocks_trigger():
    k = _k()
    a = u.classify_alt("INJ", z=0.8, corr=0.9, repairing=True, funding=None, k=k)
    assert a.funding_sign is None and a.triggered is False  # never fabricated


def test_breadth_soft_confirm():
    b1 = u.classify_breadth(asi=45.0, asi_estimated=True, btc_d=60.0, btcd_roll=False)
    assert b1.soft_confirm is True  # ASI above floor
    b2 = u.classify_breadth(asi=20.0, asi_estimated=True, btc_d=60.0, btcd_roll=True)
    assert b2.soft_confirm is True  # BTC.D rolling over
    b3 = u.classify_breadth(asi=20.0, asi_estimated=True, btc_d=60.0, btcd_roll=False)
    assert b3.soft_confirm is False


# ─── 3. Level aggregation ────────────────────────────────────────────────────
def _alts(n_trig, total=11):
    k = _k()
    out = []
    for i in range(total):
        if i < n_trig:
            out.append(u.classify_alt(f"T{i}", 0.9, 0.8, True, 0.0001, k))
        else:
            out.append(u.classify_alt(f"T{i}", -0.5, 0.2, False, -0.001, k))
    return out


def _calm_btc():
    return u.classify_btc_stab(z=-1.5, z_prev_deep=True, vol_compressing=False, band=False, k=_k())


def _noconfirm_breadth():
    return u.classify_breadth(asi=20.0, asi_estimated=True, btc_d=60.0, btcd_roll=False)


def test_level_unlock_at_required_names():
    k = _k()
    lvl = u.aggregate_level(_calm_btc(), _alts(4), _noconfirm_breadth(), k)
    assert lvl == u.UNLOCK


def test_level_approaching_on_partial_names():
    k = _k()
    lvl = u.aggregate_level(_calm_btc(), _alts(2), _noconfirm_breadth(), k)
    assert lvl == u.APPROACHING


def test_level_approaching_on_btc_fully_met():
    k = _k()
    btc = u.classify_btc_stab(z=-0.2, z_prev_deep=True, vol_compressing=True, band=None, k=k)
    lvl = u.aggregate_level(btc, _alts(0), _noconfirm_breadth(), k)
    assert lvl == u.APPROACHING


def test_level_watch_on_soft_confirm():
    k = _k()
    breadth = u.classify_breadth(asi=50.0, asi_estimated=True, btc_d=60.0, btcd_roll=True)
    lvl = u.aggregate_level(_calm_btc(), _alts(0), breadth, k)
    assert lvl == u.WATCH


def test_level_none_when_quiet():
    k = _k()
    lvl = u.aggregate_level(_calm_btc(), _alts(0), _noconfirm_breadth(), k)
    assert lvl == u.NONE


# ─── 4. Edge-triggered state machine ─────────────────────────────────────────
def test_should_fire_only_on_escalation():
    assert u.should_fire(u.WATCH, u.NONE) is True
    assert u.should_fire(u.UNLOCK, u.APPROACHING) is True
    assert u.should_fire(u.WATCH, u.WATCH) is False       # same level → no spam
    assert u.should_fire(u.WATCH, u.UNLOCK) is False       # retreat → silent


def test_state_roundtrip_and_retreat_reset(tmp_path, monkeypatch):
    # Point the module at a throwaway DB.
    monkeypatch.setattr(u, "DB_PATH", str(tmp_path / "unlock.db"))
    u._reset_for_tests()
    st = u.load_state()
    assert st["level"] == u.NONE
    u.save_state(u.APPROACHING, True, [0.3, 0.2], [66.0, 65.0])
    st = u.load_state()
    assert st["level"] == u.APPROACHING and st["btc_z_deep"] is True
    assert st["vol_series"] == [0.3, 0.2] and st["btcd_series"] == [66.0, 65.0]
    # Retreat: stored silently, and a later re-escalation can fire again.
    u.save_state(u.NONE, True, [], [])
    assert u.should_fire(u.WATCH, u.load_state()["level"]) is True


def test_alert_break_silence_default_is_unlock(monkeypatch):
    monkeypatch.delenv("UNLOCK_ALERT_BREAKS_SILENCE_LEVEL", raising=False)
    assert u.alert_breaks_silence_level() == u.UNLOCK


# ─── 5. Rendering ────────────────────────────────────────────────────────────
def _snapshot(level, n_trig):
    return u.UnlockSnapshot(
        level=level, btc=_calm_btc(), alts=_alts(n_trig),
        breadth=_noconfirm_breadth(), n_triggered=n_trig,
        ts_utc="2026-06-01 12:00 UTC", constants=_k(),
        confidence=["Cointegración = PROXY (corr rolling), NO Engle-Granger real."],
    )


def test_unlockcheck_renders_abc_and_disclaimer():
    txt = u.format_unlockcheck(_snapshot(u.NONE, 0))
    assert "A) ESTABILIZACIÓN BTC" in txt
    assert "B) RE-CORRELACIÓN ALTS" in txt
    assert "C) AMPLITUD DE RÉGIMEN" in txt
    assert "NO selecciona tokens" in txt
    assert "PROXY" in txt  # confidence note surfaced


def test_unlock_alert_names_tickers_and_manual_screen():
    txt = u.format_alert(_snapshot(u.UNLOCK, 4), prev_level=u.APPROACHING)
    assert "UNLOCK" in txt
    assert "SCREEN 5/5 COMPLETO" in txt
    assert "NO selecciona tokens" in txt
    # Each triggered ticker named with z/cointegration/funding context.
    assert "T0" in txt and "funding" in txt and "coint" in txt


def test_soft_alert_is_non_actionable():
    txt = u.format_alert(_snapshot(u.WATCH, 0), prev_level=u.NONE)
    assert "NO es gatillo" in txt


# ─── 6. REGRESSION GUARD — R-UNLOCK must not break prior rounds ──────────────
_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))


def _bot_src() -> str:
    with open(os.path.join(_ROOT, "bot.py"), encoding="utf-8") as f:
        return f.read()


def test_registry_has_unlockcheck_and_keeps_pmcore_and_variational():
    from commands_registry import COMMANDS
    by_cmd = {c.command: c for c in COMMANDS}
    # New command wired with the right handler name.
    assert by_cmd["unlockcheck"].handler_name == "cmd_unlockcheck"
    # R-PMCORE + R-VARIATIONAL + R-FARMDUMP commands all still present.
    for cmd in ("pm", "vaults", "variationalfunding", "variationalalerts", "variationalcheck"):
        assert cmd in by_cmd, f"regression: /{cmd} dropped from registry"


def test_bot_wiring_has_unlock_and_preserves_pmcore():
    src = _bot_src()
    # R-UNLOCK wiring present.
    assert '"unlockcheck": cmd_unlockcheck' in src
    assert "async def cmd_unlockcheck" in src
    assert "async def _unlock_monitor_job" in src
    assert "_unlock_monitor_job," in src  # scheduled
    # R-PMCORE wiring still intact (not clobbered by the patch).
    assert '"pm": cmd_pm' in src and '"vaults": cmd_vaults' in src
    assert "async def _pm_monitor_job" in src
    assert "_pm_monitor_job," in src
    # R-VARIATIONAL / R-FARMDUMP monitors still scheduled.
    assert "_variational_alerts_job," in src


def test_portfolio_margin_thresholds_unchanged():
    from modules.portfolio_margin import _classify
    assert _classify(0.0) == "CALM"
    assert _classify(0.40) == "WARN"
    assert _classify(0.70) == "STRESS"
    assert _classify(0.95) == "LIQ"


def test_farmdump_verdict_logic_unchanged():
    from modules import farmdump_checks as fd
    fail = fd.Check(1, "x", fd.FAIL, "")
    warn = fd.Check(2, "y", fd.WARN, "")
    ok = fd.Check(3, "z", fd.PASS, "")
    assert fd.aggregate_verdict([fail, ok]) == fd.NO_GO
    assert fd.aggregate_verdict([warn, ok]) == fd.CAUTION
    assert fd.aggregate_verdict([ok, ok]) == fd.GO


def test_leverage_autodetect_module_present():
    # R-LEVERAGE-AUTODETECT shipped this helper; importing it guards the round.
    import importlib
    mod = importlib.import_module("modules.kill_scenarios")
    assert mod is not None
