"""R-DREAMCASH-UNIFIED (2026-06-30) — pin the unified-account equity rule.

DreamCash (0x171b…, RESCATE/HEDGE wallet) is a HyperLiquid UNIFIED account:
the SAME spot USDC balance collateralises spot and perps simultaneously. The
perp ``marginSummary.accountValue`` is therefore NOT additional capital — it is
that same spot USDC (the locked-margin portion) plus unrealised PnL.

The previous fix (R-DREAMCASH-EQUITY / 461f023) mis-modelled it as SEPARATE
margin and summed ``spot_reserve ($25K) + perp_accountValue ($22K) = $47K``,
double-counting the collateral. The correct unified equity is:

    equity = spot_value (counted ONCE) + perp_UPnL      # NEVER + accountValue

So ``_estimate_spot_split`` still KEEPS the full spot USDC reserve (counted
once), and ``_wallet_perp_contribution`` returns ONLY the wallet's unrealised
PnL for these wallets — never the accountValue. Every other wallet keeps the
unified-margin default (perp = accountValue, spot stables skipped while a perp
is active), so its collateral is still counted exactly once.

These tests must stay green so the $47K double-count can never return.
"""
from __future__ import annotations

from templates.formatters import (
    _estimate_spot_split,
    _is_unified_spot_reserve_wallet,
    _is_separate_margin_wallet,  # deprecated alias — kept working on purpose
    _wallet_perp_contribution,
    _UNIFIED_SPOT_RESERVE_WALLETS,
    _SEPARATE_MARGIN_WALLETS,
)

DREAMCASH = "0x171b7880939d76abbc6b6b2094f54e6636f829a7"
C7AE = "0xc7ae23316b47f7e75f455f53ad37873a18351505"


def _dreamcash_bag() -> list[dict]:
    return [
        {"coin": "USDC", "total": "24995.00", "entry_ntl": "0"},
        {"coin": "HYPE", "total": "0.00964914", "entry_ntl": "0"},
    ]


# ── membership (case-insensitive) + deprecated alias still resolves ──────────
def test_dreamcash_is_unified_spot_reserve_case_insensitive():
    assert _is_unified_spot_reserve_wallet(DREAMCASH) is True
    assert _is_unified_spot_reserve_wallet(DREAMCASH.upper()) is True
    assert _is_unified_spot_reserve_wallet("  " + DREAMCASH + "  ") is True
    assert DREAMCASH in _UNIFIED_SPOT_RESERVE_WALLETS


def test_deprecated_separate_margin_alias_still_works():
    # The old name was renamed but kept as an alias so nothing breaks.
    assert _is_separate_margin_wallet is _is_unified_spot_reserve_wallet
    assert _SEPARATE_MARGIN_WALLETS is _UNIFIED_SPOT_RESERVE_WALLETS
    assert _is_separate_margin_wallet(DREAMCASH) is True


def test_unknown_wallet_is_not_unified_spot_reserve():
    assert _is_unified_spot_reserve_wallet(None) is False
    assert _is_unified_spot_reserve_wallet("") is False
    assert _is_unified_spot_reserve_wallet(C7AE) is False


# ── spot split: DreamCash keeps its full USDC reserve (counted ONCE) ─────────
def test_dreamcash_keeps_full_spot_reserve_with_active_perp():
    # Active perp + unified-spot-reserve wallet → the $25K USDC reserve is
    # COUNTED in full (available + locked-margin portion alike). The locked
    # part is NOT double-counted because the perp side contributes only UPnL.
    ns, st = _estimate_spot_split(
        _dreamcash_bag(), perp_account_value=22_056.40,
        prices={"HYPE": 65.0}, wallet_addr=DREAMCASH,
    )
    assert st == 24_995.00           # full reserve preserved (not just free $)
    assert ns == 65.0 * 0.00964914   # HYPE valued at live price


def test_non_reserve_wallet_still_skips_stables_with_active_perp():
    # Same bag, but NOT a unified-spot-reserve wallet → unified-account skip
    # stands (its USDC is the perp margin pool, captured via accountValue).
    ns, st = _estimate_spot_split(
        _dreamcash_bag(), perp_account_value=22_056.40,
        prices={"HYPE": 65.0}, wallet_addr="0xdeadbeef",
    )
    assert st == 0.0
    assert ns == 65.0 * 0.00964914


def test_no_wallet_addr_preserves_legacy_behaviour():
    _, st_active = _estimate_spot_split(_dreamcash_bag(), perp_account_value=5_000.0)
    assert st_active == 0.0
    _, st_idle = _estimate_spot_split(_dreamcash_bag(), perp_account_value=0.0)
    assert st_idle == 24_995.00


# ── perp contribution: UPnL for DreamCash, accountValue for everyone else ────
def test_dreamcash_perp_contributes_only_upnl_not_accountvalue():
    # THE regression that pins the fix: with a $22K accountValue and $291 UPnL,
    # DreamCash must contribute ONLY the $291 — never the $22K (which is the
    # same spot collateral already counted in the reserve).
    d = {
        "wallet": DREAMCASH,
        "account_value": 22_056.40,
        "unrealized_pnl_total": 291.0,
    }
    assert _wallet_perp_contribution(d) == 291.0


def test_normal_wallet_perp_contributes_accountvalue():
    d = {
        "wallet": "0xdeadbeef",
        "account_value": 22_056.40,
        "unrealized_pnl_total": 291.0,
    }
    assert _wallet_perp_contribution(d) == 22_056.40


def test_perp_contribution_handles_bad_data():
    assert _wallet_perp_contribution({"wallet": DREAMCASH}) == 0.0
    assert _wallet_perp_contribution({"wallet": DREAMCASH,
                                      "unrealized_pnl_total": "x"}) == 0.0
    assert _wallet_perp_contribution({"wallet": "0xabc",
                                      "account_value": None}) == 0.0


# ── end-to-end wallet equity: spot + UPnL, NOT spot + accountValue ───────────
def test_dreamcash_wallet_equity_is_spot_plus_upnl_not_plus_accountvalue():
    # Reconcile the per-wallet Capital Total the way /reporte builds it:
    #   capital = perp_contribution + spot_non_stable + spot_reserve
    # For DreamCash that is UPnL + HYPE + USDC = ~$25.3K, NOT the broken $47K.
    d = {
        "wallet": DREAMCASH,
        "account_value": 22_056.40,
        "unrealized_pnl_total": 291.0,
    }
    ns, st = _estimate_spot_split(
        _dreamcash_bag(), perp_account_value=d["account_value"],
        prices={"HYPE": 65.0}, wallet_addr=DREAMCASH,
    )
    capital = _wallet_perp_contribution(d) + ns + st
    # ~$25.3K (24,995 reserve + 0.63 HYPE + 291 UPnL), emphatically NOT ~$47K.
    assert abs(capital - (24_995.0 + 65.0 * 0.00964914 + 291.0)) < 1e-6
    assert capital < 26_000.0
    # The broken model (accountValue + reserve) would have been ~$47K:
    broken = d["account_value"] + ns + st
    assert broken > 46_000.0      # documents what we must never produce
