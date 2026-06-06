"""R-WALLET-FIX (2026-06-06) — Rabby/DeBank parity with the PM USDC borrow.

The bug
-------
The fund migrated its core capital into a HyperLiquid Portfolio Margin
position: spot HYPE is cross collateral and USDC is borrowed against it. The
borrow shows in ``spotClearinghouseState`` as a balance with a NEGATIVE
``total`` and a positive ``borrowed`` field::

    {"coin": "USDC", "total": "-10740.32", "borrowed": "39808.57"}
    {"coin": "HYPE", "total": "1317.0252", "supplied": "1317.0252", "ltv": "0.5"}

Two defects ignored that debt and overstated equity by ~$40K:

1. ``modules.portfolio._fetch_spot`` dropped EVERY balance with
   ``total <= 0`` → the borrow row vanished before any accounting saw it.
2. ``compute_pm_state`` read debt from the NET negative ``total`` (which
   under-counts the borrowed dollars already swept into perp) instead of the
   authoritative ``borrowed`` field → the KPI rendered "deuda $0 / 0% CALM".

Ground truth (6-Jun-2026, captured from Rabby):
  * HYPE supplied   ~$76,687   (1,317.0252 HYPE @ ~$58.23)
  * USDC borrowed    $39,807.72  (the real liability)
  * Perp account    ~$29,247    (BTC margin $16,072 + SOL $2,475 + free USDC
                                 $10,700, already net of perp borrow)
  * Pear staked      ~$1,245
  → wallet 0xc7ae   ~$66.1K   ;   fund total ~$67.4K
  → lending NET     ~$36,879  = HYPE $76,687 − borrowed $39,807.72
  → health factor   ~0.96     ;   borrow ratio > 100% (over the 50% LTV cap)

The correct net-worth formula is:

    wallet = HYPE_value − borrowed + perp_accountValue

NOT ``HYPE_value + perp_accountValue`` (which drops the debt) and NOT
``HYPE_value − net_USDC_total + perp`` (which under-counts the swept borrow).
"""
from __future__ import annotations

import pytest

from auto.capital_calc import compute_net_capital, format_net_capital_telegram
from modules.portfolio_margin import compute_pm_state, format_pm_state_telegram
from modules.portfolio_snapshot import _spot_native_borrow, _spot_split_value


# ─── 6-Jun ground-truth shape ───────────────────────────────────────────────
HYPE_QTY = 1317.0252
HYPE_PX = 58.23                 # → HYPE value ≈ $76,690
HYPE_VALUE = HYPE_QTY * HYPE_PX
BORROWED = 39_807.72
PERP_ACCOUNT_VALUE = 29_247.0   # BTC 16,072 + SOL 2,475 + free 10,700
PEAR_STAKED = 1_245.0

# Raw spot ledger as it arrives from the (now-fixed) fetcher: the borrow row
# is preserved with its negative total AND its authoritative ``borrowed``.
SPOT_BALANCES = [
    {"coin": "USDC", "total": -10_740.32, "borrowed": BORROWED, "ltv": 0.0},
    {"coin": "HYPE", "total": HYPE_QTY, "supplied": HYPE_QTY, "ltv": 0.5,
     "entry_ntl": 0.0},
    {"coin": "USDT0", "total": 0.0136, "entry_ntl": 0.0136},
]

WALLET_TARGET = 66_126.0   # HYPE_value − borrowed + perp
FUND_TARGET = WALLET_TARGET + PEAR_STAKED  # ≈ $67.4K


def _snapshot_dict() -> dict:
    """Build the canonical totals dict the way portfolio_snapshot aggregates
    a single PM wallet: HYPE gross in spot_non_stable, perp accountValue in
    perp, the full borrow surfaced as the new ``spot_borrow_total`` liability.
    """
    non_stable, stables = _spot_split_value(SPOT_BALANCES, {"HYPE": {"price_usd": HYPE_PX}},
                                            PERP_ACCOUNT_VALUE)
    borrow = _spot_native_borrow(SPOT_BALANCES)
    return {
        "hl_collateral_total": 0.0,     # HyperLend flywheel CLOSED
        "hl_debt_total": 0.0,
        "perp_equity_total": PERP_ACCOUNT_VALUE,
        "spot_usd_total": non_stable,   # HYPE gross
        "spot_stables_total": stables,  # dust only (borrow excluded)
        "spot_borrow_total": borrow,    # the real liability
        "upnl_perp_total": -3_191.0,    # already inside perp accountValue
        "pear_staked_total": PEAR_STAKED,
    }


# ─── _spot_native_borrow reads the authoritative `borrowed` field ───────────
def test_native_borrow_uses_borrowed_field_not_net_total():
    borrow = _spot_native_borrow(SPOT_BALANCES)
    assert abs(borrow - BORROWED) < 1.0           # ~$39.8K, NOT $10.7K net
    assert borrow > 39_000                          # definitely not $0


