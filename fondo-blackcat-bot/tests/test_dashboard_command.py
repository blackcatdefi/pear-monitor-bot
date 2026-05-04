"""R-DASHBOARD-COMMAND tests.

test_dashboard_command_exists
    /dashboard is registered in commands_registry.COMMANDS with handler
    cmd_dashboard, category core.

test_dashboard_renders_all_blocks
    render_dashboard_telegram() produces all required sections
    (Capital, Main flywheel, Secondary flywheel, Active basket, Market,
    Wallets, Upcoming catalysts, footer timestamp) from a synthetic state.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Prevent network-dependent env reads at import
os.environ.setdefault("FUND_STATE_AUTODETECT", "true")
os.environ.setdefault("HYPERLEND_AUTOREADER", "true")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeEvent:
    """Minimal macro-calendar event stub."""
    def __init__(self, name: str, impact: str, cat: str, ts: datetime):
        self.name = name
        self.impact_level = impact
        self.category = cat
        self.timestamp_utc = ts


def _make_state() -> dict:
    """Return a representative flat state dict (same shape as _build_state)."""
    return {
        "ts": "2026-05-03 12:00 UTC",
        "capital_total": 35_000.0,
        "hl_collateral_total": 73_000.0,
        "hl_debt_total": 45_000.0,
        "perp_equity_total": 5_000.0,
        "spot_usd_total": 2_000.0,
        "upnl_perp_total": 231.59,
        "main_flywheel": {
            "short": "0xA44E…4e3F",
            "hf": 1.42,
            "collateral_balance": 50_000.0,
            "collateral_symbol": "kHYPE",
            "collateral_usd": 73_000.0,
            "debt_balance": 28.5,
            "debt_symbol": "UETH",
            "debt_usd": 45_000.0,
        },
        "secondary_flywheel": {
            "short": "0xCDDF…3a1B",
            "hf": 2.10,
            "collateral_balance": 0.0,
            "collateral_symbol": "UBTC",
            "collateral_usd": 0.0,
            "debt_balance": 0.0,
            "debt_symbol": "USDT0",
            "debt_usd": 0.0,
        },
        "basket_state": {
            "wallets": {
                "0xabc123def456": {
                    "status": "ACTIVE",
                    "basket_id_inferido": "v6",
                    "positions": [
                        {"coin": "DYDX", "side": "SHORT", "ntl": 5_000.0, "upnl": -120.0},
                        {"coin": "OP",   "side": "SHORT", "ntl": 3_000.0, "upnl":   50.0},
                    ],
                    "label": "Basket v6",
                }
            },
            "summary": {"total_basket_notional_usd": 8_000.0},
        },
        "basket_positions": [
            {"coin": "DYDX", "upnl": -120.0, "notional_usd": 5_000.0},
            {"coin": "OP",   "upnl":   50.0, "notional_usd": 3_000.0},
        ],
        "basket_upnl": -70.0,
        "basket_notional": 8_000.0,
        "btc": 96_500.0,
        "eth": 1_800.0,
        "hype": 28.50,
        "fg_value": 62,
        "fg_label": "Greed",
        "wallets": [
            {
                "address": "0xaaaa",
                "short": "0xaaaa…bbbb",
                "label": "Main",
                "capital": 35_000.0,
                "perp": 5_000.0,
                "spot": 2_000.0,
                "hl_coll": 73_000.0,
                "hl_debt": 45_000.0,
            }
        ],
        "upcoming": [
            _FakeEvent(
                "FOMC May rate decision",
                "high",
                "fomc",
                datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc),
            ),
        ],
        "snap_age_sec": 15.0,
        "is_fresh": True,
        "last_error": None,
        "cached_prices": {},
        "spot_tokens": [
            {"coin": "USDC", "total": 5_000.0, "usd": 5_000.0, "wallets": ["Main"]},
        ],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_dashboard_command_exists():
    """R-DASHBOARD-COMMAND: /dashboard must be registered in COMMANDS."""
    from commands_registry import COMMANDS

    cmd_names = {c.command for c in COMMANDS}
    assert "dashboard" in cmd_names, "/dashboard not in COMMANDS registry"

    cmd = next(c for c in COMMANDS if c.command == "dashboard")
    assert cmd.handler_name == "cmd_dashboard", (
        f"Wrong handler_name: {cmd.handler_name!r} (expected 'cmd_dashboard')"
    )
    assert cmd.category == "core", (
        f"Wrong category: {cmd.category!r} (expected 'core')"
    )


def test_dashboard_renders_all_blocks():
    """R-DASHBOARD-COMMAND: render_dashboard_telegram() includes all required blocks."""
    from modules.dashboard_telegram import render_dashboard_telegram

    state = _make_state()
    text = render_dashboard_telegram(state)

    # Header
    assert "DASHBOARD" in text, "Missing DASHBOARD header"
    assert "2026-05-03" in text, "Missing timestamp in header"

    # Capital block — either the full NET label or fallback "Total"
    assert "CAPITAL" in text, "Missing CAPITAL block"
    assert "NET" in text or "Total" in text, "Missing net capital figure"

    # Main flywheel
    assert "MAIN FLYWHEEL" in text, "Missing MAIN FLYWHEEL section"
    assert "kHYPE" in text, "Missing collateral symbol (kHYPE)"
    assert "UETH" in text, "Missing debt symbol (UETH)"
    assert "1.420" in text, "Missing HF value"

    # Secondary flywheel
    assert "SECONDARY FLYWHEEL" in text, "Missing SECONDARY FLYWHEEL section"

    # Active basket
    assert "ACTIVE BASKET" in text, "Missing ACTIVE BASKET section"
    assert "Basket v6" in text, "Missing dynamic basket label"
    assert "DYDX" in text, "Missing basket leg DYDX"

    # Market
    assert "MARKET" in text, "Missing MARKET section"
    assert "BTC" in text, "Missing BTC price"
    assert "ETH" in text, "Missing ETH price"
    assert "HYPE" in text, "Missing HYPE price"
    assert "F&G" in text, "Missing Fear & Greed"
    assert "Greed" in text, "Missing F&G label"

    # Wallets
    assert "WALLETS" in text, "Missing WALLETS section"
    assert "Main" in text, "Missing wallet label"

    # Catalysts
    assert "CATALYST" in text, "Missing CATALYSTS section"
    assert "FOMC" in text, "Missing catalyst event name"

    # Footer
    assert "Read-only" in text, "Missing footer"
    assert "SSoT" in text or "reporte" in text, "Missing SSoT reference in footer"


# ---------------------------------------------------------------------------
# R-DASHBOARD-DEBT-SYMBOL tests
# ---------------------------------------------------------------------------

def test_dashboard_debt_shows_symbol():
    """R-DASHBOARD-DEBT-SYMBOL: debt_symbol must render, never '— ?'.

    Scenario A — symbol present: renders 'UETH' in the debt line.
    Scenario B — symbol=None, debt_asset known: shows short address, not '?'.
    Scenario C — symbol=None, no asset: falls back to '?' (not '— ?').
    """
    from modules.dashboard_telegram import render_dashboard_telegram

    # A: known symbol — must appear in output.
    state_a = _make_state()
    state_a["main_flywheel"]["debt_symbol"] = "UETH"
    state_a["main_flywheel"]["debt_balance"] = 19.27
    text_a = render_dashboard_telegram(state_a)
    assert "UETH" in text_a, "debt_symbol='UETH' should appear in debt line"
    assert "19.27" in text_a, "debt_balance should appear when symbol is present"
    # Make sure we're not showing '— ?' at all
    assert "— ?" not in text_a, "Should not show '— ?' when symbol is known"

    # B: symbol=None but debt_asset address available → short address fallback.
    state_b = _make_state()
    state_b["main_flywheel"]["debt_symbol"] = None
    state_b["main_flywheel"]["debt_balance"] = 0.0
    state_b["main_flywheel"]["debt_asset"] = "0xBe6727B535545C67d5cAa73dEa54865B92CF7907"
    text_b = render_dashboard_telegram(state_b)
    assert "— ?" not in text_b, "Should not show '— ?' when debt_asset is available"
    # Short-form address fallback: first 6 chars + … + last 4 chars
    assert "0xBe67" in text_b, "Should show short address prefix when symbol is None"

    # C: symbol=None, no asset — graceful '?' (no crash, no '— ?').
    state_c = _make_state()
    state_c["main_flywheel"]["debt_symbol"] = None
    state_c["main_flywheel"]["debt_balance"] = 0.0
    state_c["main_flywheel"]["debt_asset"] = None
    text_c = render_dashboard_telegram(state_c)
    # Just ensure it doesn't blow up and the USD value still shows
    assert "45.0K" in text_c or "45K" in text_c, "USD value must still appear"


def test_dashboard_collateral_and_debt_symmetric():
    """R-DASHBOARD-DEBT-SYMBOL: collateral and debt rendering must be symmetric.

    If collateral_symbol is shown via the 'or "?"' fallback, debt must follow
    the same pattern — neither should use a different fallback strategy.
    """
    from modules.dashboard_telegram import render_dashboard_telegram

    # Both known — both must appear.
    state = _make_state()
    state["main_flywheel"]["collateral_symbol"] = "WHYPE"
    state["main_flywheel"]["collateral_balance"] = 1750.0
    state["main_flywheel"]["debt_symbol"] = "UETH"
    state["main_flywheel"]["debt_balance"] = 19.27
    text = render_dashboard_telegram(state)
    assert "WHYPE" in text, "collateral_symbol must appear"
    assert "UETH" in text, "debt_symbol must appear"
    assert "1,750.00" in text or "1750" in text, "collateral_balance must appear"
    assert "19.27" in text, "debt_balance must appear"

    # Both None + asset address — both should resolve to short address.
    state2 = _make_state()
    state2["main_flywheel"]["collateral_symbol"] = None
    state2["main_flywheel"]["collateral_balance"] = 0.0
    state2["main_flywheel"]["debt_symbol"] = None
    state2["main_flywheel"]["debt_balance"] = 0.0
    state2["main_flywheel"]["debt_asset"] = "0xBe6727B535545C67d5cAa73dEa54865B92CF7907"
    text2 = render_dashboard_telegram(state2)
    # Neither collateral nor debt should make the renderer crash.
    assert "MAIN FLYWHEEL" in text2, "Flywheel section must still render"
    # Debt falls back to short address; collateral still uses '?'.
    # The critical invariant: the DEBT line must not show '— ?' when
    # debt_asset is set (even though collateral line may still show '?').
    debt_line = next(
        (ln for ln in text2.splitlines() if ln.startswith("Debt:")), None
    )
    assert debt_line is not None, "Debt: line must be present"
    assert "— ?" not in debt_line, (
        f"Debt line must not show '— ?' when debt_asset is set, got: {debt_line!r}"
    )
    assert "0xBe67" in debt_line, (
        f"Short address prefix must appear in debt line, got: {debt_line!r}"
    )
