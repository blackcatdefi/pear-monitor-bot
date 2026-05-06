"""R-DASHBOARD-DOUBLECOUNT-FIX (2026-05-06) — Bug #1 regression lock.

Background
----------
On 2026-05-06 21:04 UTC the dashboard rendered $38.8K total equity vs Rabby
(authoritative) $35.5K — a +$3.3K drift. Root cause: under HyperLiquid's
Unified Account, ``marginSummary.accountValue`` ALREADY includes every
stable spot balance (USDC + USDT0 + USDH + …) that backs perp margin. The
pre-fix ``_spot_split_value`` only special-cased USDC; USDT0/USDH/etc.
were always added to the ``stables_usd`` bucket → ~$2.6K phantom equity
on top of perp accountValue.

Fix surface (locked down by these tests)
----------------------------------------
* ``modules.portfolio_snapshot._spot_split_value`` — when
  ``perp_account_value > 0.01``, EVERY stablecoin in ``STABLECOINS`` is
  skipped (drops out of both ``non_stable`` and ``stables`` halves).
* ``templates.formatters._estimate_spot_split`` — same rule mirrored on
  the formatter side so the Telegram render and the snapshot aggregator
  agree on the cash-equivalent bucket.

Idle wallets (no active perp) still fold ALL stables into the cash bucket
1:1, which is the correct behaviour because there is no margin pool to
double-count against.
"""
from __future__ import annotations

from modules.portfolio_snapshot import STABLECOINS, _spot_split_value
from templates.formatters import _STABLECOINS, _estimate_spot_split


# ─── Active-perp wallet: every stable must drop ────────────────────────────
def test_spot_split_drops_all_stables_when_perp_active_aggregator():
    """``_spot_split_value`` is the canonical aggregator. With perp active,
    USDC AND USDT0 AND USDH must all fall out of the stables bucket — the
    pre-fix code only dropped USDC, leaving the others as phantom equity."""
    spot = [
        {"coin": "USDC", "total": 1500.0, "entry_ntl": 0.0},
        {"coin": "USDT0", "total": 1100.0, "entry_ntl": 0.0},
        {"coin": "USDH", "total": 950.0, "entry_ntl": 0.0},
        {"coin": "DAI", "total": 200.0, "entry_ntl": 0.0},
        # Real exposure — must survive both branches.
        {"coin": "HYPE", "total": 0.5, "entry_ntl": 22.0},
    ]
    non_stable, stables = _spot_split_value(spot, prices={}, perp_account_value=4_700.0)
    assert stables == 0.0  # All stables gone.
    # Non-stable was already valued via entry_ntl fallback (no price map).
    assert non_stable == 22.0


def test_spot_split_drops_all_stables_when_perp_active_formatter():
    """``_estimate_spot_split`` is the formatter-side mirror. Same rule
    must hold so /reporte and /dashboard never disagree on the stables
    bucket."""
    spot = [
        {"coin": "USDC", "total": 1500.0, "entry_ntl": 0.0},
        {"coin": "USDT0", "total": 1100.0, "entry_ntl": 0.0},
        {"coin": "USDH", "total": 950.0, "entry_ntl": 0.0},
        {"coin": "USDE", "total": 300.0, "entry_ntl": 0.0},
        {"coin": "HYPE", "total": 0.5, "entry_ntl": 22.0},
    ]
    non_stable, stables = _estimate_spot_split(spot, perp_account_value=4_700.0)
    assert stables == 0.0
    assert non_stable == 22.0


# ─── Idle wallet: stables must fold into the cash bucket ───────────────────
def test_spot_split_keeps_all_stables_when_perp_idle_aggregator():
    """When ``perp_account_value`` is zero (idle wallet), every stable
    folds into the cash bucket 1:1 — they're not being double-counted by
    a non-existent margin pool."""
    spot = [
        {"coin": "USDC", "total": 1500.0, "entry_ntl": 0.0},
        {"coin": "USDT0", "total": 1100.0, "entry_ntl": 0.0},
        {"coin": "USDH", "total": 950.0, "entry_ntl": 0.0},
    ]
    non_stable, stables = _spot_split_value(spot, prices={}, perp_account_value=0.0)
    assert stables == 1500.0 + 1100.0 + 950.0
    assert non_stable == 0.0


