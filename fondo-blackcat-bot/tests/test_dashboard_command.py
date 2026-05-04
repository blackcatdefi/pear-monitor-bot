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
