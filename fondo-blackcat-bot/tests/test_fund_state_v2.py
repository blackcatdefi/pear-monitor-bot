"""R-FINAL — Bug #1 tests.

Covers ``auto.fund_state_v2.detect_active_baskets`` and
``render_state_block`` for the three canonical scenarios:

1. v6 basket alive on registered wallet → STATUS=ACTIVE, basket_id=v6,
   no anomalies.
2. Wallet with no positions → STATUS=IDLE, no anomalies.
3. Unregistered wallet holding basket positions → ANOMALY emitted.

Plus the kill-switch and the fetch-failure paths.
"""
from __future__ import annotations

import asyncio
import os

import pytest

# Ensure tests run with autodetect ON regardless of host env.
os.environ["FUND_STATE_AUTODETECT"] = "true"
os.environ["FUND_STATE_ANOMALY_USD"] = "500"

from auto import fund_state_v2  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
REGISTERED_WALLET = "0xc7ae0000000000000000000000000000000000ae"
HL_WALLET = "0xa44e0000000000000000000000000000000000ae"
UNKNOWN_WALLET = "0xdeadbeef0000000000000000000000000000dead"


def _v6_short(coin: str, ntl: float, entry_px: float) -> dict:
    return {
        "coin": coin,
        "szi": -100.0,
        "position_value": ntl,
        "entryPx": entry_px,
    }


def _wallets_v6_active() -> list[dict]:
    return [
        {
            "status": "ok",
            "data": {
                "wallet": REGISTERED_WALLET,
                "label": "Cross 5x",
                "positions": [
                    _v6_short("DYDX", 4500.0, 0.65),
                    _v6_short("OP", 4500.0, 1.20),
                    _v6_short("ARB", 4500.0, 0.45),
                    _v6_short("PYTH", 4500.0, 0.18),
                    _v6_short("ENA", 4500.0, 0.40),
                ],
            },
        },
        {
            "status": "ok",
            "data": {
                "wallet": HL_WALLET,
                "label": "HyperLend Principal",
                "positions": [],
            },
        },
    ]


def _wallets_idle() -> list[dict]:
    return [
        {
            "status": "ok",
            "data": {
                "wallet": REGISTERED_WALLET,
                "label": "Cross 5x",
                "positions": [],
            },
        },
    ]


def _wallets_unregistered_with_basket() -> list[dict]:
    return [
        {
            "status": "ok",
            "data": {
                "wallet": UNKNOWN_WALLET,
                "label": None,
                "positions": [
                    _v6_short("DYDX", 5000.0, 0.65),
                    _v6_short("OP", 5000.0, 1.20),
                    _v6_short("ARB", 5000.0, 0.45),
                ],
            },
        },
    ]


_V6_TOKENS = {"DYDX", "OP", "ARB", "PYTH", "ENA"}
_V45_TOKENS = {"WLD", "STRK", "ZRO", "AVAX", "ENA"}


def _patch_registered(monkeypatch, registered_addrs):
    monkeypatch.setattr(
        fund_state_v2,
        "_registered_wallets",
        lambda: {a.lower(): "test" for a in registered_addrs},
    )
    # Also pin the basket-perp universe so tests don't depend on whatever
    # fund_state.BASKET_PERP_TOKENS happens to ship in the repo right now.
    monkeypatch.setattr(
        fund_state_v2,
        "_basket_perp_tokens",
        lambda: _V6_TOKENS | _V45_TOKENS,
    )


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------
def test_v6_basket_active_no_anomaly(monkeypatch):
    _patch_registered(monkeypatch, [REGISTERED_WALLET, HL_WALLET])

    async def _fetch():
        return _wallets_v6_active()

    result = asyncio.run(fund_state_v2.detect_active_baskets(_fetch))

    assert result["summary"]["any_active"] is True
    assert result["summary"]["anomalies"] == []
    assert result["summary"]["total_basket_notional_usd"] == pytest.approx(22500.0)

    w = result["wallets"][REGISTERED_WALLET]
    assert w["status"] == "ACTIVE"
    assert w["basket_id_inferido"] == "v6"
    assert {s["coin"] for s in w["shorts"]} == {"DYDX", "OP", "ARB", "PYTH", "ENA"}
    assert w["is_registered"] is True

    hl = result["wallets"][HL_WALLET]
    assert hl["status"] == "IDLE"
    assert hl["shorts"] == []


