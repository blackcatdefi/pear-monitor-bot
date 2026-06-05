"""R-BOT-TERMINOLOGY-UNIFY (2026-05-07) — Bug #5.

LMEC bear-invalidation triggers must:
1. Return one entry per condition (4 total).
2. Status must be one of {VALIDA, NEUTRO, INVALIDA, UNKNOWN}.
3. BTC > ATH must flip leg #1 to VALIDA.
4. Aggregate counters (any_triggered, all_triggered, triggered_count)
   must be consistent with the per-condition statuses.
5. format_lmec_block() must include the 4 leg names and the ACCIÓN
   SUGERIDA line when at least one leg is VALIDA.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

from modules.lmec_triggers import evaluate_lmec_triggers, format_lmec_block


@contextmanager
def env(**overrides):
    """Temporarily set / unset env vars and restore afterwards."""
    saved: dict[str, str | None] = {k: os.environ.get(k) for k in overrides}
    try:
        for k, v in overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _market(btc_price: float | None) -> dict | None:
    if btc_price is None:
        return None
    return {"prices": {"BTC": {"price_usd": float(btc_price)}}}


def test_evaluate_returns_four_conditions_with_known_statuses():
    """All 4 legs must be present with a valid status field."""
    with env(
        LMEC_BTC_ATH_USD="98000",
        LMEC_MACD_WEEKLY_POSITIVE="false",
        LMEC_RSI_WEEKLY="55",
        LMEC_MA50W_USD="95000",
        LMEC_MA50W_BROKEN_WEEKS="0",
        # Make sure TraderMap doesn't shadow LMEC env vars.
        TRADERMAP_BTC_RSI=None,
        TRADERMAP_BTC_MACD=None,
        TRADERMAP_BTC_MA50W=None,
    ):
        result = evaluate_lmec_triggers(_market(80_000))
    assert isinstance(result, dict)
    conds = result["conditions"]
    assert len(conds) == 4
    valid = {"VALIDA", "NEUTRO", "INVALIDA", "UNKNOWN", "AWAITING_BCD"}
    for c in conds:
        assert c["status"] in valid, c


def test_btc_above_ath_flips_to_valida():
    with env(
        LMEC_BTC_ATH_USD="98000",
        LMEC_MACD_WEEKLY_POSITIVE="false",
        LMEC_RSI_WEEKLY="50",
        LMEC_MA50W_USD="95000",
        LMEC_MA50W_BROKEN_WEEKS="0",
        TRADERMAP_BTC_RSI=None,
        TRADERMAP_BTC_MACD=None,
        TRADERMAP_BTC_MA50W=None,
    ):
        result = evaluate_lmec_triggers(_market(105_000))
    btc_leg = next(c for c in result["conditions"] if c["id"] == "btc_above_ath")
    assert btc_leg["status"] == "VALIDA"
    assert result["any_triggered"] is True
    assert result["triggered_count"] >= 1


def test_btc_below_ath_is_invalida():
    with env(
        LMEC_BTC_ATH_USD="98000",
        TRADERMAP_BTC_RSI=None,
        TRADERMAP_BTC_MACD=None,
        TRADERMAP_BTC_MA50W=None,
    ):
        result = evaluate_lmec_triggers(_market(60_000))
    btc_leg = next(c for c in result["conditions"] if c["id"] == "btc_above_ath")
    assert btc_leg["status"] == "INVALIDA"


def test_all_unknown_when_env_missing_and_no_market():
    """No market + no env vars → legs are non-actionable: the BTC-ATH leg has
    no price feed (UNKNOWN) and the three BCD-manual TA legs read AWAITING_BCD
    (P1.9). No leg triggers."""
    with env(
        LMEC_MACD_WEEKLY_POSITIVE=None,
        LMEC_RSI_WEEKLY=None,
        LMEC_MA50W_USD=None,
        LMEC_MA50W_BROKEN_WEEKS=None,
        TRADERMAP_BTC_RSI=None,
        TRADERMAP_BTC_MACD=None,
        TRADERMAP_BTC_MA50W=None,
    ):
        result = evaluate_lmec_triggers(None)
    statuses = {c["status"] for c in result["conditions"]}
    assert statuses <= {"UNKNOWN", "AWAITING_BCD"}
    assert "VALIDA" not in statuses
    assert result["triggered_count"] == 0
    assert result["any_triggered"] is False
    assert result["all_triggered"] is False
    assert result["triggered_count"] == 0


def test_all_four_triggered_aggregates_correctly():
    with env(
        LMEC_BTC_ATH_USD="98000",
        LMEC_MACD_WEEKLY_POSITIVE="true",
        LMEC_RSI_WEEKLY="75",
        LMEC_MA50W_USD="95000",
        LMEC_MA50W_BROKEN_WEEKS="3",
        LMEC_MA50W_SUSTAINED_WEEKS="2",
        TRADERMAP_BTC_RSI=None,
        TRADERMAP_BTC_MACD=None,
        TRADERMAP_BTC_MA50W=None,
    ):
        result = evaluate_lmec_triggers(_market(105_000))
    assert result["all_triggered"] is True
    assert result["triggered_count"] == 4


def test_format_lmec_block_renders_all_legs():
    with env(
        LMEC_BTC_ATH_USD="98000",
        LMEC_MACD_WEEKLY_POSITIVE="true",
        LMEC_RSI_WEEKLY="75",
        LMEC_MA50W_USD="95000",
        LMEC_MA50W_BROKEN_WEEKS="3",
        LMEC_MA50W_SUSTAINED_WEEKS="2",
        TRADERMAP_BTC_RSI=None,
        TRADERMAP_BTC_MACD=None,
        TRADERMAP_BTC_MA50W=None,
    ):
        result = evaluate_lmec_triggers(_market(105_000))
    out = format_lmec_block(result)
    assert "LMEC BEAR INVALIDATION TRIGGERS" in out
    assert "BTC rompe ATH" in out
    assert "MACD" in out
    assert "RSI" in out
    assert "MA50w" in out
    # When all triggered, the action line should appear.
    assert "ACCIÓN SUGERIDA" in out
