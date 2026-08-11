"""R-LTV65-QUIET (2026-08-11) — LTV 0.65 + alert hygiene + DreamCash cash.

Part 1 — HL raised HYPE max-borrow LTV under Portfolio Margin 0.50 → 0.65.
  Capacity/headroom/utilization now run on PM_MAX_BORROW_LTV (env, default
  0.65). CRITICAL SEPARATION: liquidation math uses PM_MAINT_LTV (0.75) and
  is NEVER derived from the borrow LTV — the legacy 0.5+0.5×ltv formula would
  have corrupted LIQ REAL to debt/(0.65·0.95·qty) territory (maint 0.825).
  Acceptance (oracle 54.96, 3006.28 HYPE, debt 91.6K):
    capacity ≈ 107.4K · headroom ≈ +15.8K · util ≈ 85% · LIQ REAL ≈ 42.80.

Part 2 — alert hygiene: HF hysteresis machine, SL-UNREACHABLE material-change
  dedup + daily digest, GO digest batching (GO_ALERT_BATCH_HOURS, env=0 =
  legacy behavior), generic dedup guard.

Part 3 — DreamCash 0x171b (idle perp, 1,138.79 USDC spot) must surface its
  cash in PORTFOLIO CONSOLIDADO, never 0.00.
"""
from __future__ import annotations

import asyncio
import importlib
import os

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Part 1 — LTV 65 config + maint separation
# ─────────────────────────────────────────────────────────────────────────────

def test_static_env_defaults_locked():
    """Static locks: new env defaults exactly as specified."""
    import config
    assert config.PM_MAX_BORROW_LTV == pytest.approx(0.65)
    assert config.PM_HYPE_LTV == pytest.approx(0.65)      # legacy alias follows
    assert config.PM_MAINT_LTV == pytest.approx(0.75)
    # Alert hygiene env defaults (read at call time in their modules).
    from modules.alerts_margin import _hyst_params
    band, cdi, cdc = _hyst_params()
    assert band == pytest.approx(0.03)
    assert cdi == pytest.approx(6 * 3600.0)
    assert cdc == pytest.approx(1 * 3600.0)
    from modules.go_alerts import batch_hours
    assert batch_hours() == pytest.approx(3.0)


def test_acceptance_numbers_hype_stack():
    """Pin the acceptance snapshot: oracle 54.96 · 3006.28 HYPE · debt 91.6K."""
    from modules.portfolio_margin import compute_pm_state
    spot = [
        {"coin": "HYPE", "total": 3006.28},
        {"coin": "USDC", "total": -1.0, "borrowed": 91_600.0},
    ]
    pm = compute_pm_state(spot, [], {"HYPE": 54.96})
    assert pm.capacity_usd == pytest.approx(107_400.0, abs=100.0)   # ≈107.4K
    assert pm.available_usd == pytest.approx(15_800.0, abs=100.0)   # ≈+15.8K
    assert pm.ratio * 100 == pytest.approx(85.3, abs=0.5)           # ≈85%
    # LIQ REAL UNCHANGED by the borrow-LTV bump: debt/(0.7125×qty) ≈ 42.8.
    assert pm.liq_price_real == pytest.approx(42.80, abs=0.15)
    assert pm.max_ltv == pytest.approx(0.65)
    assert pm.liq_threshold == pytest.approx(0.75)                  # maint intact
    # util < 100% → NOT flagged OVER MAX-BORROW under the NEW parameter.
    from modules.pm_panel import borrow_utilization_status
    label, _ = borrow_utilization_status(pm.ratio * 100)
    assert "OVER MAX-BORROW" not in label


def test_maint_never_derived_from_borrow_ltv():
    """The killer bug: 0.65 through the legacy formula would give maint 0.825
    and shift LIQ REAL. Maint must stay 0.75 for ANY borrow ltv."""
    from modules.portfolio_margin import _liq_threshold_for_ltv
    for ltv in (0.40, 0.50, 0.65, 0.70, 0.90):
        assert _liq_threshold_for_ltv(ltv) == pytest.approx(0.75)
    # A valid live maint override IS honoured (future re-risk by HL).
    assert _liq_threshold_for_ltv(0.65, maint_override=0.78) == pytest.approx(0.78)


