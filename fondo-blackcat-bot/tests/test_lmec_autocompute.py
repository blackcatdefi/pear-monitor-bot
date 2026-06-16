"""R-LMEC-AUTOCOMPUTE (2026-06-16) — tests for bot-computed weekly TA.

Covers the contract that the three LMEC inputs (weekly MACD / RSI / MA50w)
are computed by the bot from real CLOSED weekly candles, with /setlmec kept
only as a manual OVERRIDE, and that nothing is ever fabricated.

1. Pure compute fns reproduce expected MACD / RSI / MA50w on a fixed fixture.
2. Each of legs 2/3/4 maps to the correct status given known COMPUTED inputs —
   identical thresholds/directions to the pre-change logic.
3. The in-progress week is excluded by the fetch parser (no repaint).
4. n/d path: missing data → AWAITING_BCD; trigger neither fires nor clears.
5. Manual /setlmec override takes precedence over the computed value.
6. A stale computed snapshot is treated as unavailable (→ n/d), never stale.
"""
from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timedelta, timezone

import pytest

from modules import btc_weekly_indicators as ind
from modules import lmec_state


# ── isolation: point lmec_state at a tmp DATA_DIR per test ───────────────
@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path), raising=False)
    # Ensure no env-var/tradermap source bleeds into the computed/override tests.
    for k in (
        "LMEC_MACD_WEEKLY_POSITIVE", "LMEC_RSI_WEEKLY", "LMEC_MA50W_USD",
        "LMEC_MA50W_BROKEN_WEEKS", "TRADERMAP_BTC_MACD", "TRADERMAP_BTC_RSI",
        "TRADERMAP_BTC_MA50W",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LMEC_AUTOFEED_ENABLED", "false")  # silence weeks-counter net
    yield


def _market(btc_price):
    return {"prices": {"BTC": {"price_usd": float(btc_price)}}} if btc_price is not None else None


def _persist_computed(*, macd=None, rsi=None, ma50w=None, age_sec=0, source="binance:BTCUSDT@1w"):
    ts = (datetime.now(timezone.utc) - timedelta(seconds=age_sec)).isoformat()
    lmec_state.set_computed_inputs({
        "ok": True,
        "computed_ts_utc": ts,
        "weekly_close_ts_utc": "2026-06-14T23:59:59.999000+00:00",
        "source": source,
        "weekly_boundary": "Mon 00:00 UTC open / Sun 23:59:59.999 UTC close",
        "macd_weekly_positive": macd,
        "rsi_weekly": rsi,
        "ma50w_usd": ma50w,
        "n_closes": 119,
        "last_close": 65746.45,
    })


# ── 1. Pure compute on a fixed fixture ──────────────────────────────────
def _fixture():
    return [100.0 + 10 * math.sin(i / 3.0) + i * 0.5 for i in range(64)]


def test_compute_ma50w_exact():
    closes = [float(i) for i in range(1, 61)]  # last 50 = 11..60 → mean 35.5
    assert compute_close(closes) == 35.5


def compute_close(closes):
    return ind.compute_ma50w(closes)


def test_compute_indicators_fixture_within_tolerance():
    closes = _fixture()
    assert abs(ind.compute_ma50w(closes) - 119.53219) < 1e-3
    assert abs(ind.compute_rsi(closes) - 74.307818) < 1e-3
    macd = ind.compute_macd(closes)
    assert abs(macd["macd_line"] - 5.77452) < 1e-3
    assert macd["positive"] is True


def test_rsi_monotonic_edges():
    assert ind.compute_rsi([float(i) for i in range(1, 40)]) == 100.0   # all gains
    assert ind.compute_rsi([float(i) for i in range(40, 0, -1)]) == 0.0  # all losses


def test_insufficient_history_returns_none_never_zero():
    assert ind.compute_ma50w([1.0] * 49) is None          # needs 50
    assert ind.compute_rsi([1.0] * 5) is None              # needs 15
    assert ind.compute_macd([1.0] * 10) is None            # needs 35
    allnone = ind.compute_all([1.0, 2.0, 3.0])
    assert allnone["macd_weekly_positive"] is None
    assert allnone["rsi_weekly"] is None
    assert allnone["ma50w_usd"] is None


# ── 2. Computed values map to the correct leg statuses (logic unchanged) ─
def test_computed_macd_positive_maps_valida():
    from modules.lmec_triggers import evaluate_lmec_triggers
    _persist_computed(macd=True)
    res = evaluate_lmec_triggers(_market(80_000))
    leg = next(c for c in res["conditions"] if c["id"] == "macd_weekly_positive")
    assert leg["status"] == "VALIDA"
    assert leg["source"] == "computed"
    assert "[COMPUTED]" in leg["detail"]


def test_computed_macd_negative_maps_invalida():
    from modules.lmec_triggers import evaluate_lmec_triggers
    _persist_computed(macd=False)
    res = evaluate_lmec_triggers(_market(80_000))
    leg = next(c for c in res["conditions"] if c["id"] == "macd_weekly_positive")
    assert leg["status"] == "INVALIDA"
    assert leg["source"] == "computed"


def test_computed_rsi_above_70_valida_and_below_invalida():
    from modules.lmec_triggers import evaluate_lmec_triggers
    _persist_computed(rsi=75.0)
    res = evaluate_lmec_triggers(_market(80_000))
    leg = next(c for c in res["conditions"] if c["id"] == "rsi_weekly_above_70")
    assert leg["status"] == "VALIDA" and leg["source"] == "computed"

    _persist_computed(rsi=37.3)
    res = evaluate_lmec_triggers(_market(80_000))
    leg = next(c for c in res["conditions"] if c["id"] == "rsi_weekly_above_70")
    assert leg["status"] == "INVALIDA" and leg["source"] == "computed"


def test_computed_ma50w_leg_invalida_when_price_below(monkeypatch):
    from modules.lmec_triggers import evaluate_lmec_triggers
    # Live-style: BTC 65.7K below computed MA50w 91.8K → bear thesis intact.
    # weeks-broken supplied via env (orthogonal to the MA50w *value* source).
    monkeypatch.setenv("LMEC_MA50W_BROKEN_WEEKS", "0")
    _persist_computed(ma50w=91_806.82)
    res = evaluate_lmec_triggers(_market(65_746))
    leg = next(c for c in res["conditions"] if c["id"] == "ma50w_broken_sustained")
    assert leg["status"] == "INVALIDA"
    assert leg["source"] == "computed"
    assert "[COMPUTED]" in leg["detail"]


def test_computed_ma50w_leg_valida_when_sustained_above(monkeypatch):
    from modules.lmec_triggers import evaluate_lmec_triggers
    monkeypatch.setenv("LMEC_MA50W_BROKEN_WEEKS", "3")
    monkeypatch.setenv("LMEC_MA50W_SUSTAINED_WEEKS", "2")
    _persist_computed(ma50w=95_000.0)
    res = evaluate_lmec_triggers(_market(105_000))
    leg = next(c for c in res["conditions"] if c["id"] == "ma50w_broken_sustained")
    assert leg["status"] == "VALIDA"
    assert leg["source"] == "computed"


# ── 3. No repaint — in-progress week excluded by the fetch parser ───────
def _fake_httpx_get(payload):
    class _Resp:
        status_code = 200

        def json(self):
            return payload

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return _Resp()

    class _HX:
        @staticmethod
        def AsyncClient(*a, **k):
            return _Client()

    return _HX


def test_inprogress_week_excluded_no_repaint(monkeypatch):
    now_ms = int(time.time() * 1000)
    wk = 7 * 24 * 3600 * 1000
    # 60 CLOSED weekly rows + 1 in-progress (closeTime in the future).
    rows = []
    base_open = now_ms - 62 * wk
    for i in range(60):
        ot = base_open + i * wk
        rows.append([ot, "0", "0", "0", str(100.0 + i), "0", ot + wk - 1])
    # in-progress candle: closeTime in the future, wildly different close.
    ot = base_open + 60 * wk
    rows.append([ot, "0", "0", "0", "999999", "0", now_ms + wk])

    monkeypatch.setattr(ind, "_HTTPX_OK", True)
    monkeypatch.setattr(ind, "httpx", _fake_httpx_get(rows))

    data = asyncio.run(ind._fetch_binance_weekly())
    assert data is not None
    closes = data["closes"]
    assert len(closes) == 60                     # partial dropped
    assert closes[-1] == 159.0                   # last CLOSED close, not 999999
    assert 999999.0 not in closes
    # And the partial would have changed the MA if included → proves it matters.
    with_partial = closes + [999999.0]
    assert ind.compute_ma50w(closes) != ind.compute_ma50w(with_partial)


# ── 4. n/d path — no data → AWAITING_BCD, trigger neither fires nor clears
def test_nd_path_awaiting_and_no_trigger():
    from modules.lmec_triggers import evaluate_lmec_triggers
    # No computed snapshot persisted, no manual, env cleared.
    res = evaluate_lmec_triggers(_market(80_000))
    by_id = {c["id"]: c for c in res["conditions"]}
    for cid in ("macd_weekly_positive", "rsi_weekly_above_70", "ma50w_broken_sustained"):
        assert by_id[cid]["status"] == "AWAITING_BCD", cid
        assert by_id[cid]["status"] != "VALIDA"
    # n/d legs never count toward triggered.
    assert by_id["macd_weekly_positive"]["status"] not in ("VALIDA",)
    assert "n/d" in by_id["rsi_weekly_above_70"]["detail"]


def test_computed_partial_only_some_legs_nd():
    """If only RSI computes (MACD/MA50w None), the missing legs stay n/d while
    RSI fires — never fabricated for the missing ones."""
    from modules.lmec_triggers import evaluate_lmec_triggers
    _persist_computed(rsi=75.0, macd=None, ma50w=None)
    res = evaluate_lmec_triggers(_market(80_000))
    by_id = {c["id"]: c for c in res["conditions"]}
    assert by_id["rsi_weekly_above_70"]["status"] == "VALIDA"
    assert by_id["macd_weekly_positive"]["status"] == "AWAITING_BCD"
    assert by_id["ma50w_broken_sustained"]["status"] == "AWAITING_BCD"


# ── 5. Manual /setlmec override beats computed ──────────────────────────
def test_manual_override_beats_computed():
    from modules.lmec_triggers import evaluate_lmec_triggers
    _persist_computed(rsi=40.0)                       # computed → INVALIDA
    lmec_state.set_manual_input("rsi_weekly", 75.0)   # override → VALIDA
    res = evaluate_lmec_triggers(_market(80_000))
    leg = next(c for c in res["conditions"] if c["id"] == "rsi_weekly_above_70")
    assert leg["status"] == "VALIDA"
    assert leg["source"] == "override"
    assert "[OVERRIDE" in leg["detail"]


def test_clear_override_falls_back_to_computed():
    from modules.lmec_triggers import evaluate_lmec_triggers
    _persist_computed(rsi=40.0)
    lmec_state.set_manual_input("rsi_weekly", 75.0)
    lmec_state.set_manual_input("rsi_weekly", None)   # cleared → back to computed
    res = evaluate_lmec_triggers(_market(80_000))
    leg = next(c for c in res["conditions"] if c["id"] == "rsi_weekly_above_70")
    assert leg["source"] == "computed"
    assert leg["status"] == "INVALIDA"


# ── 6. Stale computed snapshot is ignored (never a stale prior value) ───
def test_stale_computed_snapshot_treated_as_nd():
    from modules.lmec_triggers import evaluate_lmec_triggers
    # 30 days old > default 10-day freshness window.
    _persist_computed(rsi=75.0, age_sec=30 * 24 * 3600)
    assert lmec_state.get_computed_inputs() == {}      # freshness guard drops it
    res = evaluate_lmec_triggers(_market(80_000))
    leg = next(c for c in res["conditions"] if c["id"] == "rsi_weekly_above_70")
    assert leg["status"] == "AWAITING_BCD"             # NOT VALIDA on stale data


def test_fresh_snapshot_used():
    _persist_computed(rsi=75.0, age_sec=3600)          # 1h old → fresh
    assert lmec_state.get_computed_inputs().get("rsi_weekly") == 75.0
