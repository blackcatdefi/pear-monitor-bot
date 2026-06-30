"""R-PMBORROW-DEDUP (2026-06-30) — pin the idle-perp borrow-dedup in the
formatter's ``_estimate_spot_split``.

The bug
-------
The fund's PM core wallet 0xc7ae holds spot HYPE as cross collateral with USDC
borrowed against it. The borrow arrives in ``spotClearinghouseState`` as a
stablecoin row with a NEGATIVE ``total`` (and a positive ``borrowed`` field):

    {"coin": "USDC", "total": "-113139.62", "borrowed": "113139.99"}

The headline equity formula nets that borrow ONCE via ``spot_borrow_total``
(``_spot_native_borrow``). But ``templates.formatters._estimate_spot_split``
only skipped stables when the perp was ACTIVE — it relied on that skip to keep
the negative USDC out of the cash-equivalent ``stables`` bucket. When 0xc7ae's
perp is IDLE (``accountValue == 0``) the skip never fired, so the −$113K USDC
leaked into ``stables`` AND was subtracted again as ``spot_borrow`` → the borrow
was counted twice and TOTAL EQUITY crashed to ~$15.6K vs Rabby's ~$128.7K.

(The DreamCash separate-margin reserve fix merely UNMASKED this: by adding
+$25K it pushed the raw total positive, bypassing the ``parity_stale`` clamp
that had been hiding the negative.)

The fix mirrors the long-standing guard in
``portfolio_snapshot._spot_split_value``: a borrowed stable (negative total or a
positive ``borrowed`` field) is a LIABILITY, never cash — excluded from
``stables`` regardless of perp activity. These tests must stay green so the
double-count can never return.
"""
from __future__ import annotations

from templates.formatters import _estimate_spot_split

C7AE = "0xc7ae23316b47f7e75f455f53ad37873a18351505"


def _pm_core_bag() -> list[dict]:
    # 0xc7ae as it arrives live: HYPE collateral + the PM USDC borrow row.
    return [
        {"coin": "HYPE", "total": "3007.0", "entry_ntl": "0", "borrowed": "0"},
        {"coin": "USDC", "total": "-113139.62", "borrowed": "113139.99"},
        {"coin": "USDT0", "total": "0.37", "entry_ntl": "0.37"},
    ]


def test_idle_perp_borrow_not_counted_as_cash():
    # Perp IDLE (accountValue 0) — the negative USDC borrow must NOT enter the
    # stables bucket (it is netted once via spot_borrow elsewhere).
    ns, st = _estimate_spot_split(
        _pm_core_bag(), perp_account_value=0.0,
        prices={"HYPE": 64.88}, wallet_addr=C7AE,
    )
    assert st >= 0.0                       # never negative
    assert st < 1.0                        # only the $0.37 USDT0 dust
    assert abs(ns - 3007.0 * 64.88) < 1.0  # HYPE valued gross at live price


def test_borrowed_field_excluded_even_without_negative_total():
    # A stable row flagged ``borrowed`` is a liability even if total is 0/None.
    bag = [{"coin": "USDC", "total": "0", "borrowed": "50000"}]
    _ns, st = _estimate_spot_split(bag, perp_account_value=0.0)
    assert st == 0.0


def test_positive_idle_stables_still_counted():
    # Regression guard: a genuine positive stable (no borrow) with an idle perp
    # must still fold into the cash bucket 1:1 — the dedup must not over-skip.
    bag = [{"coin": "USDC", "total": "1234.56"}]
    _ns, st = _estimate_spot_split(bag, perp_account_value=0.0)
    assert abs(st - 1234.56) < 1e-6


def test_active_perp_unified_skip_unchanged():
    # Default unified-margin wallet with an ACTIVE perp still skips its stables.
    bag = [{"coin": "USDC", "total": "1000.0"}]
    _ns, st = _estimate_spot_split(bag, perp_account_value=5000.0)
    assert st == 0.0
