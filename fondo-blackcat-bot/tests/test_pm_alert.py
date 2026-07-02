"""R-PMALERT (2026-06-01) regression tests.

Locks in the edge-triggered Portfolio Margin ratio alerting layer AND guards
that it does NOT regress R-PMCORE, R-UNLOCK, the PM display block, /pm, /vaults,
/unlockcheck, vault tracking, or the single PM watchdog.

  1. 4-band classification with the 0.85 pre-liq tier.
  2. Edge-trigger via SQLite: fire only on an upward CROSS, never re-spam a band,
     silent reset on retreat, re-fire on the next genuine cross.
  3. CRITICAL breaks silence; WARN/STRESS do not; naked-long always breaks.
  4. Naked-long edge fires regardless of band, only on transition.
  5. Alert messages carry ratio %, collateral, debt, capacity, shorts, timestamp,
     and a staleness note when the oracle was degraded.
  6. R-PMCORE intact: _classify keeps LIQ at 0.95, PMState.status unchanged,
     display block extended (4-level label) without breaking the naked-long line.
  7. R-UNLOCK intact: level ladder + should_fire untouched.
  8. Single PM monitor (extend, not duplicate) + the four PM commands registered.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from modules import pm_alert_monitor as pma


# Minimal PMState stand-in (avoids the real compute path for pure unit tests).
@dataclass
class _PM:
    ratio: float = 0.0
    collateral_usd: float = 100_000.0
    debt_usd: float = 0.0
    capacity_usd: float = 50_000.0
    available_usd: float = 50_000.0
    shorts_notional: float = 0.0
    naked_long: bool = False
    hype_qty: float = 1000.0
    hype_px: float = 100.0
    has_data: bool = True


# ─── 1. Classification with the 0.85 pre-liq tier ───────────────────────────
def test_classify_bands():
    assert pma.classify_alert_level(0.0) == pma.CALM
    assert pma.classify_alert_level(0.39) == pma.CALM
    assert pma.classify_alert_level(0.40) == pma.WARN
    assert pma.classify_alert_level(0.69) == pma.WARN
    assert pma.classify_alert_level(0.70) == pma.STRESS
    assert pma.classify_alert_level(0.84) == pma.STRESS
    assert pma.classify_alert_level(0.85) == pma.CRITICAL  # NEW pre-liq tier
    assert pma.classify_alert_level(0.95) == pma.CRITICAL  # liquidation point
    assert pma.classify_alert_level(1.20) == pma.CRITICAL


def test_classify_never_raises_on_garbage():
    assert pma.classify_alert_level(None) == pma.CALM  # type: ignore[arg-type]
    assert pma.classify_alert_level("x") == pma.CALM   # type: ignore[arg-type]


# ─── 2. Edge-trigger via SQLite ─────────────────────────────────────────────
def test_edge_trigger_fires_once_per_band_and_resets(tmp_path):
    db = str(tmp_path / "pma.db")

    # CALM → no alert.
    d = pma.evaluate(_PM(ratio=0.10), db_path=db)
    assert d.should_alert is False and d.level == pma.CALM

    # Cross into WARN → fires once.
    d = pma.evaluate(_PM(ratio=0.45), db_path=db)
    assert d.should_alert is True and d.level == pma.WARN and d.reason == "level_cross"

    # Still WARN → NO re-spam.
    d = pma.evaluate(_PM(ratio=0.50), db_path=db)
    assert d.should_alert is False

    # Cross into STRESS → fires.
    d = pma.evaluate(_PM(ratio=0.72), db_path=db)
    assert d.should_alert is True and d.level == pma.STRESS

    # Cross into CRITICAL → fires.
    d = pma.evaluate(_PM(ratio=0.90), db_path=db)
    assert d.should_alert is True and d.level == pma.CRITICAL

    # Still CRITICAL → NO re-spam.
    d = pma.evaluate(_PM(ratio=0.97), db_path=db)
    assert d.should_alert is False

    # Drop all the way back to CALM → silent reset (no alert on the way down).
    d = pma.evaluate(_PM(ratio=0.05), db_path=db)
    assert d.should_alert is False and d.level == pma.CALM

    # The next genuine upward cross fires again.
    d = pma.evaluate(_PM(ratio=0.45), db_path=db)
    assert d.should_alert is True and d.level == pma.WARN


def test_no_alert_on_downward_step_between_bands(tmp_path):
    db = str(tmp_path / "pma.db")
    pma.evaluate(_PM(ratio=0.90), db_path=db)          # arm CRITICAL
    d = pma.evaluate(_PM(ratio=0.72), db_path=db)      # CRITICAL → STRESS (down)
    assert d.should_alert is False and d.level == pma.STRESS
    # And STRESS does not re-fire from a STRESS-anchored state.
    d = pma.evaluate(_PM(ratio=0.75), db_path=db)
    assert d.should_alert is False


def test_dry_run_does_not_persist(tmp_path):
    db = str(tmp_path / "pma.db")
    pma.evaluate(_PM(ratio=0.45), db_path=db, persist=False)
    # State never advanced → a fresh evaluate at WARN still counts as a cross.
    d = pma.evaluate(_PM(ratio=0.45), db_path=db)
    assert d.should_alert is True


# ─── 3. R-SILENT break-silence semantics ────────────────────────────────────
def test_break_silence_only_critical(tmp_path):
    db = str(tmp_path / "pma.db")
    assert pma.evaluate(_PM(ratio=0.45), db_path=db).breaks_silence is False  # WARN
    assert pma.evaluate(_PM(ratio=0.72), db_path=db).breaks_silence is False  # STRESS
    assert pma.evaluate(_PM(ratio=0.90), db_path=db).breaks_silence is True   # CRITICAL


def test_breaks_silence_helper():
    assert pma.breaks_silence(pma.CRITICAL) is True
    assert pma.breaks_silence(pma.STRESS) is False
    assert pma.breaks_silence(pma.WARN) is False


# ─── 4. Naked-long edge ─────────────────────────────────────────────────────
def test_naked_long_fires_regardless_of_band(tmp_path):
    db = str(tmp_path / "pma.db")
    # Debt drawn, no shorts, ratio LOW (CALM band) → still fires, breaks silence.
    pm = _PM(ratio=0.20, debt_usd=10_000.0, shorts_notional=0.0, naked_long=True)
    d = pma.evaluate(pm, db_path=db)
    assert d.should_alert is True and d.reason == "naked_long"
    assert d.breaks_silence is True
    assert "hedge" in d.message.lower()
    # Same naked state next tick → no re-spam.
    d = pma.evaluate(pm, db_path=db)
    assert d.should_alert is False


# ─── 5. Message content ─────────────────────────────────────────────────────
def test_alert_message_carries_required_fields(tmp_path):
    db = str(tmp_path / "pma.db")
    pm = _PM(ratio=0.72, debt_usd=36_000.0, shorts_notional=22_000.0)
    d = pma.evaluate(pm, db_path=db)
    m = d.message
    assert "72.0%" in m                 # current ratio
    assert "Colateral" in m             # HYPE collateral $
    assert "Deuda" in m                 # debt drawn $
    assert "Capacidad" in m             # capacity $
    assert "notional" in m              # open short notional
    assert "UTC" in m                   # timestamp
    assert "STRESS" in m


def test_staleness_note_when_oracle_degraded(tmp_path):
    db = str(tmp_path / "pma.db")
    pm = _PM(ratio=0.45, hype_qty=1000.0, hype_px=0.0)  # oracle down
    d = pma.evaluate(pm, db_path=db)
    assert "degrad" in d.message.lower() or "no disponible" in d.message.lower()


def test_four_distinct_messages_render():
    warn = pma.build_alert_message(_PM(ratio=0.45), pma.WARN)
    stress = pma.build_alert_message(_PM(ratio=0.72), pma.STRESS)
    crit = pma.build_alert_message(_PM(ratio=0.90), pma.CRITICAL)
    naked = pma.build_naked_long_message(_PM(debt_usd=10_000.0, naked_long=True))
    assert "WARN" in warn and "Add USDC" in warn
    assert "STRESS" in stress and "liquidation path" in stress
    assert "CRÍTICO" in crit and "approaching liquidation" in crit.lower()
    assert "HEDGE MISSING" in naked
    assert len({warn, stress, crit, naked}) == 4


# ─── 6. R-PMCORE intact (no regression) ─────────────────────────────────────
def test_rpmcore_classify_still_liq_at_095():
    from modules.portfolio_margin import _classify
    assert _classify(0.40) == "WARN"
    assert _classify(0.70) == "STRESS"
    assert _classify(0.84) == "STRESS"   # 0.85 tier is alert-only, NOT _classify
    assert _classify(0.94) == "STRESS"
    assert _classify(0.95) == "LIQ"      # R-PMCORE liquidation threshold intact


def test_rpmcore_pmstate_status_unchanged():
    from modules.portfolio_margin import compute_pm_state
    bal = [{"coin": "HYPE", "total": 1000.0}, {"coin": "USDC", "total": -35_000}]
    pm = compute_pm_state(bal, [], {"HYPE": 100.0})
    assert pm.status == "STRESS"         # ratio 0.70 → STRESS (R-PMCORE classifier)
    assert pma.classify_alert_level(pm.ratio) == pma.STRESS


def test_display_block_shows_borrow_utilization_not_liq_risk():
    from modules.portfolio_margin import compute_pm_state, format_pm_state_telegram
    # R-PM-RATIO-RELABEL (2026-06-07): ratio 0.90 is borrow UTILIZATION (90% of
    # the max-borrow cap), NOT a liquidation signal. The panel must show the
    # renamed utilization line with a non-liquidation status and must NEVER
    # carry "LIQ-RISK" nor the WARN/STRESS/CRÍTICO/LIQ scale on that line.
    bal = [{"coin": "HYPE", "total": 1000.0}, {"coin": "USDC", "total": -45_000}]
    pm = compute_pm_state(bal, [], {"HYPE": 100.0})
    block = format_pm_state_telegram(pm)
    assert "Borrow utilization (vs 50% max-borrow)" in block
    assert "NEAR MAX-BORROW" in block    # 90% of the cap → near, not liquidating
    assert "LIQ-RISK" not in block
    assert "CRÍTICO 85%" not in block
    assert "LIQ 95%" not in block


def test_display_naked_long_line_preserved():
    # R-BOT-DEFINITIVE-2 T7 (2026-07-02): the panel line is now a NEUTRAL
    # owner-decision note (no siren, no imperative) — but it must still render
    # whenever naked_long is True (the structure stays visible, never hidden).
    from modules.portfolio_margin import compute_pm_state, format_pm_state_telegram
    bal = [{"coin": "HYPE", "total": 1000.0}, {"coin": "USDC", "total": -10_000}]
    pm = compute_pm_state(bal, [], {"HYPE": 100.0})
    block = format_pm_state_telegram(pm)
    assert pm.naked_long is True
    assert "Estructura: long apalancado sin hedge activo (decisión del owner)" in block
    assert "🚨" not in block


# ─── 7. R-UNLOCK intact ─────────────────────────────────────────────────────
def test_runlock_ladder_and_should_fire_untouched():
    from modules import unlock_monitor as ul
    assert ul._LEVEL_RANK == {ul.NONE: 0, ul.WATCH: 1, ul.APPROACHING: 2, ul.UNLOCK: 3}
    assert ul.should_fire(ul.UNLOCK, ul.WATCH) is True
    assert ul.should_fire(ul.WATCH, ul.UNLOCK) is False  # retreat is silent


# ─── 8. Single PM monitor + commands registered ─────────────────────────────
def test_single_pm_monitor_not_duplicated():
    import re
    with open("bot.py", "r", encoding="utf-8") as f:
        src = f.read()
    # Exactly ONE PM monitor job definition and ONE scheduler registration.
    assert len(re.findall(r"async def _pm_monitor_job", src)) == 1
    assert src.count('id="pm_monitor"') == 1


def test_pm_and_neighbour_commands_registered():
    from commands_registry import COMMANDS  # type: ignore
    names = {c.command for c in COMMANDS}
    for cmd in ("pm", "vaults", "unlockcheck"):
        assert cmd in names
