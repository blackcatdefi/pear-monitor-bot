"""R-MARGIN-STRESS-HOTFIX — cross-only metric + no-cross guard tests.

Live evidence killed (2026-06-10 03:08-10:38 UTC): 15 identical MARGIN STRESS
alerts every 30 min on an iso-only account (LONG BTC + LONG SOL). Root cause:
the blended ``marginSummary`` counts isolated margin in BOTH used and equity,
so used/equity == 100% by construction with zero cross positions — plus the
false "Buffer to liquidation <0.0%" copy.

Mandated acceptance tests:
  (a) replay current production state (2 isolated, 0 cross) → ZERO MARGIN
      STRESS alerts (at most one informational iso-only line on transition);
  (b) synthetic cross account at 95% → exactly ONE alert with the new copy;
  (c) repeated polls in the same band → silence;
  (d) band transition → one new alert.
"""
from __future__ import annotations

import asyncio
import importlib

import pytest


@pytest.fixture
def am(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import config
    importlib.reload(config)
    import modules.alerts_margin as alerts_margin
    importlib.reload(alerts_margin)
    return alerts_margin


@pytest.fixture
def harness(monkeypatch, am):
    """Wire run_margin_alerts to a capture sink; mute PM channel."""
    sent: list[str] = []

    async def _capture(bot, chat_id, msg, **kwargs):
        sent.append(msg)

    import config
    import utils.telegram as ut
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "12345", raising=False)
    monkeypatch.setattr(ut, "send_bot_message", _capture, raising=False)
    import modules.pm_context as pm_context
    monkeypatch.setattr(
        pm_context, "select_primary_pm_state", lambda *a, **k: None
    )
    return sent


def _iso_position(coin: str, mark: float, liq: float, size: float) -> dict:
    return {
        "coin": coin,
        "size": size,
        "side": "LONG",
        "entry_px": mark,
        "notional_usd": mark * size,
        "liq_px": liq,
        "leverage": 5,
        "leverage_type": "isolated",
        "margin_used": 10_000.0,
        "dex": "main",
    }


def _prod_iso_only_wallet() -> dict:
    """Replay of live 2026-06-10 state: blended used==equity==$20,393,
    only isolated LONG BTC (liq 18% away) + LONG SOL (liq 15% away)."""
    return {
        "status": "ok",
        "data": {
            "wallet": "0xc7ae0123456789abcdef0123456789abcdef1505",
            "label": "Trading",
            "account_value": 20_393.0,
            "total_margin_used": 20_393.0,   # 100% blended BY CONSTRUCTION
            "cross_account_value": 0.0,
            "cross_margin_used": 0.0,
            "positions": [
                _iso_position("BTC", 61_041.0, 50_054.0, 0.2),  # 18.0% away
                _iso_position("SOL", 64.53, 54.85, 100.0),      # 15.0% away
            ],
        },
    }


def _cross_wallet(ratio_pct: float, *, eq: float = 20_000.0) -> dict:
    used = eq * ratio_pct / 100.0
    return {
        "status": "ok",
        "data": {
            "wallet": "0xabcdef0123456789abcdef0123456789abcdef01",
            "label": "Trading",
            "account_value": eq,
            "total_margin_used": used,
            "cross_account_value": eq,
            "cross_margin_used": used,
            "positions": [
                {
                    "coin": "ETH",
                    "size": -2.0,
                    "side": "SHORT",
                    "notional_usd": 6_000.0,
                    "liq_px": 4_500.0,
                    "leverage_type": "cross",
                    "dex": "main",
                },
            ],
        },
    }


# ── (a) replay production: iso-only → ZERO MARGIN STRESS alerts ─────────────

def test_prod_replay_iso_only_zero_margin_stress(am, harness):
    sent = harness
    wallets = [_prod_iso_only_wallet()]
    for _ in range(5):  # five half-hourly polls (the live spam pattern)
        asyncio.run(am.run_margin_alerts(None, wallets))
    stress = [m for m in sent if "MARGIN STRESS" in m]
    assert stress == [], f"iso-only account must NEVER fire MARGIN STRESS: {stress}"
    # Liq distances 18%/15% are above the 12% real-risk threshold → silent.
    assert not any("DISTANCIA A LIQ" in m for m in sent)
    # At most ONE informational line on the transition into iso-only.
    infos = [m for m in sent if "isolated margins" in m]
    assert len(infos) <= 1
    if infos:
        assert "NOT a liquidation risk" in infos[0]
        assert "Buffer to liquidation" not in infos[0]


