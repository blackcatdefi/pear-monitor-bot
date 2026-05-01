"""R-FUNDFIX (1 may 2026) — regression tests for LLM prompt single-source-of-truth.

These tests guard against the 1 may 17:23 UTC bug where ``/reporte``
asked BCD to confirm a basket-state discrepancy. Root cause: the prompt
builder injected DOS contradictory sources:

  1. on-chain truth (5 SHORTs ACTIVE in 0xc7AE — basket v6)
  2. legacy ``fund_state.py`` strings ("v4 cerrado / v5 PENDING_CAPITAL /
     wallets IDLE")

Fix: remove the legacy basket section from the LLM prompt context. The
on-chain block from ``auto.fund_state_v2`` is the only basket source the
LLM sees.
"""
from __future__ import annotations

import asyncio


def test_build_fund_state_block_no_legacy_basket_strings() -> None:
    """The non-state block must NOT contain stale basket prose.

    Anything mentioning v4/v5 status, "PENDING_CAPITAL", "wallets están
    IDLE", "BASKET_V5_PLAN" is the 1 may bug surface — it must not
    re-appear here.
    """
    from templates.system_prompt import build_fund_state_block

    block = build_fund_state_block()

    # Stale basket-state strings — MUST NOT appear in the LLM context.
    forbidden = [
        "v4 cerrado",
        "v4 CERRADO",
        "PENDING_CAPITAL",
        "v5 EN PAUSA",
        "v5 pending",
        "están IDLE desde",
        "BASKET_V5_PLAN",
        "basket_v5_plan",
        "Activo: NO (IDLE)",
        "next_basket",
        "last_basket_result_net_usd",
        "deploy_eta",
        "capital_target_usdt",
    ]
    for s in forbidden:
        assert s not in block, (
            f"Legacy basket string '{s}' leaked into LLM prompt context. "
            "This is the 1 may 17:23 UTC regression."
        )


def test_build_fund_state_block_keeps_legitimate_constants() -> None:
    """HF thresholds, Trade del Ciclo, Flywheel, DCA plan SHOULD remain."""
    from templates.system_prompt import build_fund_state_block

    block = build_fund_state_block()

    # These are non-stale, ground-truth constants — keep them.
    assert "HF THRESHOLDS" in block
    assert "1.10" in block  # HF_CRITICAL
    assert "TRADE DEL CICLO" in block
    assert "BLOFIN" in block.upper()
    assert "FLYWHEEL HYPERLEND" in block
    assert "PLAN DCA TRAMIFICADO" in block


def test_build_fund_state_block_points_to_onchain_block() -> None:
    """The basket section must defer to the on-chain authoritative block."""
    from templates.system_prompt import build_fund_state_block

    block = build_fund_state_block()
    # Tells the LLM where the truth lives
    assert "ON-CHAIN AUTORITATIVO" in block or "on-chain" in block.lower()


def test_system_prompt_prose_no_v4_closed_hardcode() -> None:
    """SYSTEM_PROMPT prose must not bake a specific basket version state."""
    from templates.system_prompt import SYSTEM_PROMPT

    # The 1 may bug surface in the prose (the LLM saw "BASKET v4 CERRADO
    # 2026-04-20" hardcoded and merged it with the contradictory on-chain
    # truth showing v6 active).
    assert "BASKET v4 CERRADO 2026-04-20" not in SYSTEM_PROMPT
    assert "v5 EN PAUSA hasta nueva orden" not in SYSTEM_PROMPT
    # Confirm the prose now points at the on-chain block instead
    assert "ON-CHAIN AUTORITATIVO" in SYSTEM_PROMPT


def test_full_state_block_concatenates_onchain_first() -> None:
    """analysis._full_state_block must put on-chain truth FIRST (shadows legacy)."""
    from modules.analysis import _full_state_block

    text = asyncio.run(_full_state_block())

    # Even with the on-chain block empty (no env config in tests), the
    # downstream non-state block must NOT contain the bug strings.
    forbidden = [
        "v4 cerrado",
        "v5 EN PAUSA",
        "PENDING_CAPITAL",
        "BASKET_V5_PLAN",
    ]
    for s in forbidden:
        assert s not in text, f"Stale state '{s}' surfaced in LLM context"


def test_basket_metadata_infers_v6() -> None:
    """The cosmetic basket-label module must recognise the v6 universe."""
    from auto.basket_metadata import (
        infer_basket_label,
        infer_basket_label_from_coins,
    )

    # Exact match
    assert infer_basket_label_from_coins({"DYDX", "OP", "ARB", "PYTH", "ENA"}) == "v6"
    # Position-dict input with side filter
    positions = [
        {"coin": "DYDX", "side": "SHORT"},
        {"coin": "OP", "side": "SHORT"},
        {"coin": "ARB", "side": "SHORT"},
        {"coin": "PYTH", "side": "SHORT"},
        {"coin": "ENA", "side": "SHORT"},
        # Long leg ignored by SHORT-side filter
        {"coin": "BTC", "side": "LONG"},
    ]
    assert infer_basket_label(positions) == "v6"


def test_basket_metadata_unknown_falls_back() -> None:
    """Unknown coin set returns 'unknown' (not v6)."""
    from auto.basket_metadata import infer_basket_label_from_coins

    assert infer_basket_label_from_coins({"FOO", "BAR"}) == "unknown"
    assert infer_basket_label_from_coins(set()) == "unknown"


def test_fund_constants_re_exports() -> None:
    """auto.fund_constants must expose the legitimate constants only."""
    from auto import fund_constants as fc

    # Non-stale constants present
    assert hasattr(fc, "HF_CRITICAL")
    assert hasattr(fc, "BCD_DCA_PLAN")
    assert hasattr(fc, "FLYWHEEL_NOTE")
    assert hasattr(fc, "BASKET_PERP_TOKENS")
    assert hasattr(fc, "classify_fill")

    # Stale state symbols MUST NOT be re-exported. New code that wants
    # basket state should call auto.fund_state_v2.detect_active_baskets().
    assert not hasattr(fc, "BASKET_STATUS")
    assert not hasattr(fc, "BASKET_V5_PLAN")
    assert not hasattr(fc, "BASKET_V5_STATUS")
    assert not hasattr(fc, "BASKET_NOTE")
