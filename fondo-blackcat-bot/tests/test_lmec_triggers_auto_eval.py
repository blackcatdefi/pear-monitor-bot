"""R-BOT-FEEDS-EXPAND (2026-05-07) — Task 3.

LMEC bear-invalidation triggers must consume TraderMap.io overrides when
present, and fall back to LMEC_* env vars otherwise. Precedence rules:

  Leg #2 (MACD weekly): TRADERMAP_BTC_MACD > LMEC_MACD_WEEKLY_POSITIVE
  Leg #3 (RSI weekly):  TRADERMAP_BTC_RSI  > LMEC_RSI_WEEKLY
  Leg #4 (MA50w):       TRADERMAP_BTC_MA50W > LMEC_MA50W_USD
                        LMEC_MA50W_BROKEN_WEEKS stays env-var-only
                        (TraderMap doesn't expose weeks-broken).

Leg #1 (BTC > ATH) is unaffected — driven by live market dict + LMEC_BTC_ATH_USD.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

from modules.lmec_triggers import evaluate_lmec_triggers


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


# ── Leg #2 — MACD weekly positive ───────────────────────────────────────


def test_tradermap_macd_overrides_when_lmec_unset():
    """TRADERMAP_BTC_MACD=positive → leg #2 VALIDA even with LMEC unset."""
    with env(
        TRADERMAP_BTC_MACD="positive",
        TRADERMAP_BTC_RSI=None,
        TRADERMAP_BTC_MA50W=None,
        LMEC_MACD_WEEKLY_POSITIVE=None,
    ):
        result = evaluate_lmec_triggers(_market(80_000))
    leg = next(c for c in result["conditions"] if c["id"] == "macd_weekly_positive")
    assert leg["status"] == "VALIDA"


def test_tradermap_macd_negative_overrides_lmec_positive():
    """When both set, TraderMap wins — negative TraderMap → INVALIDA."""
    with env(
        TRADERMAP_BTC_MACD="negative",
        TRADERMAP_BTC_RSI=None,
        TRADERMAP_BTC_MA50W=None,
        LMEC_MACD_WEEKLY_POSITIVE="true",  # would normally → VALIDA
    ):
        result = evaluate_lmec_triggers(_market(80_000))
    leg = next(c for c in result["conditions"] if c["id"] == "macd_weekly_positive")
    assert leg["status"] == "INVALIDA"


def test_lmec_macd_used_when_tradermap_unset():
    """No TraderMap override → fall back to LMEC env var."""
    with env(
        TRADERMAP_BTC_MACD=None,
        TRADERMAP_BTC_RSI=None,
        TRADERMAP_BTC_MA50W=None,
        LMEC_MACD_WEEKLY_POSITIVE="true",
    ):
        result = evaluate_lmec_triggers(_market(80_000))
    leg = next(c for c in result["conditions"] if c["id"] == "macd_weekly_positive")
    assert leg["status"] == "VALIDA"


def test_macd_unknown_when_neither_set():
    with env(
        TRADERMAP_BTC_MACD=None,
        TRADERMAP_BTC_RSI=None,
        TRADERMAP_BTC_MA50W=None,
        LMEC_MACD_WEEKLY_POSITIVE=None,
    ):
        result = evaluate_lmec_triggers(_market(80_000))
    leg = next(c for c in result["conditions"] if c["id"] == "macd_weekly_positive")
    assert leg["status"] == "UNKNOWN"


# ── Leg #3 — RSI weekly > 70 ────────────────────────────────────────────


def test_tradermap_rsi_overrides_when_lmec_unset():
    """TRADERMAP_BTC_RSI=75 → leg #3 VALIDA even with LMEC_RSI_WEEKLY unset."""
    with env(
        TRADERMAP_BTC_RSI="75",
        TRADERMAP_BTC_MACD=None,
        TRADERMAP_BTC_MA50W=None,
        LMEC_RSI_WEEKLY=None,
    ):
        result = evaluate_lmec_triggers(_market(80_000))
    leg = next(c for c in result["conditions"] if c["id"] == "rsi_weekly_above_70")
    assert leg["status"] == "VALIDA"


def test_tradermap_rsi_overrides_lmec_when_both_set():
    """TraderMap RSI=80 wins over LMEC RSI=40 → VALIDA."""
    with env(
        TRADERMAP_BTC_RSI="80",
        TRADERMAP_BTC_MACD=None,
        TRADERMAP_BTC_MA50W=None,
        LMEC_RSI_WEEKLY="40",
    ):
        result = evaluate_lmec_triggers(_market(80_000))
    leg = next(c for c in result["conditions"] if c["id"] == "rsi_weekly_above_70")
    assert leg["status"] == "VALIDA"


