"""R-EQUITY-DEDUP-DREAMCASH (2026-07-07) — pin the SINGLE unified-equity rule.

DreamCash (0x171b…, RESCATE/HEDGE wallet) is a HyperLiquid UNIFIED account:
``marginSummary.accountValue`` is ALREADY the full perp-side equity — locked
USDC margin + free margin + unrealised PnL. The one correct model for EVERY
wallet is therefore::

    wallet_equity = perp accountValue + non-USDC spot

with spot STABLES skipped while a perp is active (they ARE the margin pool
captured inside accountValue), and UPnL informational only — NEVER an
additive line.

Both previous DreamCash special-cases were wrong and overcounted:
* R-DREAMCASH-EQUITY/461f023 — spot reserve + accountValue (~$47K bug).
* R-DREAMCASH-UNIFIED/106c84a — spot reserve + UPnL-only, which still
  counted the locked-margin USDC twice via the kept reserve: DreamCash
  printed Capital Total $40.7K vs the real ~$26.7K, and bot TOTAL EQUITY
  $158.8K vs Rabby ground truth $144.66K (verified 2026-07-06).

``_is_unified_spot_reserve_wallet`` survives as a RENDER-ONLY predicate
(liq-px position lines + "Unified pool" label). These tests must stay green
so neither overcount can ever return.
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


# ── spot split: stables skipped for EVERY wallet while a perp is active ──────
def test_dreamcash_skips_stables_with_active_perp():
    # THE regression that killed the $14.1K overcount: with an active perp the
    # $24,995 USDC IS the margin pool inside accountValue — it must NOT be
    # counted again as a spot reserve, DreamCash included.
    ns, st = _estimate_spot_split(
        _dreamcash_bag(), perp_account_value=26_400.0,
        prices={"HYPE": 65.0}, wallet_addr=DREAMCASH,
    )
    assert st == 0.0                 # no reserve on top of accountValue
    assert ns == 65.0 * 0.00964914   # HYPE valued at live price


def test_non_reserve_wallet_still_skips_stables_with_active_perp():
    ns, st = _estimate_spot_split(
        _dreamcash_bag(), perp_account_value=26_400.0,
        prices={"HYPE": 65.0}, wallet_addr="0xdeadbeef",
    )
    assert st == 0.0
    assert ns == 65.0 * 0.00964914


def test_idle_perp_keeps_stables_for_any_wallet():
    # No active perp → the USDC is plain idle cash, counted once.
    _, st_dc = _estimate_spot_split(
        _dreamcash_bag(), perp_account_value=0.0, wallet_addr=DREAMCASH,
    )
    assert st_dc == 24_995.00
    _, st_other = _estimate_spot_split(
        _dreamcash_bag(), perp_account_value=0.0, wallet_addr="0xdeadbeef",
    )
    assert st_other == 24_995.00


def test_no_wallet_addr_preserves_legacy_behaviour():
    _, st_active = _estimate_spot_split(_dreamcash_bag(), perp_account_value=5_000.0)
    assert st_active == 0.0
    _, st_idle = _estimate_spot_split(_dreamcash_bag(), perp_account_value=0.0)
    assert st_idle == 24_995.00


# ── perp contribution: accountValue for EVERY wallet, DreamCash included ─────
def test_dreamcash_perp_contributes_full_accountvalue():
    d = {
        "wallet": DREAMCASH,
        "account_value": 26_400.0,
        "unrealized_pnl_total": 1_700.0,
    }
    assert _wallet_perp_contribution(d) == 26_400.0


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
                                      "account_value": "x"}) == 0.0
    assert _wallet_perp_contribution({"wallet": "0xabc",
                                      "account_value": None}) == 0.0


# ── acceptance regression: unified equity NEVER adds UPnL twice ──────────────
def test_unified_equity_never_adds_upnl_twice():
    """Acceptance fixture: wallet with margin_used, withdrawable and UPnL →
    equity must be EXACTLY accountValue + non-USDC spot. Adding the UPnL or
    the stable reserve on top reproduces the $158.8K-vs-$144.66K bug."""
    d = {
        "wallet": DREAMCASH,
        "account_value": 26_713.0,       # = margin_used + withdrawable + UPnL
        "total_margin_used": 8_000.0,
        "withdrawable": 2_000.0,
        "unrealized_pnl_total": 1_713.0,
    }
    ns, st = _estimate_spot_split(
        _dreamcash_bag(), perp_account_value=d["account_value"],
        prices={"HYPE": 65.0}, wallet_addr=DREAMCASH,
    )
    equity = _wallet_perp_contribution(d) + ns + st
    expected = 26_713.0 + 65.0 * 0.00964914    # accountValue + non-USDC spot
    assert abs(equity - expected) < 1e-6
    # The two historical broken models, documented so they can never return:
    broken_reserve_plus_av = d["account_value"] + 24_995.0 + ns   # ~$51.7K
    broken_reserve_plus_upnl = d["unrealized_pnl_total"] + 24_995.0 + ns
    assert equity < broken_reserve_plus_av
    assert abs(equity - broken_reserve_plus_upnl) > 1.0
    # UPnL is informational — never an additive term.
    assert abs(
        equity - (d["account_value"] + d["unrealized_pnl_total"] + ns)
    ) > 1.0


def test_dreamcash_wallet_equity_matches_rabby_ground_truth_scale():
    # 2026-07-06 ground truth: HL DreamCash = $26,713 (Rabby). The bot printed
    # $40.7K (reserve $24.0K + UPnL $16.7K). Under the fixed model the wallet
    # contributes exactly its accountValue + dust HYPE.
    d = {"wallet": DREAMCASH, "account_value": 26_713.0,
         "unrealized_pnl_total": 16_700.0}
    ns, st = _estimate_spot_split(
        _dreamcash_bag(), perp_account_value=d["account_value"],
        prices={"HYPE": 65.0}, wallet_addr=DREAMCASH,
    )
    capital = _wallet_perp_contribution(d) + ns + st
    assert abs(capital - (26_713.0 + 65.0 * 0.00964914)) < 1e-6
    assert capital < 27_000.0        # emphatically NOT ~$40.7K
