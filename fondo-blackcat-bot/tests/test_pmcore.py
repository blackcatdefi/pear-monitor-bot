"""R-PMCORE (2026-06-01) regression tests.

Locks in the post-migration behaviour:
  1. HYPE spot collateral valued at LIVE oracle price (NOT entryNtl=0).
  2. Portfolio Margin state: collateral/debt/capacity/ratio + thresholds.
  3. Naked-long guard (debt drawn, no shorts) breaks silence.
  4. PM ratio stays silent below WARN.
  5. Flywheel deprecation drops stale HL collateral from TOTAL EQUITY.
  6. Per-vault max drawdown from the SQLite series.
"""
from __future__ import annotations

import pytest


# ─── 1. HYPE spot valued at oracle price, not cost basis ────────────────────
def test_hype_spot_valued_at_oracle_not_entry_ntl():
    from templates import formatters as fmt
    # Migrated HYPE: entry_ntl is 0.0 (the bug surface). At oracle it's worth
    # qty × price; the old code returned 0.0 here.
    bal = [{"coin": "HYPE", "total": 1049.07, "entry_ntl": 0.0}]
    prices = {"HYPE": 71.5}
    old = fmt._estimate_spot_split(bal, 0.0, None)[0]
    new = fmt._estimate_spot_split(bal, 0.0, prices)[0]
    assert old == 0.0  # the pre-fix behaviour (cost basis 0)
    assert round(new, 0) == round(1049.07 * 71.5, 0)  # ~$75K
    assert new > 70_000


def test_khype_uses_hype_price_proxy():
    from templates import formatters as fmt
    bal = [{"coin": "kHYPE", "total": 100.0, "entry_ntl": 0.0}]
    ns, _ = fmt._estimate_spot_split(bal, 0.0, {"HYPE": 70.0})
    assert round(ns, 0) == 7000.0


def test_stablecoins_skipped_when_perp_active():
    from templates import formatters as fmt
    bal = [
        {"coin": "USDC", "total": 5000, "entry_ntl": 5000},
        {"coin": "HYPE", "total": 10, "entry_ntl": 0},
    ]
    ns, st = fmt._estimate_spot_split(bal, perp_account_value=1000.0, prices={"HYPE": 70})
    assert st == 0.0  # stables folded into perp margin (Unified Account)
    assert round(ns, 0) == 700.0


# ─── 2 & 4. PM state + thresholds + silent below WARN ───────────────────────
def test_pm_state_unleveraged_calm_and_silent():
    from modules.portfolio_margin import compute_pm_state, pm_alert
    bal = [{"coin": "HYPE", "total": 1000.0}, {"coin": "USDC", "total": 0.5}]
    pm = compute_pm_state(bal, [], {"HYPE": 70.0})
    assert round(pm.collateral_usd, 0) == 70_000
    assert pm.debt_usd == 0.0
    assert round(pm.capacity_usd, 0) == 35_000  # 0.5 × collateral
    assert pm.ratio == 0.0
    assert pm.status == "CALM"
    should, _ = pm_alert(pm)
    assert should is False  # stays silent below WARN


def test_pm_ratio_thresholds():
    from modules.portfolio_margin import compute_pm_state
    # collateral 100k → capacity 50k. debt 20k → ratio 0.40 = WARN.
    bal_warn = [{"coin": "HYPE", "total": 1000.0}, {"coin": "USDC", "total": -20_000}]
    assert compute_pm_state(bal_warn, [], {"HYPE": 100.0}).status == "WARN"
    bal_stress = [{"coin": "HYPE", "total": 1000.0}, {"coin": "USDC", "total": -35_000}]
    assert compute_pm_state(bal_stress, [], {"HYPE": 100.0}).status == "STRESS"
    bal_liq = [{"coin": "HYPE", "total": 1000.0}, {"coin": "USDC", "total": -48_000}]
    assert compute_pm_state(bal_liq, [], {"HYPE": 100.0}).status == "LIQ"


# ─── 3. Naked-long guard ────────────────────────────────────────────────────
def test_naked_long_guard_fires_when_debt_no_shorts():
    from modules.portfolio_margin import compute_pm_state, pm_alert, format_pm_state_telegram
    bal = [{"coin": "HYPE", "total": 1000.0}, {"coin": "USDC", "total": -10_000}]
    pm = compute_pm_state(bal, [], {"HYPE": 100.0})  # no positions = no shorts
    assert pm.naked_long is True
    should, msg = pm_alert(pm)
    assert should is True
    assert "hedge missing" in msg.lower()
    assert "naked leveraged long" in format_pm_state_telegram(pm).lower()


