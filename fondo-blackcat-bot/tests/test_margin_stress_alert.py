"""R-ONDEMAND (2026-05-09) — margin-stress alert path coverage.

Validates:
  * Pure ratio helper handles empty wallets, negative values, and bad types.
  * Edge-triggered alert fires once per breach and clears on recovery.
  * Idle wallets (no perp equity) never alert.
  * Threshold respects MARGIN_STRESS_ALERT_PCT.
  * Telegram emit goes through ``send_bot_message`` with a structured msg.
"""
from __future__ import annotations

import asyncio
import importlib

import pytest


@pytest.fixture
def alerts_module(monkeypatch, tmp_path):
    """Reload alerts module against a temp data dir so state is isolated."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import config
    importlib.reload(config)
    import modules.alerts as alerts
    importlib.reload(alerts)
    return alerts


def test_ratio_idle_wallet_returns_none(alerts_module):
    assert alerts_module.margin_stress_ratio(0.0, 0.0) is None
    assert alerts_module.margin_stress_ratio(0.0, 100.0) is None
    assert alerts_module.margin_stress_ratio(-5.0, 100.0) is None


def test_ratio_basic(alerts_module):
    assert alerts_module.margin_stress_ratio(1000.0, 900.0) == pytest.approx(0.9)
    assert alerts_module.margin_stress_ratio(2000.0, 100.0) == pytest.approx(0.05)


def test_ratio_garbage_returns_none(alerts_module):
    assert alerts_module.margin_stress_ratio("abc", 100.0) is None
    assert alerts_module.margin_stress_ratio(None, 50.0) is None
    assert alerts_module.margin_stress_ratio({}, []) is None


class _FakeBot:
    def __init__(self):
        self.sent: list[str] = []


@pytest.fixture
def fake_bot(monkeypatch):
    """Patch ``send_bot_message`` and ``TELEGRAM_CHAT_ID`` so emits stay local."""
    import modules.alerts as alerts

    bot = _FakeBot()
    sent_messages: list[str] = []

    async def _capture(_bot, _chat_id, msg):
        sent_messages.append(msg)

    monkeypatch.setattr(alerts, "send_bot_message", _capture)
    monkeypatch.setattr(alerts, "TELEGRAM_CHAT_ID", "12345")
    return bot, sent_messages


def _wallet(account_value: float, total_margin_used: float, *, label="Trading", addr="0xabcdef0123456789abcdef0123456789abcdef01") -> dict:
    return {
        "status": "ok",
        "data": {
            "wallet": addr,
            "label": label,
            "account_value": account_value,
            "total_margin_used": total_margin_used,
        },
    }


def test_breach_emits_once_and_clears(alerts_module, fake_bot):
    bot, sent = fake_bot
    state: dict = {}
    wallets = [_wallet(1000.0, 950.0)]  # 95% — over threshold

    asyncio.run(alerts_module._run_margin_stress_alerts(bot, state, wallets, threshold_pct=90.0))
    assert len(sent) == 1, "first breach should emit"
    assert "MARGIN STRESS" in sent[0]
    assert "95.0%" in sent[0]

    # Second cycle still over threshold → must NOT re-emit (edge-triggered).
    asyncio.run(alerts_module._run_margin_stress_alerts(bot, state, wallets, threshold_pct=90.0))
    assert len(sent) == 1, "second cycle while still breached must not duplicate alert"

    # Recovery → state cleared, ratio dropped under threshold.
    wallets[0]["data"]["total_margin_used"] = 500.0  # 50%
    asyncio.run(alerts_module._run_margin_stress_alerts(bot, state, wallets, threshold_pct=90.0))
    assert len(sent) == 1, "recovery must not emit"
    # State should have cleaned up the wallet's margin_stress_ key.
    assert not any(k.startswith("margin_stress_") and v for k, v in state.items())

    # Re-breach after recovery → fresh emit.
    wallets[0]["data"]["total_margin_used"] = 920.0  # 92%
    asyncio.run(alerts_module._run_margin_stress_alerts(bot, state, wallets, threshold_pct=90.0))
    assert len(sent) == 2, "fresh breach after recovery must re-emit"


def test_idle_wallet_never_alerts(alerts_module, fake_bot):
    bot, sent = fake_bot
    state: dict = {}
    wallets = [_wallet(0.0, 0.0)]
    asyncio.run(alerts_module._run_margin_stress_alerts(bot, state, wallets, threshold_pct=90.0))
    assert sent == []


def test_threshold_override_via_param(alerts_module, fake_bot):
    bot, sent = fake_bot
    state: dict = {}
    wallets = [_wallet(1000.0, 800.0)]  # 80%
    asyncio.run(alerts_module._run_margin_stress_alerts(bot, state, wallets, threshold_pct=90.0))
    assert sent == [], "below threshold (80% < 90%) must not emit"
    asyncio.run(alerts_module._run_margin_stress_alerts(bot, state, wallets, threshold_pct=70.0))
    assert len(sent) == 1, "with threshold lowered to 70%, the same 80% ratio breaches"


def test_status_not_ok_skipped(alerts_module, fake_bot):
    bot, sent = fake_bot
    state: dict = {}
    wallets = [{"status": "fetch_error", "data": {}}]
    asyncio.run(alerts_module._run_margin_stress_alerts(bot, state, wallets, threshold_pct=90.0))
    assert sent == []


def test_multi_wallet_independent(alerts_module, fake_bot):
    bot, sent = fake_bot
    state: dict = {}
    wallets = [
        _wallet(1000.0, 950.0, label="Trading", addr="0x" + "a" * 40),  # breach
        _wallet(1000.0, 100.0, label="Flywheel", addr="0x" + "b" * 40),  # safe
    ]
    asyncio.run(alerts_module._run_margin_stress_alerts(bot, state, wallets, threshold_pct=90.0))
    assert len(sent) == 1
    assert "Trading" in sent[0]