def test_env_override_max_borrow_ltv(monkeypatch):
    monkeypatch.setenv("PM_MAX_BORROW_LTV", "0.70")
    import config
    importlib.reload(config)
    try:
        assert config.PM_MAX_BORROW_LTV == pytest.approx(0.70)
        assert config.PM_HYPE_LTV == pytest.approx(0.70)
        assert config.PM_MAINT_LTV == pytest.approx(0.75)  # untouched
    finally:
        monkeypatch.delenv("PM_MAX_BORROW_LTV")
        importlib.reload(config)


def test_health_payload_reports_pm_ltv_and_warning():
    from modules.version_info import _pm_ltv_status
    st = _pm_ltv_status()
    assert st["max_borrow_ltv"] == pytest.approx(0.65)
    assert st["maint_ltv"] == pytest.approx(0.75)
    # borrowLendReserveState does not expose maint (verified 2026-08-11) →
    # unverified path must carry the WARNING string.
    if not st.get("maint_verified_live"):
        assert "warning" in st and "PM_MAINT_LTV" in st["warning"]


def test_capacity_line_renders_065():
    from modules.portfolio_margin import compute_pm_state, format_pm_state_telegram
    spot = [
        {"coin": "HYPE", "total": 3006.28},
        {"coin": "USDC", "total": -1.0, "borrowed": 91_600.0},
    ]
    block = format_pm_state_telegram(compute_pm_state(spot, [], {"HYPE": 54.96}))
    assert "Capacidad borrow (LTV 0.65)" in block
    assert "Borrow utilization (vs 65% max-borrow)" in block
    assert "Liq nominal (maint-LTV 0.75" in block
    assert "LIQ REAL (0.7125" in block


