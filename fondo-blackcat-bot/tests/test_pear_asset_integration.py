"""R-PEAR-ASSET-INTEGRATION — tests for the two-asset equity + HYPE metrics.

Covers the four contractual guarantees of the round:

1. stPEAR is valued LIVE (balance × price) and, with a large balance, the
   PEAR component is far above the retired static $1.2K hardcode.
2. Fund equity INCLUDES the PEAR component as a first-class line.
3. A fetch failure (balance OR price) yields n/d — NEVER a fabricated or
   stale value, and PEAR contributes 0 to equity.
4. The negative-equity "Rabby parity" artifact is suppressed in favour of the
   oracle computation when collateral is clearly six-figure.

Plus the HYPE acquisition module: real PPC/net when fills reconcile, n/d when
they don't (migrated/bridged balance or truncated fill page).

All network is monkeypatched — these tests never hit Arbitrum or HL.
"""
from __future__ import annotations

import pytest

from auto.capital_calc import (
    compute_net_capital,
    format_net_capital_telegram,
    render_net_capital_html,
)

# Old retired hardcode the round replaces.
OLD_STATIC_PEAR_USD = 1_224.0

# Live-shaped 0xc7ae numbers (HYPE six-figure collateral, large PM borrow).
HYPE_VALUE = 223_787.0
PM_BORROW = 112_947.0
STPEAR_BAL = 158_601.72
PEAR_PX = 0.015891
PEAR_VALUE = STPEAR_BAL * PEAR_PX  # ≈ $2,520


def _live_dict(**over):
    d = {
        "hl_collateral_total": 0.0,
        "hl_debt_total": 0.0,
        "perp_equity_total": 0.0,
        "spot_usd_total": HYPE_VALUE,
        "spot_stables_total": 0.37,
        "spot_borrow_total": PM_BORROW,
        "upnl_perp_total": 0.0,
        "vault_deposits_total": 0.0,
        "pear_staked_total": PEAR_VALUE,
        "pear_staked_balance": STPEAR_BAL,
        "pear_staked_price": PEAR_PX,
        "pear_staked_known": True,
    }
    d.update(over)
    return d


# ─── 1. stPEAR valued live, well above the old $1.2K ────────────────────────
def test_stpear_value_exceeds_old_static_when_balance_large():
    net = compute_net_capital(_live_dict())
    assert net.pear_staked_usd == pytest.approx(PEAR_VALUE, abs=1.0)
    # The whole point of the round: real value > the retired $1.2K hardcode.
    assert net.pear_staked_usd > OLD_STATIC_PEAR_USD
    # The detailed balance × price round-trips into the dataclass.
    assert net.pear_staked_balance == pytest.approx(STPEAR_BAL)
    assert net.pear_staked_price == pytest.approx(PEAR_PX)


def test_telegram_line_shows_balance_times_price():
    tg = format_net_capital_telegram(compute_net_capital(_live_dict()))
    assert "PEAR (2º activo)" in tg
    assert "stPEAR" in tg
    # balance and the multiplication are both rendered.
    assert "158,602 stPEAR" in tg or "158,601 stPEAR" in tg
    assert "×" in tg


# ─── 2. Equity includes the PEAR component ──────────────────────────────────
def test_total_equity_includes_pear_component():
    net = compute_net_capital(_live_dict())
    expected = (
        net.net_total_usd
        + net.spot_stables_usd
        + net.pear_staked_usd
        - net.spot_borrow_usd
    )
    assert net.total_equity_usd == pytest.approx(expected, abs=0.5)
    # Removing PEAR changes the headline by exactly the PEAR value.
    no_pear = compute_net_capital(
        _live_dict(pear_staked_total=0.0, pear_staked_balance=0.0,
                   pear_staked_price=0.0)
    )
    delta = net.total_equity_usd - no_pear.total_equity_usd
    assert delta == pytest.approx(PEAR_VALUE, abs=1.0)


# ─── 3. Fetch failure → n/d, never fabricated / stale ───────────────────────
def test_pear_fetch_failure_renders_nd_and_excludes_from_equity():
    net = compute_net_capital(
        _live_dict(pear_staked_known=False, pear_staked_total=0.0,
                   pear_staked_balance=0.0, pear_staked_price=0.0)
    )
    # Contributes nothing to equity (no fabricated/stale number).
    assert net.pear_staked_usd == 0.0
    tg = format_net_capital_telegram(net)
    assert "PEAR (2º activo): n/d" in tg
    # The old static value must NEVER appear.
    assert "1,224" not in tg and "$1.2K" not in tg
    html = render_net_capital_html(
        net,
        fmt_compact_usd=lambda v: f"${float(v) / 1000:.1f}K",
        signed=lambda v: ("pos" if v >= 0 else "neg", f"${v:+.2f}"),
    )
    assert "n/d" in html


def test_pear_known_false_overrides_any_supplied_value():
    """Even if a value sneaks in, known=False forces 0 (never stale)."""
    net = compute_net_capital(
        _live_dict(pear_staked_known=False, pear_staked_total=9999.0)
    )
    assert net.pear_staked_usd == 0.0


# ─── 4. Negative-equity parity artifact suppressed ──────────────────────────
def test_negative_equity_artifact_suppressed_six_figure_lag():
    """Collateral feed lags → raw total slightly negative while HYPE is
    six-figure. Guard fires; headline uses the oracle computation, never the
    nonsense negative."""
    # spot lags low so net + stables + pear - borrow < 0.
    laggy_spot = 109_000.0
    net = compute_net_capital(_live_dict(spot_usd_total=laggy_spot))
    raw = laggy_spot + 0.37 + PEAR_VALUE - PM_BORROW
    assert raw < 0  # the artifact the round kills
    assert net.parity_stale is True
    assert net.total_equity_usd > 0  # never prints the negative
    tg = format_net_capital_telegram(net)
    assert "parity feed STALE" in tg