def test_naked_long_guard_clears_with_shorts_open():
    from modules.portfolio_margin import compute_pm_state
    bal = [{"coin": "HYPE", "total": 1000.0}, {"coin": "USDC", "total": -10_000}]
    positions = [{"coin": "ENA", "size": -500.0, "notional_usd": 8000.0}]
    pm = compute_pm_state(bal, positions, {"HYPE": 100.0})
    assert pm.shorts_notional == 8000.0
    assert pm.naked_long is False


# ─── 5. Flywheel deprecation drops stale HL from equity ─────────────────────
def test_flywheel_deprecation_excludes_stale_hl(monkeypatch):
    """With FLYWHEEL_DEPRECATED on, the destacado header must NOT add stale
    HL collateral to TOTAL EQUITY (HYPE now lives in spot)."""
    from templates import formatters as fmt
    monkeypatch.setattr(
        "modules.hl_prices.get_oracle_prices", lambda force=False: {"HYPE": 70.0}
    )
    # Isolate: zero live vault deposits so we measure ONLY the HL-vs-spot fold.
    monkeypatch.setattr(
        "modules.vault_deposits.get_vault_deposits_total", lambda force=False: 0.0
    )
    wallets = [{
        "status": "ok",
        "data": {
            "wallet": "0xc7ae23316b47f7e75f455f53ad37873a18351505",
            "label": "BlackCatDeFi EVM",
            "account_value": 0.0,
            "spot_balances": [{"coin": "HYPE", "total": 1000.0, "entry_ntl": 0.0}],
            "positions": [],
        },
    }]
    # Stale flywheel HL collateral that must be IGNORED in equity.
    hl = [{"status": "ok", "data": {
        "wallet": "0xa44eaaaa", "total_collateral_usd": 73000.0,
        "total_debt_usd": 45000.0,
    }}]
    header = fmt.format_report_header(wallets, hl, {"status": "error"})
    # P1.4: the legacy "HF FLYWHEEL: CERRADO" KPI is replaced by the live
    # PM-core health band. Deprecation indicator is now the PM SALUD line.
    assert "PM SALUD" in header
    assert "HF FLYWHEEL" not in header
    # TOTAL EQUITY ≈ HYPE spot ($70K), NOT inflated by stale $73K HL collateral.
    # If HL were still counted, net would jump by +$28K (col-debt).
    import re
    m = re.search(r"TOTAL EQUITY: \$([\d.]+)K", header)
    assert m is not None
    val = float(m.group(1))
    assert 65.0 <= val <= 80.0  # ~$70K HYPE only, no stale HL net added


# ─── 6. Per-vault max drawdown ──────────────────────────────────────────────
def test_max_drawdown_from_series(tmp_path):
    from modules import vault_history as vh
    db = str(tmp_path / "vh.db")
    from dataclasses import dataclass

    @dataclass
    class _D:
        vault_address: str = "0xvault"
        label: str = "Test"
        equity_usd: float = 0.0
        cost_basis_usd: float = 1000.0
        pnl_usd: float = 0.0
        found: bool = True

    from datetime import datetime, timezone, timedelta
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    # equity path: 1000 → 1200 (peak) → 900 (trough, -25% from 1200) → 1100
    for i, eq in enumerate([1000, 1200, 900, 1100]):
        vh.record_vault_snapshot([_D(equity_usd=eq)], now=base + timedelta(days=i), db_path=db)
    mdd = vh.compute_max_drawdown("0xvault", db_path=db)
    assert mdd["has_data"] is True
    assert round(mdd["mdd_pct"], 1) == 25.0  # (1200-900)/1200


def test_max_drawdown_monotonic_rise_is_zero(tmp_path):
    from modules import vault_history as vh
    from dataclasses import dataclass

    @dataclass
    class _D:
        vault_address: str = "0xup"
        label: str = "Up"
        equity_usd: float = 0.0
        cost_basis_usd: float = 1000.0
        pnl_usd: float = 0.0
        found: bool = True

    from datetime import datetime, timezone, timedelta
    db = str(tmp_path / "vh.db")
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    for i, eq in enumerate([1000, 1050, 1100, 1200]):
        vh.record_vault_snapshot([_D(equity_usd=eq)], now=base + timedelta(days=i), db_path=db)
    assert vh.compute_max_drawdown("0xup", db_path=db)["mdd_pct"] == 0.0