# ─────────────────────────────────────────────────────────────────────────────
# Part 2.1 — HF hysteresis machine
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def am(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import config
    importlib.reload(config)
    import modules.alerts_margin as alerts_margin
    importlib.reload(alerts_margin)
    yield alerts_margin
    monkeypatch.delenv("DATA_DIR")
    importlib.reload(config)


def test_hf_wobble_around_threshold_fires_once(am):
    """THE spam scenario: HF oscillating 1.29↔1.31 paged on every wobble."""
    t = 1_000_000.0
    assert am.evaluate_pm_hf(1.50, now=t)[0] is False
    assert am.evaluate_pm_hf(1.29, now=t + 60)[0] is True     # cross → fire
    # Wobble inside the hysteresis band [1.30, 1.33): NO re-arm, NO re-fire.
    for i, hf in enumerate((1.31, 1.29, 1.32, 1.28, 1.31, 1.29)):
        assert am.evaluate_pm_hf(hf, now=t + 120 + i * 60)[0] is False


def test_hf_rearm_requires_two_sustained_checks(am):
    t = 1_000_000.0
    am.evaluate_pm_hf(1.50, now=t)
    assert am.evaluate_pm_hf(1.29, now=t + 60)[0] is True
    # One recovery above 1.33 then a dip: NOT re-armed.
    assert am.evaluate_pm_hf(1.34, now=t + 120)[0] is False   # streak 1
    assert am.evaluate_pm_hf(1.29, now=t + 180)[0] is False   # streak reset, latched
    # Two CONSECUTIVE recoveries → re-armed; next cross (past cooldown) fires.
    assert am.evaluate_pm_hf(1.34, now=t + 240)[0] is False
    assert am.evaluate_pm_hf(1.35, now=t + 300)[0] is False   # re-armed here
    cd = 6 * 3600.0
    assert am.evaluate_pm_hf(1.28, now=t + cd + 400)[0] is True


def test_hf_critical_zone_never_silenced(am):
    """<1.10 re-fires every ALERT_COOLDOWN_HOURS_CRITICAL (1h), forever."""
    t = 1_000_000.0
    am.evaluate_pm_hf(1.50, now=t)
    s, msg = am.evaluate_pm_hf(1.05, now=t + 60)
    assert s is True and "1.10" in msg
    assert am.evaluate_pm_hf(1.05, now=t + 600)[0] is False    # inside 1h cd
    s2, msg2 = am.evaluate_pm_hf(1.04, now=t + 3_700)          # 1h later
    assert s2 is True and "1.10" in msg2                       # re-fired
    s3, _ = am.evaluate_pm_hf(1.04, now=t + 7_400)             # another hour
    assert s3 is True


def test_hf_info_cooldown_blocks_rapid_recross(am):
    t = 1_000_000.0
    am.evaluate_pm_hf(1.50, now=t)
    assert am.evaluate_pm_hf(1.29, now=t + 60)[0] is True
    # Fast recovery ×2 (re-armed) then re-cross INSIDE the 6h info cooldown:
    am.evaluate_pm_hf(1.35, now=t + 120)
    am.evaluate_pm_hf(1.35, now=t + 180)
    assert am.evaluate_pm_hf(1.29, now=t + 240)[0] is False    # cooldown holds
    # After the cooldown the same cross fires again.
    assert am.evaluate_pm_hf(1.35, now=t + 300)[0] is False
    assert am.evaluate_pm_hf(1.35, now=t + 360)[0] is False
    assert am.evaluate_pm_hf(1.29, now=t + 6 * 3600.0 + 400)[0] is True


def test_hf_state_survives_restart(am):
    t = 1_000_000.0
    am.evaluate_pm_hf(1.50, now=t)
    assert am.evaluate_pm_hf(1.25, now=t + 60)[0] is True
    import modules.alerts_margin as am2
    importlib.reload(am2)
    assert am2.DB_PATH == am.DB_PATH
    assert am2.evaluate_pm_hf(1.25, now=t + 120)[0] is False   # no boot re-fire


# ─────────────────────────────────────────────────────────────────────────────
# Part 2.2 — SL UNREACHABLE dedup lifecycle
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def sv(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import config
    importlib.reload(config)
    import modules.sl_validator as sl_validator
    importlib.reload(sl_validator)
    yield sl_validator
    monkeypatch.delenv("DATA_DIR")
    importlib.reload(config)


def test_sl_static_position_stays_silent(sv):
    """THE ZORA case: 15+ re-fires in 48h for an unchanged position."""
    assert sv.should_alert("ZORA", 0.02890, 0.02650) is True   # first: fire
    # Funding drift moves liq <2% and SL is a placed order (static): SILENT.
    for liq in (0.02652, 0.02660, 0.02645, 0.02670, 0.02655):
        assert sv.should_alert("ZORA", 0.02890, liq) is False


def test_sl_material_changes_refire(sv):
    assert sv.should_alert("HOOD", 99.58, 94.90) is True
    assert sv.should_alert("HOOD", 99.58, 94.90) is False      # unchanged
    assert sv.should_alert("HOOD", 101.00, 94.90) is True      # SL modified
    assert sv.should_alert("HOOD", 101.00, 91.00) is True      # margin → liq >2%
    # Gap compression >10% (liq creeping toward SL) re-fires:
    # gap 10.00 → 8.50 = 15% compression; liq move 1.65% stays under the 2%
    # margin-change trigger, so ONLY the gap rule can fire here.
    assert sv.should_alert("HOOD", 101.00, 92.50) is True
    sv.clear_condition("HOOD")
    assert sv.should_alert("HOOD", 101.00, 92.50) is True      # position re-armed


def test_sl_daily_digest_once_per_day(sv):
    assert sv._digest_due(now=1_000_000.0) is True
    assert sv._digest_due(now=1_000_000.0 + 3600.0) is False       # same day
    assert sv._digest_due(now=1_000_000.0 + 23 * 3600.0) is False
    assert sv._digest_due(now=1_000_000.0 + 25 * 3600.0) is True   # next day


# ─────────────────────────────────────────────────────────────────────────────
# Part 2.3 — GO batching
# ─────────────────────────────────────────────────────────────────────────────

def test_go_batch_env_zero_is_legacy(monkeypatch):
    monkeypatch.setenv("GO_ALERT_BATCH_HOURS", "0")
    from modules.go_alerts import batch_hours
    assert batch_hours() == 0.0
    monkeypatch.setenv("GO_ALERT_BATCH_HOURS", "-5")
    assert batch_hours() == 0.0        # clamped, never negative


@pytest.fixture
def go(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import config
    importlib.reload(config)
    import modules.go_alerts as go_alerts
    importlib.reload(go_alerts)
    sent: list[str] = []

    async def _send(bot, chat_id, msg, **kw):
        sent.append(msg)

    monkeypatch.setattr(go_alerts, "send_bot_message", _send)
    monkeypatch.setattr(go_alerts, "TELEGRAM_CHAT_ID", "123")
    yield go_alerts, sent, monkeypatch
    monkeypatch.delenv("DATA_DIR")
    importlib.reload(config)


class _Gate:
    def __init__(self, z=-2.1, hurst=0.31):
        self.z, self.hurst = z, hurst


class _Row:
    def __init__(self, ticker, go=True):
        self.ticker = ticker
        self.is_go_candidate = go
        self.gate = _Gate()


class _Res:
    def __init__(self, ranked, long_context=()):
        self.ranked = list(ranked)
        self.long_context = list(long_context)


def _patch_screen(go_alerts, monkeypatch, res):
    import modules.universal_screener as scr

    async def _fake(*a, **k):
        return res

    monkeypatch.setattr(scr, "compute_screen_cached", _fake)


def test_go_batching_accumulates_and_flushes(go, monkeypatch):
    go_alerts, sent, mp = go
    mp.setenv("GO_ALERT_BATCH_HOURS", "3")
    _patch_screen(go_alerts, mp, _Res([]))
    asyncio.run(go_alerts.run_go_alert_cycle(None))            # seed silent
    assert sent == []
    # 1st entrant: quiet window → immediate digest (rate-limiter semantics).
    _patch_screen(go_alerts, mp, _Res([_Row("WLD")]))
    assert asyncio.run(go_alerts.run_go_alert_cycle(None)) == 1
    assert len(sent) == 1 and "GO DIGEST" in sent[0] and "WLD" in sent[0]
    # 2nd entrant 1h later: inside the window → ACCUMULATE, no push.
    st = go_alerts._load_state()
    st["last_batch_send"] = st["last_batch_send"]  # sanity: field persisted
    _patch_screen(go_alerts, mp, _Res([_Row("WLD"), _Row("STRK")]))
    assert asyncio.run(go_alerts.run_go_alert_cycle(None)) == 0
    assert len(sent) == 1
    assert "STRK" in str(go_alerts._load_state().get("pending", {}))
    # Window elapses → next cycle flushes the pending digest.
    st = go_alerts._load_state()
    st["last_batch_send"] = st["last_batch_send"] - 3.1 * 3600.0
    go_alerts._save_state(st)
    assert asyncio.run(go_alerts.run_go_alert_cycle(None)) == 1
    assert len(sent) == 2 and "STRK" in sent[1]
    assert go_alerts._load_state().get("pending") == {}


def test_go_regime_flip_bypasses_batch(go, monkeypatch):
    go_alerts, sent, mp = go
    mp.setenv("GO_ALERT_BATCH_HOURS", "3")
    _patch_screen(go_alerts, mp, _Res([]))
    asyncio.run(go_alerts.run_go_alert_cycle(None))            # seed
    rows = [_Row(f"T{i}") for i in range(7)]
    _patch_screen(go_alerts, mp, _Res(rows))
    assert asyncio.run(go_alerts.run_go_alert_cycle(None)) == 1
    assert len(sent) == 1 and "REGIME FLIP" in sent[0]


def test_go_env_zero_per_run_behavior(go, monkeypatch):
    go_alerts, sent, mp = go
    mp.setenv("GO_ALERT_BATCH_HOURS", "0")
    _patch_screen(go_alerts, mp, _Res([]))
    asyncio.run(go_alerts.run_go_alert_cycle(None))            # seed
    _patch_screen(go_alerts, mp, _Res([_Row("WLD")]))
    assert asyncio.run(go_alerts.run_go_alert_cycle(None)) == 1
    _patch_screen(go_alerts, mp, _Res([_Row("WLD"), _Row("STRK")]))
    assert asyncio.run(go_alerts.run_go_alert_cycle(None)) == 1  # per-run push
    assert len(sent) == 2 and "GO DIGEST" not in sent[0]


# ─────────────────────────────────────────────────────────────────────────────
# Part 2.4 — generic dedup guard
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def dd(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import config
    importlib.reload(config)
    import modules.alert_dedup as alert_dedup
    importlib.reload(alert_dedup)
    yield alert_dedup
    monkeypatch.delenv("DATA_DIR")
    importlib.reload(config)


def test_dedup_blocks_identical_within_cooldown(dd):
    t = 1_000_000.0
    assert dd.should_emit("x", "BTC", "s1", cooldown_hours=6,
                          material={"v": 1.0}, now=t) is True
    assert dd.should_emit("x", "BTC", "s1", cooldown_hours=6,
                          material={"v": 1.0}, now=t + 60) is False
    # Cooldown lapse re-allows.
    assert dd.should_emit("x", "BTC", "s1", cooldown_hours=6,
                          material={"v": 1.0}, now=t + 6.1 * 3600) is True


def test_dedup_material_change_and_state_change_emit(dd):
    t = 1_000_000.0
    dd.should_emit("x", "ETH", "s1", cooldown_hours=6, material={"v": 1.0}, now=t)
    assert dd.should_emit("x", "ETH", "s1", cooldown_hours=6,
                          material={"v": 2.0}, now=t + 60) is True   # material
    assert dd.should_emit("x", "ETH", "s2", cooldown_hours=6,
                          material={"v": 2.0}, now=t + 120) is True  # state
    # Tolerance suppresses sub-threshold numeric drift.
    assert dd.should_emit("x", "ETH", "s2", cooldown_hours=6,
                          material={"v": 2.01}, tolerance=0.02,
                          now=t + 180) is False
    # Entities are independent; clear() re-arms.
    assert dd.should_emit("x", "SOL", "s1", cooldown_hours=6, now=t + 200) is True
    dd.clear("x", "ETH")
    assert dd.should_emit("x", "ETH", "s2", cooldown_hours=6,
                          material={"v": 2.0}, now=t + 240) is True


# ─────────────────────────────────────────────────────────────────────────────
# Part 3 — DreamCash idle USDC surfaces in PORTFOLIO CONSOLIDADO
# ─────────────────────────────────────────────────────────────────────────────

DREAMCASH = "0x171b7880939d76abbc6b6b2094f54e6636f829a7"


def test_dreamcash_idle_usdc_counted():
    from templates.formatters import _estimate_spot_split
    spot = [{"coin": "USDC", "total": 1138.79}]
    ns, st = _estimate_spot_split(spot, perp_account_value=0.0,
                                  wallet_addr=DREAMCASH)
    assert ns == pytest.approx(0.0)
    assert st == pytest.approx(1138.79)


def test_dreamcash_cash_renders_in_consolidado():
    from templates.formatters import format_quick_positions
    wallets = [{
        "status": "ok",
        "data": {
            "wallet": DREAMCASH,
            "label": "DreamCash (RESCATE/HEDGE)",
            "account_value": 0.0,
            "positions": [],
            "spot_balances": [{"coin": "USDC", "total": 1138.79}],
            "unrealized_pnl_total": 0.0,
        },
    }]
    out = format_quick_positions(wallets, [])
    assert "PORTFOLIO CONSOLIDADO" in out
    assert "Spot USDC idle $1.1K" in out          # NOT 0.00
    assert "Capital Total: $1.1K" in out