def test_idle_wallet_no_anomaly(monkeypatch):
    _patch_registered(monkeypatch, [REGISTERED_WALLET])

    async def _fetch():
        return _wallets_idle()

    result = asyncio.run(fund_state_v2.detect_active_baskets(_fetch))

    assert result["summary"]["any_active"] is False
    assert result["summary"]["anomalies"] == []
    assert result["wallets"][REGISTERED_WALLET]["status"] == "IDLE"


def test_unregistered_wallet_triggers_anomaly(monkeypatch):
    # KNOWN_WALLETS is empty → the unknown wallet is unregistered.
    _patch_registered(monkeypatch, [])

    async def _fetch():
        return _wallets_unregistered_with_basket()

    result = asyncio.run(fund_state_v2.detect_active_baskets(_fetch))

    anomalies = result["summary"]["anomalies"]
    assert len(anomalies) == 1
    assert anomalies[0]["wallet"] == UNKNOWN_WALLET
    assert anomalies[0]["reason"] == "UNREGISTERED_WALLET_HOLDS_BASKET"
    assert anomalies[0]["notional_usd"] >= 500.0


def test_dust_short_below_anomaly_threshold(monkeypatch):
    """A $50 short on an unregistered wallet must NOT trigger anomaly."""
    _patch_registered(monkeypatch, [])

    async def _fetch():
        return [
            {
                "status": "ok",
                "data": {
                    "wallet": UNKNOWN_WALLET,
                    "positions": [_v6_short("DYDX", 50.0, 0.65)],
                },
            }
        ]

    result = asyncio.run(fund_state_v2.detect_active_baskets(_fetch))
    # The wallet is ACTIVE (it has a basket short) but no anomaly because
    # notional <$500 dust threshold.
    assert result["summary"]["anomalies"] == []


def test_kill_switch_disabled(monkeypatch):
    monkeypatch.setattr(fund_state_v2, "ENABLED", False)

    async def _fetch():
        # Even if real positions exist, kill switch should short-circuit.
        return _wallets_v6_active()

    result = asyncio.run(fund_state_v2.detect_active_baskets(_fetch))
    assert result["wallets"] == {}
    assert result["summary"]["disabled"] is True


def test_fetch_failure_returns_safe_payload(monkeypatch):
    _patch_registered(monkeypatch, [REGISTERED_WALLET])

    async def _broken():
        raise RuntimeError("RPC down")

    result = asyncio.run(fund_state_v2.detect_active_baskets(_broken))
    assert result["summary"]["any_active"] is False
    assert result["summary"].get("fetch_error") is True


def test_render_state_block_active(monkeypatch):
    _patch_registered(monkeypatch, [REGISTERED_WALLET, HL_WALLET])

    async def _fetch():
        return _wallets_v6_active()

    detected = asyncio.run(fund_state_v2.detect_active_baskets(_fetch))
    block = fund_state_v2.render_state_block(detected)

    assert "ON-CHAIN AUTORITATIVO" in block
    assert "Basket activa: SÍ" in block
    assert "v6" in block
    # Must NOT contain the false-positive "ANOMALÍA" block.
    assert "ANOMALÍAS" not in block
    assert "Sin anomalías" in block


def test_render_state_block_idle(monkeypatch):
    _patch_registered(monkeypatch, [REGISTERED_WALLET])

    async def _fetch():
        return _wallets_idle()

    detected = asyncio.run(fund_state_v2.detect_active_baskets(_fetch))
    block = fund_state_v2.render_state_block(detected)

    assert "Basket activa: NO" in block
    assert "ANOMALÍAS" not in block


