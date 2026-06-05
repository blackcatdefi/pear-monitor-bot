"""P2.11 — wallet roll-up integrity (Rabby-parity, no double-count).

TOTAL EQUITY is built from raw components (perp accountValue + non-stable
spot + stables + pear + vault), NOT from a pre-summed HyperDash/unified total
(which would double-count spot+perp). UPnL is already inside perp
accountValue, so passing it must not inflate NET.
"""
from __future__ import annotations

from auto.capital_calc import compute_net_capital


def test_equity_is_component_sum_not_unified_total():
    snap = {
        "hl_collateral_total": 0.0,
        "hl_debt_total": 0.0,
        "perp_equity_total": 20000.0,
        "spot_usd_total": 70000.0,      # non-stable HYPE collateral
        "spot_stables_total": 5000.0,
        "upnl_perp_total": 0.0,
        "pear_staked_total": 0.0,
        "vault_deposits_total": 3000.0,
        # A HyperDash-style pre-summed total must be IGNORED (never added on
        # top of the components — that is the double-count the audit forbids).
        "hyperdash_unified_total": 999999.0,
    }
    net = compute_net_capital(snap)
    # perp + spot_nonstable = NET (stables excluded from exposure).
    assert abs(net.net_total_usd - (20000.0 + 70000.0)) < 1e-6
    # TOTAL EQUITY (Rabby parity) = net + stables + pear + vault.
    assert abs(net.total_equity_usd - (90000.0 + 5000.0 + 3000.0)) < 1e-6
    # The bogus unified total never leaks into equity.
    assert net.total_equity_usd < 200000.0


def test_upnl_not_double_counted():
    base = {
        "hl_collateral_total": 0.0, "hl_debt_total": 0.0,
        "perp_equity_total": 20000.0, "spot_usd_total": 0.0,
        "spot_stables_total": 0.0, "pear_staked_total": 0.0,
        "vault_deposits_total": 0.0,
    }
    with_upnl = dict(base, upnl_perp_total=5000.0)
    without = dict(base, upnl_perp_total=0.0)
    # UPnL is already inside perp accountValue → passing it must NOT change NET.
    assert compute_net_capital(with_upnl).net_total_usd == compute_net_capital(without).net_total_usd
