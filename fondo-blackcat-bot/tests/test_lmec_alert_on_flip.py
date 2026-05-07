"""R-BOT-LMEC-AUTOFEED — Test #4.

Validate the flip-detection pipeline:

* When a leg moves from non-VALIDA → VALIDA between two evaluate calls,
  ``record_legs_snapshot`` reports the leg id in ``flips``.
* ``detect_and_alert_flips`` builds an alert text only when there's a flip.
* No-flip case → empty alert text.
* Idempotent: re-evaluating with the same conditions doesn't re-alert.
"""
from __future__ import annotations

import importlib
import os
import tempfile
from contextlib import contextmanager

import pytest


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="lmec_test_")
    monkeypatch.setenv("DATA_DIR", tmp)
    import config

    monkeypatch.setattr(config, "DATA_DIR", tmp, raising=False)
    yield tmp


@contextmanager
def env(**overrides):
    saved = {k: os.environ.get(k) for k in overrides}
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


def _fresh():
    from modules import lmec_state, lmec_triggers

    importlib.reload(lmec_state)
    return importlib.reload(lmec_triggers)


def _market(price):
    return {"prices": {"BTC": {"price_usd": float(price)}}}


def test_flip_detected_when_macd_turns_positive():
    """First eval: MACD invalida. Second eval: MACD valida → flip."""
    lt = _fresh()
    with env(
        TRADERMAP_BTC_MACD=None,
        LMEC_MACD_WEEKLY_POSITIVE="false",
        LMEC_RSI_WEEKLY="50",
        LMEC_MA50W_USD="75000",
        LMEC_MA50W_BROKEN_WEEKS="0",
    ):
        first = lt.evaluate_lmec_triggers(_market(80_000))
    assert "macd_weekly_positive" not in (first.get("flips") or [])

    with env(
        TRADERMAP_BTC_MACD=None,
        LMEC_MACD_WEEKLY_POSITIVE="true",  # FLIP
        LMEC_RSI_WEEKLY="50",
        LMEC_MA50W_USD="75000",
        LMEC_MA50W_BROKEN_WEEKS="0",
    ):
        second = lt.evaluate_lmec_triggers(_market(80_000))
    assert "macd_weekly_positive" in (second.get("flips") or [])


def test_no_flip_when_state_stable():
    """Two identical evals — no flip recorded."""
    lt = _fresh()
    with env(
        TRADERMAP_BTC_MACD=None,
        LMEC_MACD_WEEKLY_POSITIVE="false",
        LMEC_RSI_WEEKLY="50",
        LMEC_MA50W_USD="75000",
        LMEC_MA50W_BROKEN_WEEKS="0",
    ):
        lt.evaluate_lmec_triggers(_market(80_000))
        second = lt.evaluate_lmec_triggers(_market(80_000))
    assert (second.get("flips") or []) == []


def test_detect_and_alert_flips_builds_text_on_flip():
    lt = _fresh()
    # First, prime state with a non-VALIDA snapshot.
    with env(
        TRADERMAP_BTC_MACD=None,
        LMEC_MACD_WEEKLY_POSITIVE="false",
        LMEC_RSI_WEEKLY="50",
        LMEC_MA50W_USD="75000",
        LMEC_MA50W_BROKEN_WEEKS="0",
    ):
        lt.evaluate_lmec_triggers(_market(80_000))
    # Now flip MACD + RSI.
    with env(
        TRADERMAP_BTC_MACD=None,
        LMEC_MACD_WEEKLY_POSITIVE="true",
        LMEC_RSI_WEEKLY="75",  # > 70 → VALIDA
        LMEC_MA50W_USD="75000",
        LMEC_MA50W_BROKEN_WEEKS="0",
    ):
        out = lt.detect_and_alert_flips(_market(80_000))
    assert out["alert_text"]
    assert "VALIDA" in out["alert_text"]
    assert len(out["flips"]) >= 1


def test_detect_and_alert_flips_empty_on_no_change():
    lt = _fresh()
    with env(
        TRADERMAP_BTC_MACD=None,
        LMEC_MACD_WEEKLY_POSITIVE="false",
        LMEC_RSI_WEEKLY="50",
        LMEC_MA50W_USD="75000",
        LMEC_MA50W_BROKEN_WEEKS="0",
    ):
        lt.detect_and_alert_flips(_market(80_000))
        out = lt.detect_and_alert_flips(_market(80_000))
    assert out["alert_text"] == ""
    assert out["flips"] == []


def test_format_lmec_block_includes_data_source_label():
    lt = _fresh()
    with env(
        TRADERMAP_BTC_MACD="positive",
        LMEC_MACD_WEEKLY_POSITIVE=None,
        LMEC_RSI_WEEKLY="50",
        LMEC_MA50W_USD="75000",
        LMEC_MA50W_BROKEN_WEEKS="0",
    ):
        result = lt.evaluate_lmec_triggers(_market(80_000))
        text = lt.format_lmec_block(result)
    assert "(data:" in text
    # Source must mention tradermap because the override is present.
    assert "tradermap" in text.lower()


def test_format_lmec_status_renders_persisted_state():
    lt = _fresh()
    with env(
        LMEC_MACD_WEEKLY_POSITIVE="false",
        LMEC_RSI_WEEKLY="50",
        LMEC_MA50W_USD="75000",
        LMEC_MA50W_BROKEN_WEEKS="0",
    ):
        result = lt.evaluate_lmec_triggers(_market(80_000))
        text = lt.format_lmec_status(result)
    assert "/lmec_status" in text
    assert "TraderMap health" in text
    assert "Persisted state" in text