def test_infer_basket_id_v6():
    assert (
        fund_state_v2._infer_basket_id({"DYDX", "OP", "ARB", "PYTH", "ENA"}) == "v6"
    )


def test_infer_basket_id_v45():
    assert (
        fund_state_v2._infer_basket_id({"WLD", "STRK", "ZRO", "AVAX", "ENA"})
        == "v4/v5"
    )


def test_infer_basket_id_unknown():
    assert fund_state_v2._infer_basket_id({"BTC", "ETH"}) is None


def test_long_positions_visible_with_side_long(monkeypatch):
    """R-DASH-FIX (1 may 2026): the detector is no longer SHORT-only.

    Pre-fix: szi >= 0 was hard-skipped → LONG hedge wallets always IDLE.
    Post-fix: side derives from sign of szi → LONG positions appear in
    ``positions`` with side="LONG", legacy ``shorts`` view stays empty,
    basket_id_inferido is None (not classifiable as SHORT basket)."""
    _patch_registered(monkeypatch, [REGISTERED_WALLET])

    async def _fetch():
        return [
            {
                "status": "ok",
                "data": {
                    "wallet": REGISTERED_WALLET,
                    "positions": [
                        {
                            "coin": "DYDX",
                            "szi": 100.0,  # long
                            "position_value": 4500.0,
                            "entryPx": 0.65,
                        }
                    ],
                },
            }
        ]

    result = asyncio.run(fund_state_v2.detect_active_baskets(_fetch))
    w = result["wallets"][REGISTERED_WALLET]
    assert w["status"] == "ACTIVE"  # any non-dust position → ACTIVE
    assert len(w["positions"]) == 1
    assert w["positions"][0]["side"] == "LONG"
    assert w["shorts"] == []  # legacy view stays SHORT-only filtered
    assert w["basket_id_inferido"] is None  # not a SHORT basket


# ---------------------------------------------------------------------------
# R-DASH regression — production naming convention (size/notional_usd)
# ---------------------------------------------------------------------------
def _v6_short_prod(coin: str, ntl: float, entry_px: float) -> dict:
    """Production-shape position dict — matches what
    ``modules.portfolio._summarize_positions`` emits.

    R-DASH bug: the previous fund_state_v2 only read the raw HL info-API
    shape (``szi`` / ``position_value`` / ``entryPx``), so when the data
    flowed through portfolio.py it was always classified as dust.
    """
    return {
        "coin": coin,
        "size": -100.0,
        "side": "SHORT",
        "notional_usd": ntl,
        "entry_px": entry_px,
    }


def test_v6_basket_visible_with_production_field_names(monkeypatch):
    """REGRESSION: the production data flow uses size/notional_usd/entry_px.

    Before the R-DASH fix, every basket position would show notional=0
    after the field-name lookup miss → dropped by the dust filter →
    wallet IDLE → dashboard "Sin posiciones abiertas" despite live v6.
    """
    _patch_registered(monkeypatch, [REGISTERED_WALLET, HL_WALLET])

    async def _fetch():
        return [
            {
                "status": "ok",
                "data": {
                    "wallet": REGISTERED_WALLET,
                    "label": "Cross 5x",
                    "positions": [
                        _v6_short_prod("DYDX", 4500.0, 0.65),
                        _v6_short_prod("OP", 4500.0, 1.20),
                        _v6_short_prod("ARB", 4500.0, 0.45),
                        _v6_short_prod("PYTH", 4500.0, 0.18),
                        _v6_short_prod("ENA", 4500.0, 0.40),
                    ],
                },
            }
        ]

    result = asyncio.run(fund_state_v2.detect_active_baskets(_fetch))

    assert result["summary"]["any_active"] is True, (
        "Production-shape data must trigger ACTIVE — this is the exact "
        "regression that caused dashboard 'Sin posiciones abiertas' on "
        "1 may 2026 13:31 UTC."
    )
    assert result["summary"]["total_basket_notional_usd"] == pytest.approx(22500.0)

    w = result["wallets"][REGISTERED_WALLET]
    assert w["status"] == "ACTIVE"
    assert w["basket_id_inferido"] == "v6"
    assert {s["coin"] for s in w["shorts"]} == {
        "DYDX",
        "OP",
        "ARB",
        "PYTH",
        "ENA",
    }