def test_iso_only_info_fires_once_with_24h_cooldown(am):
    t0 = 1_000_000.0
    # Transition into iso-only → fires once.
    assert am.evaluate_iso_only_transition("w1", True, now=t0) is True
    # Same state, later polls → silence (persisted latch).
    assert am.evaluate_iso_only_transition("w1", True, now=t0 + 1800) is False
    # Out and back in WITHIN 24h → still silent (cooldown).
    assert am.evaluate_iso_only_transition("w1", False, now=t0 + 3600) is False
    assert am.evaluate_iso_only_transition("w1", True, now=t0 + 7200) is False
    # Out and back in AFTER 24h → fires again.
    assert am.evaluate_iso_only_transition("w1", False, now=t0 + 90_000) is False
    assert am.evaluate_iso_only_transition("w1", True, now=t0 + 91_000) is True


# ── (b) synthetic cross account at 95% → exactly ONE alert, new copy ────────

def test_cross_account_95_fires_exactly_once_with_new_copy(am, harness):
    sent = harness
    wallets = [_cross_wallet(95.0)]
    asyncio.run(am.run_margin_alerts(None, wallets))
    stress = [m for m in sent if "MARGIN STRESS" in m]
    assert len(stress) == 1
    msg = stress[0]
    assert "Perp cross margin used vs cross equity = 95.0%" in msg
    assert "Above 100% blocks NEW positions" in msg
    assert "tracked per position and in the PM panel" in msg
    assert "Buffer to liquidation" not in msg
    assert "buffer to liquidation" not in msg.lower()


# ── (c) repeated polls in the same band → silence ───────────────────────────

def test_repeated_polls_same_band_silent(am, harness):
    sent = harness
    wallets = [_cross_wallet(95.0)]
    for _ in range(6):
        asyncio.run(am.run_margin_alerts(None, wallets))
    stress = [m for m in sent if "MARGIN STRESS" in m]
    assert len(stress) == 1, f"same band must not re-fire: {len(stress)}"


# ── (d) band transition → one new alert ─────────────────────────────────────

def test_band_transition_fires_one_new_alert(am, harness):
    sent = harness
    asyncio.run(am.run_margin_alerts(None, [_cross_wallet(95.0)]))
    asyncio.run(am.run_margin_alerts(None, [_cross_wallet(95.5)]))  # same band
    asyncio.run(am.run_margin_alerts(None, [_cross_wallet(105.0)]))  # 90-100 → 100-110
    stress = [m for m in sent if "MARGIN STRESS" in m]
    assert len(stress) == 2
    assert "105.0%" in stress[1]


# ── guard hardening ─────────────────────────────────────────────────────────

def test_blended_metric_never_used(am, harness):
    """Cross legs exist but cross fields are MISSING (stale cache) → the
    alert must SKIP, never fall back to the blended marginSummary."""
    sent = harness
    w = _cross_wallet(95.0)
    del w["data"]["cross_margin_used"]
    w["data"]["total_margin_used"] = w["data"]["account_value"]  # blended 100%
    asyncio.run(am.run_margin_alerts(None, [w]))
    assert not any("MARGIN STRESS" in m for m in sent)


def test_cross_below_90_silent(am, harness):
    sent = harness
    asyncio.run(am.run_margin_alerts(None, [_cross_wallet(45.0)]))
    assert sent == []


def test_count_cross_positions(am):
    assert am.count_cross_positions(None) == 0
    assert am.count_cross_positions([_iso_position("BTC", 100, 80, 1)]) == 0
    legs = [
        _iso_position("BTC", 100, 80, 1),
        {"coin": "ETH", "size": 1.0, "leverage_type": "cross"},
        {"coin": "DOGE", "size": 2.0},  # unknown mode → counts as cross
        {"coin": "DUST", "size": 0.0, "leverage_type": "cross"},  # closed
    ]
    assert am.count_cross_positions(legs) == 2


def test_empty_wallet_no_info_line(am, harness):
    """Zero positions at all (no iso legs) → no info line, no stress."""
    sent = harness
    w = {
        "status": "ok",
        "data": {
            "wallet": "0x" + "9" * 40,
            "label": "Idle",
            "account_value": 0.0,
            "total_margin_used": 0.0,
            "cross_account_value": 0.0,
            "cross_margin_used": 0.0,
            "positions": [],
        },
    }
    asyncio.run(am.run_margin_alerts(None, [w]))
    assert sent == []


def test_portfolio_extracts_cross_margin_used():
    """_summarize_positions must surface crossMarginSummary.totalMarginUsed."""
    from modules.portfolio import _summarize_positions
    state = {
        "marginSummary": {"accountValue": "20393", "totalMarginUsed": "20393"},
        "crossMarginSummary": {"accountValue": "1500", "totalMarginUsed": "300"},
        "assetPositions": [],
    }
    out = _summarize_positions(state)
    assert out["cross_margin_used"] == 300.0
    assert out["cross_account_value"] == 1500.0
    assert out["total_margin_used"] == 20393.0