def test_lmec_rsi_used_when_tradermap_unset():
    with env(
        TRADERMAP_BTC_RSI=None,
        TRADERMAP_BTC_MACD=None,
        TRADERMAP_BTC_MA50W=None,
        LMEC_RSI_WEEKLY="55",
    ):
        result = evaluate_lmec_triggers(_market(80_000))
    leg = next(c for c in result["conditions"] if c["id"] == "rsi_weekly_above_70")
    # 55 < 70 - 5 (default neutral band) → INVALIDA
    assert leg["status"] == "INVALIDA"


def test_tradermap_rsi_garbage_falls_back_to_lmec():
    """A non-numeric TRADERMAP_BTC_RSI must not crash; should fall back to LMEC."""
    with env(
        TRADERMAP_BTC_RSI="not-a-number",
        TRADERMAP_BTC_MACD=None,
        TRADERMAP_BTC_MA50W=None,
        LMEC_RSI_WEEKLY="72",
    ):
        result = evaluate_lmec_triggers(_market(80_000))
    leg = next(c for c in result["conditions"] if c["id"] == "rsi_weekly_above_70")
    # LMEC fallback 72 > 70 → VALIDA
    assert leg["status"] == "VALIDA"


# ── Leg #4 — MA50w broken sustained ─────────────────────────────────────


def test_tradermap_ma50w_overrides_when_lmec_unset():
    """TRADERMAP_BTC_MA50W=95000 + weeks_broken from env → VALIDA when sustained."""
    with env(
        TRADERMAP_BTC_MA50W="95000",
        TRADERMAP_BTC_MACD=None,
        TRADERMAP_BTC_RSI=None,
        LMEC_MA50W_USD=None,
        LMEC_MA50W_BROKEN_WEEKS="3",
        LMEC_MA50W_SUSTAINED_WEEKS="2",
    ):
        result = evaluate_lmec_triggers(_market(105_000))
    leg = next(c for c in result["conditions"] if c["id"] == "ma50w_broken_sustained")
    assert leg["status"] == "VALIDA"


def test_tradermap_ma50w_overrides_lmec():
    """TraderMap MA50w wins over LMEC MA50w when both set."""
    with env(
        TRADERMAP_BTC_MA50W="95000",
        TRADERMAP_BTC_MACD=None,
        TRADERMAP_BTC_RSI=None,
        LMEC_MA50W_USD="60000",  # would otherwise make BTC 105K trivially > MA
        LMEC_MA50W_BROKEN_WEEKS="0",
        LMEC_MA50W_SUSTAINED_WEEKS="2",
    ):
        result = evaluate_lmec_triggers(_market(105_000))
    leg = next(c for c in result["conditions"] if c["id"] == "ma50w_broken_sustained")
    # weeks_broken=0 < sustained_min=2 → NEUTRO (BTC > MA but not sustained)
    assert leg["status"] == "NEUTRO"


def test_weeks_broken_remains_env_var_only():
    """LMEC_MA50W_BROKEN_WEEKS controls the sustained-weeks check (TraderMap
    does not expose it). Without it, leg should be UNKNOWN."""
    with env(
        TRADERMAP_BTC_MA50W="95000",
        TRADERMAP_BTC_MACD=None,
        TRADERMAP_BTC_RSI=None,
        LMEC_MA50W_USD="95000",
        LMEC_MA50W_BROKEN_WEEKS=None,
    ):
        result = evaluate_lmec_triggers(_market(105_000))
    leg = next(c for c in result["conditions"] if c["id"] == "ma50w_broken_sustained")
    assert leg["status"] == "UNKNOWN"


# ── Leg #1 unaffected ──────────────────────────────────────────────────


def test_btc_above_ath_unaffected_by_tradermap():
    """Leg #1 reads market dict + LMEC_BTC_ATH_USD only."""
    with env(
        LMEC_BTC_ATH_USD="98000",
        TRADERMAP_BTC_RSI="80",  # set everything to ensure it doesn't bleed
        TRADERMAP_BTC_MACD="positive",
        TRADERMAP_BTC_MA50W="50000",
    ):
        result = evaluate_lmec_triggers(_market(105_000))
    leg = next(c for c in result["conditions"] if c["id"] == "btc_above_ath")
    assert leg["status"] == "VALIDA"


# ── Aggregate behavior ──────────────────────────────────────────────────


def test_all_four_triggered_via_tradermap_only():
    """When all 4 legs flip via TraderMap (+ btc>ath via market) → all_triggered."""
    with env(
        LMEC_BTC_ATH_USD="98000",
        TRADERMAP_BTC_MACD="positive",
        TRADERMAP_BTC_RSI="75",
        TRADERMAP_BTC_MA50W="95000",
        LMEC_MA50W_USD=None,
        LMEC_MA50W_BROKEN_WEEKS="3",
        LMEC_MA50W_SUSTAINED_WEEKS="2",
        LMEC_MACD_WEEKLY_POSITIVE=None,
        LMEC_RSI_WEEKLY=None,
    ):
        result = evaluate_lmec_triggers(_market(105_000))
    assert result["all_triggered"] is True
    assert result["triggered_count"] == 4
