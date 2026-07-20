"""R-BOT-DEFINITIVE-2 — Tasks 1 (LIQ REAL 0.7125), 2 (DreamCash liq) & 7
(neutral naked-long line) regression tests.

T1: HL PM liquidation TRIGGERS at portfolio_margin_ratio > 0.95, so the REAL
liquidation LTV is 0.95 × 0.75 = 0.7125 — the real liq price is ALWAYS above
the nominal HF=1.0 maintenance price. Known inputs (debt 113,200 / 3,006.28
HYPE) must yield LIQ REAL ≈ $52.87.
T2: DreamCash (0x171b) BTC long renders its live liquidationPx + distance %,
and kill trigger #5 ``btc_near_dreamcash_liq`` fires at mark <= liq × 1.06.
T7: the naked-long panel line is a neutral note, not a siren alarm.
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from modules.portfolio_margin import (  # noqa: E402
    PMState,
    compute_pm_risk_metrics,
    compute_pm_state,
    format_pm_state_telegram,
)
from modules import basket_killer  # noqa: E402


# ─── T1 — math ────────────────────────────────────────────────────────────────

_DEBT = 113_200.0
_QTY = 3_006.28
_PX = 60.0


def _metrics(**kw):
    breakdown = {"HYPE": _QTY * _PX}
    return compute_pm_risk_metrics(
        breakdown, _DEBT, _QTY, _PX, ltv_map={"HYPE": 0.50}, **kw
    )


def test_liq_real_known_inputs():
    m = _metrics()
    # debt / (0.7125 × qty) — spec: ~52.87 (±0.05 covers the 20-USDC offset).
    assert m["liq_price_real"] == pytest.approx(52.87, abs=0.05)
    # Pure formula (no min-borrow offset): 113200 / (0.7125 × 3006.28).
    m0 = _metrics(min_borrow_offset=0.0)
    assert m0["liq_price_real"] == pytest.approx(
        _DEBT / (0.7125 * _QTY), abs=1e-6
    )


def test_liq_real_always_above_nominal():
    m = _metrics()
    assert m["liq_price_real"] > m["liq_price"] > 0
    # Nominal uses 0.75; real uses 0.7125 → real = nominal / 0.95.
    assert m["liq_price_real"] == pytest.approx(m["liq_price"] / 0.95, rel=1e-9)


def test_hf_at_real_liq_and_buffer():
    m = _metrics()
    assert m["hf_at_real_liq"] == pytest.approx(1.0 / 0.95, rel=1e-9)  # ≈1.053
    # Buffer measured against LIQ REAL, from the oracle px.
    expected_buf = (_PX - m["liq_price_real"]) / _PX * 100.0
    assert m["price_buffer_real_pct"] == pytest.approx(expected_buf, rel=1e-9)
    assert m["price_buffer_real_pct"] < m["price_buffer_pct"]


def test_liq_real_zero_without_debt():
    m = compute_pm_risk_metrics({"HYPE": _QTY * _PX}, 0.0, _QTY, _PX)
    assert m["liq_price_real"] == 0.0
    assert m["price_buffer_real_pct"] == 0.0


def _pm_state() -> PMState:
    return compute_pm_state(
        [
            {"coin": "HYPE", "total": _QTY},
            {"coin": "USDC", "total": -_DEBT, "borrowed": _DEBT},
        ],
        [],
        prices={"HYPE": _PX},
        ltv_map={"HYPE": 0.50},
    )


def test_pm_state_carries_real_liq_fields():
    pm = _pm_state()
    assert pm.liq_price_real == pytest.approx(52.87, abs=0.05)
    assert pm.liq_price_real > pm.liq_price
    assert pm.hf_at_real_liq == pytest.approx(1.053, abs=0.001)


# ─── T1 — panel render ────────────────────────────────────────────────────────

def test_panel_renders_both_liq_lines():
    pm = _pm_state()
    txt = format_pm_state_telegram(pm)
    assert "Liq nominal (maint-LTV 0.75, HF 1.0)" in txt
    assert f"${pm.liq_price:,.2f}" in txt
    assert "LIQ REAL (0.7125, trigger ratio>0.95)" in txt
    assert f"${pm.liq_price_real:,.2f}" in txt
    assert "HF at real liquidation = 1.053" in txt
    # Buffer shown against LIQ REAL.
    assert f"buffer {pm.price_buffer_real_pct:.1f}%" in txt
    # Umbrales line references the REAL liq.
    assert f"liq real ${pm.liq_price_real:,.2f}" in txt


def test_pm_hf_alert_copy_cites_liq_real(monkeypatch):
    class _PM:
        has_data = True
        debt_usd = _DEBT
        aave_hf = 1.05  # < 1.10 → fires
        liq_price_real = 52.86
        hype_px = 60.0

    async def _fake_wallets():
        return []

    monkeypatch.setattr(
        "modules.portfolio.fetch_all_wallets", _fake_wallets, raising=False
    )
    monkeypatch.setattr(
        "modules.pm_context.select_primary_pm_state",
        lambda wallets, market=None: _PM(),
        raising=True,
    )
    res = asyncio.run(basket_killer._evaluate_pm_hf())
    assert res.fired is True
    assert "LIQ REAL (0.7125, ratio>0.95)" in res.detail
    assert "$52.86" in res.detail


# ─── T2 — R-SIGNAL-DIET (2026-07-20): trigger #5 ELIMINADO ───────────────────
# DreamCash wallet 0x171b verificada VACÍA on-chain (accountValue=0, sin
# posiciones) → btc_near_dreamcash_liq + dreamcash_liq_proximity borrados.
# El registry queda en EXACTAMENTE 3 triggers.

def test_dreamcash_trigger_removed():
    assert not hasattr(basket_killer, "_evaluate_btc_near_dreamcash_liq")
    assert not hasattr(basket_killer, "dreamcash_liq_proximity")
    assert not hasattr(basket_killer, "_DREAMCASH_ADDR")


def test_registry_has_three_triggers():
    assert len(basket_killer._TRIGGERS) == 3
    names = {fn.__name__ for fn in basket_killer._TRIGGERS}
    assert names == {
        "_evaluate_btc_above_82k",
        "_evaluate_pm_hf",
        "_evaluate_basket_drawdown",
    }


# ─── T2 — DreamCash position render ──────────────────────────────────────────

def test_position_with_liq_render():
    from templates.formatters import _position_with_liq
    p = {
        "side": "LONG", "coin": "BTC", "liq_px": 48_000.0,
        "entry_px": 52_000.0, "size": 0.5, "unrealized_pnl": -1_000.0,
    }
    # mark = 52,000 + (−1,000 / 0.5) = 50,000 → dist = +4.2%
    out = _position_with_liq(p)
    assert "LONG BTC" in out
    assert "liq $48,000" in out
    assert "dist +4.2% del mark" in out
    # No liq px → plain summary, nothing fabricated.
    assert _position_with_liq({"side": "SHORT", "coin": "OP"}) == "SHORT OP"


# ─── T7 — neutral naked-long line ────────────────────────────────────────────

def test_naked_long_line_is_neutral():
    pm = _pm_state()  # debt, no shorts → naked_long True
    assert pm.naked_long is True
    txt = format_pm_state_telegram(pm)
    assert "Estructura: long apalancado sin hedge activo (decisión del owner)" in txt
    assert "🚨" not in txt
    assert "WARNING" not in txt
    assert "hedge missing" not in txt.lower()
