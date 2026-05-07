"""R-BOT-TERMINOLOGY-UNIFY (2026-05-07) — Bug #1.

The wallet 0xc7AE…1505 used to render as "💰 SECUNDARIA 0xc7ae…1505"
because templates/formatters.py applied a hardcoded RANK_LABELS list
("PRINCIPAL"/"SECUNDARIA") that overrode the env-var label. The fix is
to route every wallet label through ``auto.wallet_labels.apply_wallet_label``
so the canonical address→label map is the single source of truth.

These tests pin the contract:
1. The canonical map exposes "BlackCatDeFi EVM (Trading)" for 0xc7ae…1505.
2. ``format_quick_positions`` renders that exact label (with "💼" prefix).
3. Other canonical wallets resolve correctly (Main Flywheel, DreamCash).
4. The legacy "SECUNDARIA" / "PRINCIPAL" string never appears in the
   formatter output.
"""
from __future__ import annotations

from auto.wallet_labels import CANONICAL_WALLET_LABELS, apply_wallet_label
from templates.formatters import format_quick_positions


def _wallet_payload(addr: str, label: str = "?") -> dict:
    return {
        "status": "ok",
        "label": label,
        "wallet": addr,
        "data": {
            "wallet": addr,
            "account_value": 1500.0,
            "label": label,
            "spot_balances": [],
            "positions": [],
            "total_ntl_pos": 0.0,
            "total_margin_used": 0.0,
            "withdrawable": 1500.0,
            "unrealized_pnl_total": 0.0,
        },
    }


def test_canonical_map_has_trading_wallet_label():
    addr = "0xc7ae23316b47f7e75f455f53ad37873a18351505"
    assert CANONICAL_WALLET_LABELS[addr] == "BlackCatDeFi EVM (Trading)"


def test_apply_wallet_label_resolves_trading_wallet():
    addr = "0xc7AE23316b47f7e75f455f53AD37873A18351505"  # mixed case
    out = apply_wallet_label(addr, "ignored-fallback")
    assert out == "BlackCatDeFi EVM (Trading)"


def test_apply_wallet_label_resolves_main_flywheel():
    addr = "0xa44e8b9522a5f710e2b63ab790465af2f155b632"
    assert apply_wallet_label(addr, None) == "Main Flywheel (DDS)"


def test_apply_wallet_label_resolves_dreamcash():
    addr = "0x171b7880939d76abbc6b6b2094f54e6636f829a7"
    assert apply_wallet_label(addr, None) == "DreamCash (WAR TRADE)"


def test_apply_wallet_label_falls_back_when_unknown():
    out = apply_wallet_label("0xdeadbeef", "Custom Label")
    assert out == "Custom Label"


def test_format_quick_positions_uses_canonical_trading_label():
    """Pin the rendered label for the trading wallet."""
    payload = _wallet_payload(
        "0xc7ae23316b47f7e75f455f53ad37873a18351505",
        label="env-var-fallback",
    )
    out = format_quick_positions([payload], [])
    assert "BlackCatDeFi EVM (Trading)" in out
    # Bug #1 anti-regression: the legacy label MUST NOT appear in the body.
    assert "SECUNDARIA" not in out
    # PRINCIPAL was the other half of the legacy hardcoded RANK_LABELS list.
    # Allowed only in section headers if any future feature reuses the word,
    # so we check the actual wallet line specifically.
    for line in out.splitlines():
        if "0xc7ae" in line.lower():
            assert "PRINCIPAL" not in line
            assert "SECUNDARIA" not in line


def test_format_quick_positions_uses_canonical_flywheel_label():
    payload = _wallet_payload(
        "0xa44e8b9522a5f710e2b63ab790465af2f155b632",
        label="env-fallback",
    )
    out = format_quick_positions([payload], [])
    assert "Main Flywheel (DDS)" in out


def test_format_quick_positions_no_legacy_rank_labels_anywhere():
    """No wallet line in /reporte should ever say SECUNDARIA again."""
    wallets = [
        _wallet_payload("0xc7ae23316b47f7e75f455f53ad37873a18351505", "x"),
        _wallet_payload("0xa44e8b9522a5f710e2b63ab790465af2f155b632", "y"),
    ]
    out = format_quick_positions(wallets, [])
    assert "💰 SECUNDARIA" not in out
    assert "💰 PRINCIPAL" not in out
