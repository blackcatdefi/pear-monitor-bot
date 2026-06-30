"""R-DREAMCASH-EQUITY (2026-06-30) — pin the separate-margin spot rule.

DreamCash (0x171b…, RESCATE/HEDGE wallet) runs perp shorts against a spot
USDC reserve that is a SEPARATE on-chain balance from its perp
``marginSummary.accountValue``. The unified-account double-count guard in
``_estimate_spot_split`` was skipping that reserve whenever a perp was active,
so ~$25K dropped out of both the fund TOTAL EQUITY and DreamCash's per-wallet
Capital Total. The fix scopes the skip: separate-margin wallets keep their
stables; everyone else (the unified-margin default) still skips. The PM-core
wallet 0xc7ae is NOT separate-margin — its perp is idle so the skip never
fired anyway, and these tests prove the default behaviour is unchanged.
"""
from __future__ import annotations

from templates.formatters import (
    _estimate_spot_split,
    _is_separate_margin_wallet,
    _SEPARATE_MARGIN_WALLETS,
)

DREAMCASH = "0x171b7880939d76abbc6b6b2094f54e6636f829a7"


def _dreamcash_bag() -> list[dict]:
    return [
        {"coin": "USDC", "total": "25009.07", "entry_ntl": "0"},
        {"coin": "HYPE", "total": "0.00964914", "entry_ntl": "0"},
    ]


def test_dreamcash_is_separate_margin_case_insensitive():
    assert _is_separate_margin_wallet(DREAMCASH) is True
    assert _is_separate_margin_wallet(DREAMCASH.upper()) is True
    assert _is_separate_margin_wallet("  " + DREAMCASH + "  ") is True
    assert DREAMCASH in _SEPARATE_MARGIN_WALLETS


def test_unknown_wallet_is_not_separate_margin():
    assert _is_separate_margin_wallet(None) is False
    assert _is_separate_margin_wallet("") is False
    assert _is_separate_margin_wallet("0xc7ae23316b47f7e75f455f53ad37873a18351505") is False


def test_dreamcash_keeps_stables_with_active_perp():
    # Active perp + separate-margin wallet → the $25K USDC reserve is COUNTED.
    ns, st = _estimate_spot_split(
        _dreamcash_bag(), perp_account_value=11_881.03,
        prices={"HYPE": 65.0}, wallet_addr=DREAMCASH,
    )
    assert st == 25_009.07           # reserve preserved
    assert ns == 65.0 * 0.00964914   # HYPE valued at live price


def test_non_separate_wallet_still_skips_stables_with_active_perp():
    # Same bag, but NOT a separate-margin wallet → unified-account skip stands.
    ns, st = _estimate_spot_split(
        _dreamcash_bag(), perp_account_value=11_881.03,
        prices={"HYPE": 65.0}, wallet_addr="0xdeadbeef",
    )
    assert st == 0.0                 # skipped (regression guard)
    assert ns == 65.0 * 0.00964914


def test_no_wallet_addr_preserves_legacy_behaviour():
    # Legacy callers pass no wallet_addr → identical to pre-fix (skip on perp).
    _, st_active = _estimate_spot_split(_dreamcash_bag(), perp_account_value=5_000.0)
    assert st_active == 0.0
    # Idle → stables fold into the cash bucket as before.
    _, st_idle = _estimate_spot_split(_dreamcash_bag(), perp_account_value=0.0)
    assert st_idle == 25_009.07
