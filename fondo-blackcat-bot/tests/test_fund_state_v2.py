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


def test_long_positions_ignored(monkeypatch):
    """Only SHORTs feed basket detection — LONG positions are filtered."""
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
    assert result["wallets"][REGISTERED_WALLET]["status"] == "IDLE"
    assert result["wallets"][REGISTERED_WALLET]["shorts"] == []


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