def test_v6_basket_visible_with_mixed_field_names(monkeypatch):
    """Defensive: a wallet with positions emitted by the raw HL fetcher
    AND another emitted by portfolio.py must both be detected."""
    _patch_registered(monkeypatch, [REGISTERED_WALLET])

    async def _fetch():
        return [
            {
                "status": "ok",
                "data": {
                    "wallet": REGISTERED_WALLET,
                    "positions": [
                        # Raw HL shape (legacy)
                        {
                            "coin": "DYDX",
                            "szi": -100.0,
                            "position_value": 4500.0,
                            "entryPx": 0.65,
                        },
                        # Production-normalised shape
                        _v6_short_prod("OP", 4500.0, 1.20),
                    ],
                },
            }
        ]

    result = asyncio.run(fund_state_v2.detect_active_baskets(_fetch))
    w = result["wallets"][REGISTERED_WALLET]
    assert w["status"] == "ACTIVE"
    coins = {s["coin"] for s in w["shorts"]}
    assert coins == {"DYDX", "OP"}


# ---------------------------------------------------------------------------
# R-DASH-FIX (1 may 2026) — basket-agnostic regression
# ---------------------------------------------------------------------------
def _patch_registered_no_whitelist(monkeypatch, registered_addrs):
    """Like _patch_registered but does NOT touch ``_basket_perp_tokens``.

    The whole point of R-DASH-FIX is that the detector no longer uses a
    token whitelist — these tests must pass without patching it. If they
    relied on the patched whitelist, regression to a whitelist-based
    detector would silently re-pass.
    """
    monkeypatch.setattr(
        fund_state_v2,
        "_registered_wallets",
        lambda: {a.lower(): "test" for a in registered_addrs},
    )