def test_native_borrow_falls_back_to_negative_total():
    """Older payloads without a ``borrowed`` field still net the magnitude of
    a negative stable total."""
    legacy = [{"coin": "USDC", "total": -12_345.0}]
    assert abs(_spot_native_borrow(legacy) - 12_345.0) < 0.5


def test_borrow_excluded_from_cash_equivalent_stables():
    """The borrow must never land in the cash-equivalent stables bucket — that
    would double-count it against the subtraction in total_equity."""
    _ns, stables = _spot_split_value(SPOT_BALANCES, {}, PERP_ACCOUNT_VALUE)
    assert stables >= 0.0
    assert stables < 1.0  # only the $0.0136 USDT0 dust, never the -$10.7K USDC


# ─── End-to-end: TOTAL EQUITY matches Rabby (~$67.4K), debt NOT dropped ──────
def test_total_equity_nets_the_borrow_to_rabby():
    net = compute_net_capital(_snapshot_dict())
    # Fund total ≈ $67.4K (wallet $66.1K + Pear $1.2K), within 2%.
    assert abs(net.total_equity_usd - FUND_TARGET) < FUND_TARGET * 0.02
    # The borrow is carried as a real liability, not zero.
    assert abs(net.spot_borrow_usd - BORROWED) < 1.0
    # Net equity = collateral(gross HYPE) + perp − borrow (+ pear).
    expected = HYPE_VALUE + PERP_ACCOUNT_VALUE - BORROWED + PEAR_STAKED
    assert abs(net.total_equity_usd - expected) < 5.0


def test_gross_is_never_the_headline():
    """Pre-fix the bot showed ~$103K (HYPE gross + perp, no borrow). The fixed
    headline must be ~$40K lower — the borrow must move the number."""
    net = compute_net_capital(_snapshot_dict())
    naive_gross = HYPE_VALUE + PERP_ACCOUNT_VALUE + PEAR_STAKED  # no debt netted
    assert net.total_equity_usd < naive_gross - 35_000.0
    assert net.total_equity_usd == pytest.approx(
        naive_gross - BORROWED, abs=5.0
    )


def test_lending_net_is_gross_minus_borrow():
    """HYPE collateral must contribute its NET (~$36.9K), never gross
    (~$76.7K), once the borrow is netted."""
    lending_net = HYPE_VALUE - BORROWED
    assert 35_000 < lending_net < 39_000   # ~$36.9K


def test_telegram_block_shows_real_debt_line():
    net = compute_net_capital(_snapshot_dict())
    block = format_net_capital_telegram(net)
    assert "Deuda PM" in block
    assert "39.8K" in block or "39,808" in block or "39808" in block


# ─── PM KPI: real debt / HF / non-CALM (replaces "deuda $0 / 0% CALM") ───────
def test_pm_state_reads_borrowed_field_not_zero():
    pm = compute_pm_state(SPOT_BALANCES, [], {"HYPE": HYPE_PX})
    assert abs(pm.debt_usd - BORROWED) < 1.0        # ~$39.8K, NOT $0
    assert pm.collateral_usd > 75_000               # gross HYPE collateral


def test_pm_state_health_factor_near_096():
    pm = compute_pm_state(SPOT_BALANCES, [], {"HYPE": HYPE_PX})
    # HF = (collateral × 0.5) / debt ≈ 0.96.
    assert 0.92 <= pm.health_factor <= 1.00
    assert pm.liq_price > 0


def test_pm_kpi_not_calm_when_over_capacity():
    pm = compute_pm_state(SPOT_BALANCES, [], {"HYPE": HYPE_PX})
    assert pm.status != "CALM"                       # the whole point
    assert pm.status in ("STRESS", "LIQ")            # debt > capacity
    assert pm.ratio > 0.95


def test_pm_telegram_renders_debt_hf_not_calm():
    pm = compute_pm_state(SPOT_BALANCES, [], {"HYPE": HYPE_PX})
    block = format_pm_state_telegram(pm)
    assert "Health factor" in block
    # The debt line must NOT read $0.
    assert "Deuda (USDC/USDH borrowed): $0" not in block
    assert "39" in block  # $39.8K appears somewhere in the block


def test_pm_debt_zero_path_still_calm():
    """Unleveraged wallet (no borrow) stays CALM with HF 0 — guards against the
    fix accidentally flagging clean positions."""
    bal = [{"coin": "HYPE", "total": 1000.0}, {"coin": "USDC", "total": 0.5}]
    pm = compute_pm_state(bal, [], {"HYPE": 70.0})
    assert pm.debt_usd == 0.0
    assert pm.status == "CALM"
    assert pm.health_factor == 0.0
    assert pm.liq_price == 0.0
