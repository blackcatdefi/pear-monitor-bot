"""R-NOISE-CUT (2026-06-16) — MARGIN STRESS removed from paging entirely.

Supersedes R-MARGIN-STRESS-HOTFIX. The perp-cross-margin-used vs cross-equity
ratio is NOT a risk metric: under the fund's unified Portfolio Margin the perp
cross sub-account rests at ~100% utilization by construction (thin perp equity;
HYPE spot collateral cross-margins everything). It fired every few hours with
no actionable content, so ``run_margin_alerts`` no longer pushes it at all. The
one real datum (≥100% blocks opening NEW perp legs) now lives in the /reporte
PM panel as an INFORMATIONAL line (``format_perp_cross_util_line``).

Invariants under test:
  (a) replay production state (2 isolated, 0 cross) → ZERO pushes;
  (b) synthetic cross account at 95% / 105% → ZERO MARGIN STRESS pushes;
  (c) repeated polls / band transitions → still ZERO pushes;
  (d) the panel info line renders the utilization with honest non-liq copy;
  (e) the REAL-RISK channel (aave-HF + per-position liq) is UNAFFECTED.
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


# ── (b) cross account at 95% / 105% → ZERO MARGIN STRESS pushes ─────────────

def test_cross_account_95_never_pushes_margin_stress(am, harness):
    sent = harness
    wallets = [_cross_wallet(95.0)]
    asyncio.run(am.run_margin_alerts(None, wallets))
    assert not any("MARGIN STRESS" in m for m in sent), (
        "MARGIN STRESS must never push — it moved to the PM panel as info"
    )


def test_cross_account_over_100_never_pushes(am, harness):
    sent = harness
    asyncio.run(am.run_margin_alerts(None, [_cross_wallet(105.0)]))
    assert not any("MARGIN STRESS" in m for m in sent)


# ── (c) repeated polls / band transitions → still ZERO pushes ───────────────

def test_repeated_polls_and_transitions_silent(am, harness):
    sent = harness
    asyncio.run(am.run_margin_alerts(None, [_cross_wallet(95.0)]))
    asyncio.run(am.run_margin_alerts(None, [_cross_wallet(95.5)]))   # same band
    asyncio.run(am.run_margin_alerts(None, [_cross_wallet(105.0)]))  # band jump
    for _ in range(6):
        asyncio.run(am.run_margin_alerts(None, [_cross_wallet(105.0)]))
    assert not any("MARGIN STRESS" in m for m in sent)


# ── (d) panel info line renders the ex-stress datum (never a push) ──────────

def test_panel_util_helper_and_line(am):
    # cross legs present + cross fields available → util computed.
    w = _cross_wallet(95.0)["data"]
    util, n = am.perp_cross_utilization(w)
    assert n == 1 and util == pytest.approx(95.0)
    line = am.format_perp_cross_util_line(util)
    assert "Perp cross utilization: 95.0%" in line
    assert "head-room" in line
    assert "liquidación" in line.lower()  # honest: NOT a liq signal
    # at/over 100% → blocks-new-positions copy, still non-liq framing.
    over = am.format_perp_cross_util_line(101.2)
    assert "bloquea ABRIR nuevas patas perp" in over
    assert "MARGIN STRESS" not in over and "🚨" not in over


def test_panel_util_none_when_not_applicable(am):
    # zero cross legs → N/A.
    assert am.perp_cross_utilization(_prod_iso_only_wallet()["data"]) == (None, 0)
    # cross legs but cross fields stale/missing → N/A, never blend.
    w = _cross_wallet(95.0)["data"]
    del w["cross_margin_used"]
    util, n = am.perp_cross_utilization(w)
    assert util is None and n == 1


def test_pm_panel_renders_util_info_line(am):
    """The /reporte PM panel renders the info line, with no push wording."""
    from modules.portfolio_margin import PMState, format_pm_state_telegram
    pm = PMState(
        collateral_usd=80_000.0, debt_usd=0.0, capacity_usd=40_000.0,
        available_usd=40_000.0, ratio=0.0, status="CALM",
        shorts_notional=0.0, naked_long=False, hype_qty=1000.0, hype_px=80.0,
    )
    block = format_pm_state_telegram(
        pm, perp_cross_util_pct=100.3, perp_cross_count=1
    )
    assert "Perp cross utilization: 100.3%" in block
    assert "bloquea ABRIR nuevas patas perp" in block
    assert "MARGIN STRESS" not in block
    # default (no util passed) → no line, panel unchanged.
    assert "Perp cross utilization" not in format_pm_state_telegram(pm)


# ── guard hardening ─────────────────────────────────────────────────────────

def test_blended_metric_never_used(am, harness):
    """Cross legs exist but cross fields are MISSING (stale cache) → never push
    and never fall back to the blended marginSummary for the panel util."""
    sent = harness
    w = _cross_wallet(95.0)
    del w["data"]["cross_margin_used"]
    w["data"]["total_margin_used"] = w["data"]["account_value"]  # blended 100%
    asyncio.run(am.run_margin_alerts(None, [w]))
    assert not any("MARGIN STRESS" in m for m in sent)
    assert am.perp_cross_utilization(w["data"])[0] is None


def test_cross_below_90_silent(am, harness):
    sent = harness
    asyncio.run(am.run_margin_alerts(None, [_cross_wallet(45.0)]))
    assert sent == []


# ── (e) REAL-RISK channel intact — per-position liq distance still pushes ────

def test_real_risk_liq_distance_still_pushes(am, harness):
    """Removing MARGIN STRESS must NOT weaken the real-risk channel: a leg 5%
    from its liq price must still page, with no MARGIN STRESS noise."""
    sent = harness
    w = {
        "status": "ok",
        "data": {
            "wallet": "0x" + "a" * 40,
            "label": "Trading",
            "cross_account_value": 20_000.0,
            "cross_margin_used": 1_000.0,
            "positions": [
                {
                    "coin": "ETH", "size": -2.0,
                    "notional_usd": 6_000.0, "positionValue": 6_000.0,
                    "liq_px": 3_150.0,  # mark=3000 → dist 5.0% (<8%)
                    "leverage_type": "cross",
                },
            ],
        },
    }
    asyncio.run(am.run_margin_alerts(None, [w]))
    assert any("DISTANCIA A LIQ" in m for m in sent), "real-risk leg must page"
    assert not any("MARGIN STRESS" in m for m in sent)


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
