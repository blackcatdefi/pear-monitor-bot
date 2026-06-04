"""R-REPORTE-NOLEGACY-SHORT (2026-06-04) regression.

The legacy "Super Basket Stage 6" path in the reporting layer hardcoded a
SHORT label for ANY active trading-wallet position. When the only open
position was an isolated LONG BTC cycle accumulation (margin=isolated, no
SL/TP, a ladder of DCA limit orders below price), /reporte printed:

    SUPER BASKET STAGE 6 (on-chain autoritativo): Estado: ACTIVA — SHORT ...

while on-chain reality was LONG BTC $27,259 (ACUMULACIÓN CICLO). Root cause:
``auto.fund_state_v2.render_state_block`` looped over EVERY active wallet and
emitted ``SHORT {coins}`` reading the SHORT-only ``shorts`` view — so a
LONG-only wallet printed a bogus SHORT line, and ``system_prompt`` told the
LLM to call any active position "Super Basket Stage 6 — SHORT".

These tests pin that the reporting layer NEVER emits a SHORT /
"Super Basket Stage 6 — SHORT" label for a position that is on-chain LONG,
and that direction is reported as LONG everywhere it appears. Direction must
always come from live on-chain classification, never a hardcoded assumption.
"""
from __future__ import annotations

import asyncio
import os

os.environ["FUND_STATE_AUTODETECT"] = "true"
os.environ.setdefault("POSITION_CLASSIFIER_ENABLED", "true")

from auto import fund_state_v2  # noqa: E402
from modules.position_classifier import (  # noqa: E402
    build_classification_block,
    classify_portfolio,
)

CYCLE_WALLET = "0xc7ae0000000000000000000000000000000000ae"


def _btc_long_cycle_portfolio() -> list[dict]:
    """One wallet, one position: isolated LONG BTC + DCA ladder, no SL/TP.

    Mirrors the real 0xc7AE state that triggered the bug.
    """
    return [
        {
            "status": "ok",
            "data": {
                "wallet": CYCLE_WALLET,
                "label": "Trading",
                "positions": [
                    {
                        "coin": "BTC",
                        "szi": 0.255,
                        "size": 0.255,
                        "side": "LONG",
                        "leverage_type": "isolated",
                        "margin_mode": "isolated",
                        "entryPx": 106900.0,
                        "entry_px": 106900.0,
                        "position_value": 27259.0,
                        "notional_usd": 27259.0,
                        "liquidationPx": 60000.0,
                        "liq_px": 60000.0,
                    }
                ],
                # DCA ladder: resting BUY limits below mark, no SL/TP.
                "open_orders": [
                    {"coin": "BTC", "side": "BUY", "limit_px": 100000.0,
                     "is_trigger": False, "reduce_only": False, "is_sl_tp": False},
                    {"coin": "BTC", "side": "BUY", "limit_px": 92000.0,
                     "is_trigger": False, "reduce_only": False, "is_sl_tp": False},
                    {"coin": "BTC", "side": "BUY", "limit_px": 84000.0,
                     "is_trigger": False, "reduce_only": False, "is_sl_tp": False},
                ],
            },
        }
    ]


def _market() -> dict:
    return {"data": {"BTC": {"price": 106900.0}}}


def _patch_registered(monkeypatch):
    monkeypatch.setattr(
        fund_state_v2,
        "_registered_wallets",
        lambda: {CYCLE_WALLET.lower(): "Trading"},
    )


def _assert_no_btc_short(text: str) -> None:
    """No RENDERED position/label line for BTC may carry a SHORT tag.

    Only inspects bullet/leg lines (the actual rendered positions) and
    status lines — NOT the explanatory legend prose, which legitimately
    names both directions while explaining the rule.
    """
    for line in text.splitlines():
        is_label_line = ("•" in line) or ("Estado:" in line) or ("ACTIVA" in line)
        if is_label_line and "BTC" in line and "SHORT" in line:
            raise AssertionError(f"BTC label line wrongly carries SHORT: {line!r}")


# ── Layer 1: on-chain BASKET STATE block (render_state_block) ──
def test_onchain_block_btc_long_never_labeled_short(monkeypatch):
    _patch_registered(monkeypatch)

    async def _fetch():
        return _btc_long_cycle_portfolio()

    detected = asyncio.run(fund_state_v2.detect_active_baskets(_fetch))
    block = fund_state_v2.render_state_block(detected)

    # No SHORT alt-basket is active (the only leg is LONG).
    assert "Basket activa: NO" in block
    # The LONG must be reported as LONG, with its coin.
    assert "LONG BTC" in block
    # CRITICAL: the block must NEVER label the BTC long as SHORT.
    assert "SHORT BTC" not in block
    assert "ACTIVA — SHORT" not in block
    _assert_no_btc_short(block)


def test_detect_btc_long_active_long_side(monkeypatch):
    _patch_registered(monkeypatch)

    async def _fetch():
        return _btc_long_cycle_portfolio()

    detected = asyncio.run(fund_state_v2.detect_active_baskets(_fetch))
    w = detected["wallets"][CYCLE_WALLET.lower()]
    assert w["status"] == "ACTIVE"          # has a non-dust position
    assert w["shorts"] == []                # no SHORT legs → not a basket
    assert w["basket_id_inferido"] is None  # never inferred as a SHORT basket
    assert len(w["positions"]) == 1
    assert w["positions"][0]["side"] == "LONG"
    assert w["positions"][0]["coin"] == "BTC"


# ── Layer 2: position classifier / CLASIFICACIÓN DE POSICIONES ──
def test_classifier_tags_btc_long_cycle_not_short():
    tags = classify_portfolio(_btc_long_cycle_portfolio(), _market())
    assert len(tags) == 1
    t = tags[0]
    assert t.coin == "BTC"
    assert t.side == "LONG"
    assert t.bucket == "CYCLE_ACCUMULATION"
    assert t.ladder_count >= 1
    assert t.has_sl_tp is False
    assert t.margin_mode == "isolated"


def test_classification_block_btc_long_no_short_label():
    block = build_classification_block(
        classify_portfolio(_btc_long_cycle_portfolio(), _market())
    )
    assert block  # non-empty
    assert "LONG BTC" in block
    assert "ACUMULACIÓN CICLO" in block
    # The CLASIFICACIÓN block must never call this position SHORT or basket.
    assert "SHORT" not in block
    assert "Super Basket Stage 6" not in block


# ── End-to-end: both reporting blocks agree on LONG, zero SHORT for BTC ──
def test_report_blocks_consistent_long_zero_short(monkeypatch):
    _patch_registered(monkeypatch)

    async def _fetch():
        return _btc_long_cycle_portfolio()

    detected = asyncio.run(fund_state_v2.detect_active_baskets(_fetch))
    onchain = fund_state_v2.render_state_block(detected)
    clf = build_classification_block(
        classify_portfolio(_btc_long_cycle_portfolio(), _market())
    )
    combined = onchain + "\n" + clf
    assert "LONG BTC" in combined          # directional truth present
    assert "ACTIVA — SHORT" not in combined
    _assert_no_btc_short(combined)         # no contradictory SHORT anywhere