def test_total_collapse_when_price_feed_dies():
    """HYPE oracle dies → spot ~0 while a six-figure borrow remains. Borrow
    exceeds all visible collateral (impossible live) → guard fires."""
    net = compute_net_capital(_live_dict(spot_usd_total=0.0))
    assert net.parity_stale is True
    assert net.total_equity_usd >= 0


def test_healthy_account_not_flagged_stale():
    net = compute_net_capital(_live_dict())
    assert net.parity_stale is False
    assert net.total_equity_usd == pytest.approx(
        HYPE_VALUE + 0.37 + PEAR_VALUE - PM_BORROW, abs=1.0
    )


def test_no_borrow_never_flagged_stale():
    """A genuinely small/zero account with no borrow is never mislabeled."""
    net = compute_net_capital(
        _live_dict(spot_usd_total=0.0, spot_borrow_total=0.0,
                   spot_stables_total=0.0, pear_staked_total=0.0,
                   pear_staked_balance=0.0)
    )
    assert net.parity_stale is False


# ─── PEAR staking reader contract (monkeypatched, no network) ───────────────
def test_pear_staking_reader_nd_on_price_failure(monkeypatch):
    import modules.pear_staking as ps

    monkeypatch.setattr(ps, "_fetch_stpear_balance", lambda: STPEAR_BAL)

    def _boom():
        raise RuntimeError("all PEAR price feeds failed")

    monkeypatch.setattr(ps, "_fetch_pear_price", _boom)
    res = ps.get_pear_staked(force=True)
    assert res.ok is False
    assert res.known is False
    assert res.value_usd is None  # never fabricated
    fields = ps.pear_staked_capital_fields(force=True)
    assert fields["pear_staked_known"] is False
    assert fields["pear_staked_total"] == 0.0


def test_pear_staking_reader_live_value(monkeypatch):
    import modules.pear_staking as ps

    monkeypatch.setattr(ps, "_fetch_stpear_balance", lambda: STPEAR_BAL)
    monkeypatch.setattr(ps, "_fetch_pear_price", lambda: (PEAR_PX, "defillama"))
    res = ps.get_pear_staked(force=True)
    assert res.ok is True and res.known is True
    assert res.value_usd == pytest.approx(STPEAR_BAL * PEAR_PX, abs=1.0)
    assert res.value_usd > OLD_STATIC_PEAR_USD


# ─── HYPE acquisition reliability gate ──────────────────────────────────────
def test_hype_acq_nd_when_fills_dont_reconcile(monkeypatch):
    import modules.hype_acquisition as ha

    # 1,987 HYPE of buys but 3,006 on-chain → 34% gap → n/d.
    fills = [
        {"coin": "@107", "side": "B", "dir": "Buy", "px": "65.0", "sz": "1987.38"}
    ]
    monkeypatch.setattr(ha, "_resolve_spot_map", lambda: {"@107": "HYPE"})
    monkeypatch.setattr(ha, "_fetch_fills", lambda w: fills)
    monkeypatch.setattr(ha, "_live_hype_balance", lambda w: 3006.28)
    acq = ha.compute_hype_acquisition("0xc7ae")
    assert acq.known is False
    assert acq.ppc_usd is None and acq.net_acq_usd is None
    assert "reconcil" in (acq.reason or "")
    line = ha.format_hype_acquisition_line(acq)
    assert "n/d" in line


def test_hype_acq_real_ppc_when_fills_reconcile(monkeypatch):
    import modules.hype_acquisition as ha

    fills = [
        {"coin": "HYPE", "side": "B", "dir": "Buy", "px": "60.0", "sz": "600"},
        {"coin": "HYPE", "side": "B", "dir": "Buy", "px": "70.0", "sz": "400"},
        {"coin": "HYPE", "side": "A", "dir": "Sell", "px": "80.0", "sz": "100"},
    ]
    monkeypatch.setattr(ha, "_resolve_spot_map", lambda: {})
    monkeypatch.setattr(ha, "_fetch_fills", lambda w: fills)
    monkeypatch.setattr(ha, "_live_hype_balance", lambda w: 900.0)  # 1000-100
    acq = ha.compute_hype_acquisition("0xc7ae")
    assert acq.known is True
    # PPC = (600*60 + 400*70)/1000 = 64.0 (sells don't move it).
    assert acq.ppc_usd == pytest.approx(64.0, abs=0.01)
    # net acq = (64000 - 8000) / 900 = 62.222…
    assert acq.net_acq_usd == pytest.approx(56000.0 / 900.0, abs=0.01)


def test_hype_acq_nd_when_no_hype_fills(monkeypatch):
    import modules.hype_acquisition as ha

    monkeypatch.setattr(ha, "_resolve_spot_map", lambda: {})
    monkeypatch.setattr(ha, "_fetch_fills", lambda w: [{"coin": "BTC", "side": "B",
                                                        "px": "1", "sz": "1"}])
    monkeypatch.setattr(ha, "_live_hype_balance", lambda w: 3006.0)
    acq = ha.compute_hype_acquisition("0xc7ae")
    assert acq.known is False
    assert "migrado" in (acq.reason or "") or "bridged" in (acq.reason or "")