def test_spot_split_keeps_all_stables_when_perp_idle_formatter():
    spot = [
        {"coin": "USDC", "total": 1500.0, "entry_ntl": 0.0},
        {"coin": "USDT0", "total": 1100.0, "entry_ntl": 0.0},
    ]
    non_stable, stables = _estimate_spot_split(spot, perp_account_value=0.0)
    assert stables == 2_600.0
    assert non_stable == 0.0


# ─── Threshold sanity: perp_account_value > 0.01 is the gate ───────────────
def test_perp_threshold_is_one_cent_aggregator():
    """The "active perp" gate is a one-cent floor. A wallet with $0.01 of
    floating perp residue (closed positions, dust) should still drop
    stables — anything > 0.01 means there IS a margin pool aggregating
    them."""
    spot = [{"coin": "USDC", "total": 100.0, "entry_ntl": 0.0}]
    # Just above the threshold → stables drop.
    _, stables_active = _spot_split_value(spot, prices={}, perp_account_value=0.011)
    assert stables_active == 0.0
    # Exactly 0.01 → not yet "active" → fold into cash.
    _, stables_idle = _spot_split_value(spot, prices={}, perp_account_value=0.01)
    assert stables_idle == 100.0


def test_perp_threshold_is_one_cent_formatter():
    spot = [{"coin": "USDC", "total": 100.0, "entry_ntl": 0.0}]
    _, stables_active = _estimate_spot_split(spot, perp_account_value=0.011)
    assert stables_active == 0.0
    _, stables_idle = _estimate_spot_split(spot, perp_account_value=0.01)
    assert stables_idle == 100.0


# ─── Stablecoin set parity between modules ─────────────────────────────────
def test_stablecoin_sets_match_across_modules():
    """``modules.portfolio_snapshot.STABLECOINS`` and
    ``templates.formatters._STABLECOINS`` MUST stay in lockstep — a
    drift means the formatter and aggregator disagree on what counts as
    cash, which is precisely what reopened bug #1."""
    assert STABLECOINS == _STABLECOINS


def test_stablecoin_set_covers_all_known_pegs():
    """Defensive lockdown — every stablecoin we hold MUST be in the set.
    Adding a new stable to the wallet without updating this set re-opens
    the double-count."""
    expected = {
        "USDC", "USDT", "USDT0", "USDH", "USDE", "USDHL", "USR",
        "SUSDE", "DAI",
    }
    assert STABLECOINS == expected


# ─── Mixed bag: only stables drop, non-stables survive ─────────────────────
def test_non_stables_survive_active_perp_branch():
    """The active-perp branch must not accidentally drop non-stables.
    HYPE / USOL / kHYPE etc. are real exposure and belong in the
    non_stable bucket."""
    spot = [
        {"coin": "USDC", "total": 1000.0, "entry_ntl": 0.0},  # drop
        {"coin": "HYPE", "total": 1.0, "entry_ntl": 44.0},    # keep
        {"coin": "USOL", "total": 0.1, "entry_ntl": 18.0},    # keep
        {"coin": "USDT0", "total": 800.0, "entry_ntl": 0.0},  # drop
    ]
    non_stable, stables = _spot_split_value(spot, prices={}, perp_account_value=4_700.0)
    assert stables == 0.0
    assert non_stable == 44.0 + 18.0


# ─── Empty bag stays empty ─────────────────────────────────────────────────
def test_empty_spot_balances_no_crash():
    non_stable, stables = _spot_split_value([], prices={}, perp_account_value=0.0)
    assert non_stable == 0.0
    assert stables == 0.0
    non_stable_f, stables_f = _estimate_spot_split([], perp_account_value=0.0)
    assert non_stable_f == 0.0
    assert stables_f == 0.0


# ─── Coin name normalization is case-insensitive ───────────────────────────
def test_lowercase_coin_names_still_classified_as_stable():
    """Spot bag may surface coin names in any case (HL API has flipped
    casing in the past). Lower-case ``usdc`` must still be recognised as
    a stable so the double-count is killed regardless of API casing."""
    spot = [{"coin": "usdc", "total": 1500.0, "entry_ntl": 0.0}]
    _, stables = _spot_split_value(spot, prices={}, perp_account_value=4_700.0)
    assert stables == 0.0
    _, stables_f = _estimate_spot_split(spot, perp_account_value=4_700.0)
    assert stables_f == 0.0