def test_rdashfix_5_positions_mix_field_names_all_visible(monkeypatch):
    """REGRESSION 1 may 2026 14:17 UTC.

    Pear shows 5 open positions on 0xc7AE…1505 (DYDX/OP/ARB/PYTH/ENA
    SHORT, total ntl ~$22,645, total UPnL +$78.84). Bot reported 1 of 5
    because fund_state.BASKET_PERP_TOKENS lacks DYDX/OP/ARB/PYTH (v6
    deployed 29 abr 2026 was never added to the whitelist).

    This test reproduces the exact observed payload — mix of raw HL and
    portfolio-normalised field names, the same 5 coins, similar notionals
    — and asserts ALL 5 positions appear. NO whitelist patching: the
    detector must not depend on BASKET_PERP_TOKENS for inclusion.
    """
    _patch_registered_no_whitelist(monkeypatch, [REGISTERED_WALLET])

    async def _fetch():
        return [
            {
                "status": "ok",
                "data": {
                    "wallet": REGISTERED_WALLET,
                    "label": "Cross 5x basket v6",
                    "positions": [
                        # Raw HL shape — szi/positionValue/entryPx/unrealizedPnl
                        {
                            "coin": "DYDX",
                            "szi": -28503.0,
                            "positionValue": 4460.0,
                            "entryPx": 0.157,
                            "unrealizedPnl": 52.53,
                        },
                        # Portfolio-normalised — size/notional_usd/entry_px/upnl
                        {
                            "coin": "OP",
                            "size": -37430.0,
                            "side": "SHORT",
                            "notional_usd": 4534.0,
                            "entry_px": 0.120,
                            "upnl": -20.29,
                        },
                        # Raw HL again
                        {
                            "coin": "ARB",
                            "szi": -35736.0,
                            "positionValue": 4485.0,
                            "entryPx": 0.126,
                            "unrealizedPnl": 29.23,
                        },
                        # Portfolio-normalised
                        {
                            "coin": "PYTH",
                            "size": -96712.0,
                            "side": "SHORT",
                            "notional_usd": 4510.0,
                            "entry_px": 0.046,
                            "upnl": 3.31,
                        },
                        # Mixed alias for upnl (unrealized_pnl)
                        {
                            "coin": "ENA",
                            "size": -43682.0,
                            "side": "SHORT",
                            "notional_usd": 4477.0,
                            "entry_px": 0.103,
                            "unrealized_pnl": 36.18,
                        },
                    ],
                },
            }
        ]

    result = asyncio.run(fund_state_v2.detect_active_baskets(_fetch))

    assert result["summary"]["any_active"] is True
    w = result["wallets"][REGISTERED_WALLET]
    assert w["status"] == "ACTIVE"

    # All 5 must be visible — NOT 1 (regression target)
    assert len(w["positions"]) == 5, (
        f"Expected 5 positions (regression 1 may 14:17), got "
        f"{len(w['positions'])} — whitelist filter likely re-introduced"
    )
    coins = {p["coin"] for p in w["positions"]}
    assert coins == {"DYDX", "OP", "ARB", "PYTH", "ENA"}

    # Every position must be SHORT (szi negative or explicit side)
    assert all(p["side"] == "SHORT" for p in w["positions"])

    # Notional total matches Pear screenshot ($22,645 → sum of 4460+4534+4485+4510+4477 = $22,466 ≈)
    total = w["basket_notional_usd"]
    assert 22000 < total < 23000, f"basket_notional_usd={total} outside Pear range"

    # UPnL aggregates from inline upnl field (no snapshot lookup needed)
    upnls = {p["coin"]: p["upnl"] for p in w["positions"]}
    assert upnls["DYDX"] == pytest.approx(52.53)
    assert upnls["OP"] == pytest.approx(-20.29)
    assert upnls["ARB"] == pytest.approx(29.23)
    assert upnls["PYTH"] == pytest.approx(3.31)
    assert upnls["ENA"] == pytest.approx(36.18)
    total_upnl = sum(upnls.values())
    assert 70 < total_upnl < 110, f"total UPnL={total_upnl} outside Pear range +$78.84"

    # Legacy `shorts` view must also be populated (backward compat)
    assert len(w["shorts"]) == 5
    assert {s["coin"] for s in w["shorts"]} == coins


def test_rdashfix_no_token_whitelist_unknown_token_visible(monkeypatch):
    """A token never seen by the bot before MUST still be detected.

    Pre-fix: any new HL listing the fund shorted (XRPMOON, KAITO, etc.)
    would silently disappear from the dashboard until BASKET_PERP_TOKENS
    was manually edited. Post-fix: the detector is content-neutral,
    surfaces whatever HL returns.
    """
    _patch_registered_no_whitelist(monkeypatch, [REGISTERED_WALLET])

    async def _fetch():
        return [
            {
                "status": "ok",
                "data": {
                    "wallet": REGISTERED_WALLET,
                    "positions": [
                        {
                            "coin": "XRPMOON",  # fictional token, not in any list
                            "size": -1000.0,
                            "side": "SHORT",
                            "notional_usd": 1500.0,
                            "entry_px": 1.5,
                            "upnl": 12.0,
                        }
                    ],
                },
            }
        ]

    result = asyncio.run(fund_state_v2.detect_active_baskets(_fetch))
    w = result["wallets"][REGISTERED_WALLET]
    assert w["status"] == "ACTIVE"
    assert len(w["positions"]) == 1
    assert w["positions"][0]["coin"] == "XRPMOON"


def test_rdashfix_dust_filter_50usd(monkeypatch):
    """Position with notional below $50 is dropped as dust.

    Edge: a $30 phantom position should not turn the wallet ACTIVE.
    """
    _patch_registered_no_whitelist(monkeypatch, [REGISTERED_WALLET])

    async def _fetch():
        return [
            {
                "status": "ok",
                "data": {
                    "wallet": REGISTERED_WALLET,
                    "positions": [
                        {
                            "coin": "ZRO",
                            "size": -10.0,
                            "side": "SHORT",
                            "notional_usd": 30.0,  # dust
                            "entry_px": 3.0,
                            "upnl": 0.1,
                        }
                    ],
                },
            }
        ]

    result = asyncio.run(fund_state_v2.detect_active_baskets(_fetch))
    w = result["wallets"][REGISTERED_WALLET]
    assert w["status"] == "IDLE", "dust ($30 < $50) must not turn wallet ACTIVE"
    assert w["positions"] == []


def test_rdashfix_dust_filter_just_above_threshold(monkeypatch):
    """Position with notional ≥ $50 is kept."""
    _patch_registered_no_whitelist(monkeypatch, [REGISTERED_WALLET])

    async def _fetch():
        return [
            {
                "status": "ok",
                "data": {
                    "wallet": REGISTERED_WALLET,
                    "positions": [
                        {
                            "coin": "ZRO",
                            "size": -10.0,
                            "side": "SHORT",
                            "notional_usd": 75.0,  # $75 > $50 = kept
                            "entry_px": 3.0,
                            "upnl": 0.1,
                        }
                    ],
                },
            }
        ]

    result = asyncio.run(fund_state_v2.detect_active_baskets(_fetch))
    w = result["wallets"][REGISTERED_WALLET]
    assert w["status"] == "ACTIVE"
    assert len(w["positions"]) == 1


def test_rdashfix_long_position_appears_with_correct_side(monkeypatch):
    """Side detection works for LONG positions too.

    Pre-fix: the detector hard-skipped szi >= 0, so a LONG hedge wallet
    would always be IDLE. Post-fix: side derives from sign of szi (or
    explicit `side` field if present).
    """
    _patch_registered_no_whitelist(monkeypatch, [REGISTERED_WALLET])

    async def _fetch():
        return [
            {
                "status": "ok",
                "data": {
                    "wallet": REGISTERED_WALLET,
                    "positions": [
                        {
                            "coin": "BTC",
                            "size": 0.5,  # positive → LONG
                            "notional_usd": 30000.0,
                            "entry_px": 60000.0,
                            "upnl": 100.0,
                        }
                    ],
                },
            }
        ]

    result = asyncio.run(fund_state_v2.detect_active_baskets(_fetch))
    w = result["wallets"][REGISTERED_WALLET]
    assert w["status"] == "ACTIVE"
    assert len(w["positions"]) == 1
    assert w["positions"][0]["side"] == "LONG"
    # Legacy shorts view should be empty (it's a LONG)
    assert w["shorts"] == []
    assert w["basket_id_inferido"] is None  # not classifiable as a SHORT basket


def test_rdashfix_basket_notional_does_not_doublecount(monkeypatch):
    """basket_notional_usd should sum each position's ntl exactly once."""
    _patch_registered_no_whitelist(monkeypatch, [REGISTERED_WALLET])

    async def _fetch():
        return [
            {
                "status": "ok",
                "data": {
                    "wallet": REGISTERED_WALLET,
                    "positions": [
                        {"coin": "DYDX", "size": -100, "notional_usd": 4500.0,
                         "side": "SHORT", "entry_px": 0.65, "upnl": 10.0},
                        {"coin": "OP", "size": -200, "notional_usd": 4500.0,
                         "side": "SHORT", "entry_px": 1.20, "upnl": 5.0},
                    ],
                },
            }
        ]

    result = asyncio.run(fund_state_v2.detect_active_baskets(_fetch))
    w = result["wallets"][REGISTERED_WALLET]
    assert w["basket_notional_usd"] == pytest.approx(9000.0)
    # summary total also matches
    assert result["summary"]["total_basket_notional_usd"] == pytest.approx(9000.0)
