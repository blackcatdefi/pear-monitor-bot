"""R-TELEMETRY — per-token telemetry command regression tests.

Covers the four properties the round spec requires, all OFFLINE (the HL info
endpoints are monkeypatched so the assembly + fallback wiring is deterministic):

  (a) TICKER PARSING — space/comma split, upper-case, ``$`` strip, dedup, 1-8
      cap, invalid-format drop.
  (b) METRIC ASSEMBLY — OI notional = openInterest×markPx, OI/vol ratio,
      distance-above-7d-low %, depth banding (±0.5%/±1.0% per side), funding
      hourly→APR + PAYS/RECEIVES short flag, fails-first gate order.
  (c) PER-METRIC n/d FALLBACK — a failure in ONE feed prints n/d for THAT
      metric only and never blanks the others, never fabricates/0-fills.
  (d) INJECTION SANITIZATION — every ticker is run through the SAME
      ``_sanitize_untrusted`` guard; role-marker / fenced payloads are defanged
      and then dropped by the strict charset, never surfaced as a "ticker".

The R-SCREEN 5-gate engine itself is NOT re-implemented — squeeze/fails-first/
z/Hurst come from ``check_single`` (monkeypatched here for determinism).
"""
from __future__ import annotations

import pytest

from modules import telemetry as tel


# ─── (a) Ticker parsing ──────────────────────────────────────────────────────
def test_parse_space_and_comma_upper_dollar_dedup():
    tickers, notes = tel.parse_tickers(["btc", "$hype,wld", "BTC"])
    assert tickers == ["BTC", "HYPE", "WLD"]  # upper, $-strip, comma-split, dedup
    assert notes == []


def test_parse_single_string_input():
    tickers, _ = tel.parse_tickers("eth, sol  arb")
    assert tickers == ["ETH", "SOL", "ARB"]


def test_parse_caps_at_eight_with_note():
    raw = "a b c d e f g h i j"  # 10 valid one-letter tickers
    tickers, notes = tel.parse_tickers(raw)
    assert len(tickers) == tel.MAX_TICKERS == 8
    assert any("máx" in n for n in notes)


def test_parse_empty_returns_empty():
    assert tel.parse_tickers([]) == ([], [])
    assert tel.parse_tickers("") == ([], [])
    assert tel.parse_tickers(None) == ([], [])


def test_parse_drops_invalid_format():
    tickers, notes = tel.parse_tickers(["BTC", "bad-sym!", "a.b", "HYPE"])
    assert tickers == ["BTC", "HYPE"]
    assert any("inválido" in n for n in notes)


# ─── (d) Injection sanitization ──────────────────────────────────────────────
def test_injection_role_marker_is_sanitized_and_dropped():
    # "system:" is a defanged role marker → becomes "[redacted-injection]…" →
    # fails the [A-Z0-9] charset → dropped. The payload never becomes a ticker.
    tickers, _ = tel.parse_tickers(["system:DROP", "BTC"])
    assert tickers == ["BTC"]
    assert "DROP" not in "".join(tickers)
    assert all("SYSTEM" != t for t in tickers)


def test_injection_fenced_payload_dropped():
    tickers, _ = tel.parse_tickers(["<|im_start|>", "HYPE"])
    assert tickers == ["HYPE"]


def test_injection_control_chars_stripped_not_crashing():
    # C0 control chars are stripped by the sanitizer; the clean residue (if
    # upper-alnum) survives, otherwise it is dropped — never raises.
    tickers, _ = tel.parse_tickers(["BTC\x07", "HY\x00PE"])
    assert "BTC" in tickers


def test_sanitizer_is_actually_invoked(monkeypatch):
    """Guard against someone bypassing the sanitizer: assert parse_tickers routes
    every chunk through _sanitize_untrusted."""
    seen: list[str] = []
    real = tel._sanitize_untrusted

    def _spy(text, **kw):
        seen.append(str(text))
        return real(text, **kw)

    monkeypatch.setattr(tel, "_sanitize_untrusted", _spy)
    tel.parse_tickers(["BTC", "HYPE"])
    assert "BTC" in seen and "HYPE" in seen


# ─── (b) Metric assembly — funding flag + formatting helpers ─────────────────
def test_short_funding_flag():
    assert tel._short_funding_flag(0.0001) == "RECEIVES (short)"
    assert tel._short_funding_flag(-0.0001) == "PAYS (short)"
    assert tel._short_funding_flag(0.0) == "FLAT"
    assert tel._short_funding_flag(None) == "n/d"


def test_annualization_is_hourly_times_8760():
    # 0.00001 hourly → ×24×365 ×100 = +8.76% APR
    assert tel._ann(0.00001) == "+8.8%"
    assert tel._ann(None) == "n/d"


def test_fails_first_gate_order():
    class G:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    allok = dict(data_ok=True, z_ok=True, hurst_ok=True, squeeze_flag=False, funding_ok=True)
    assert tel._fails_first(G(**allok)) == "none — 5/5 GO"
    assert tel._fails_first(G(**{**allok, "data_ok": False})) == "data"
    assert tel._fails_first(G(**{**allok, "z_ok": False})) == "z"
    assert tel._fails_first(G(**{**allok, "hurst_ok": False})) == "Hurst"
    assert tel._fails_first(G(**{**allok, "squeeze_flag": True})) == "squeeze"
    assert tel._fails_first(G(**{**allok, "funding_ok": False})) == "funding"
    assert tel._fails_first(None) == "n/d"


# ─── (b) Metric assembly — full build_one with patched feeds ─────────────────
@pytest.fixture
def _patch_feeds(monkeypatch):
    async def _avg(coin):
        return 0.00001, 168

    async def _low(coin):
        return 90000.0

    async def _depth(coin):
        return {"bid_05": 1_000_000.0, "ask_05": 800_000.0,
                "bid_10": 2_500_000.0, "ask_10": 2_100_000.0}

    async def _gate(coin):
        return {"squeeze_state": "CLEAR", "fails_first": "none — 5/5 GO",
                "z": 1.5, "hurst": 0.40, "venue_label": "HL"}

    monkeypatch.setattr(tel, "fetch_funding_avg_7d", _avg)
    monkeypatch.setattr(tel, "fetch_low_7d", _low)
    monkeypatch.setattr(tel, "fetch_depth", _depth)
    monkeypatch.setattr(tel, "fetch_gate", _gate)


async def test_build_one_full_metrics(_patch_feeds):
    ctx_map = {"BTC": {"funding": 0.0000125, "openInterest": 1000.0,
                       "markPx": 100000.0, "dayNtlVlm": 500_000_000.0}}
    t = await tel.build_one("BTC", ctx_map)
    assert t.on_hl is True
    assert t.funding_live == 0.0000125
    assert t.funding_avg7d == 0.00001 and t.funding_samples == 168
    assert t.oi_usd == pytest.approx(1000.0 * 100000.0)        # 1e8
    assert t.vol24h_usd == 500_000_000.0
    assert t.oi_vol_ratio == pytest.approx(1e8 / 5e8)          # 0.2
    assert t.low7d == 90000.0 and t.mark == 100000.0
    assert t.dist_low_pct == pytest.approx((100000 - 90000) / 90000 * 100)
    assert t.bid_05 == 1_000_000.0 and t.ask_10 == 2_100_000.0
    assert t.squeeze_state == "CLEAR" and t.fails_first == "none — 5/5 GO"
    assert t.z == 1.5 and t.hurst == 0.40
    # rendering must not crash and must include the ticker header
    block = tel.format_token(t)
    assert "BTC" in block and "RECEIVES (short)" in block


# ─── (c) Per-metric n/d fallback ─────────────────────────────────────────────
async def test_ndfallback_only_failing_metric(monkeypatch):
    # funding-7d feed fails, low feed fails, depth fails, gate fails — but the
    # ctx-derived metrics (funding live, OI, vol) must STILL render.
    async def _avg(coin):
        return None, 0

    async def _low(coin):
        return None

    async def _depth(coin):
        return {"bid_05": None, "ask_05": None, "bid_10": None, "ask_10": None}

    async def _gate(coin):
        return {"squeeze_state": None, "fails_first": None,
                "z": None, "hurst": None, "venue_label": None}

    monkeypatch.setattr(tel, "fetch_funding_avg_7d", _avg)
    monkeypatch.setattr(tel, "fetch_low_7d", _low)
    monkeypatch.setattr(tel, "fetch_depth", _depth)
    monkeypatch.setattr(tel, "fetch_gate", _gate)

    ctx_map = {"HYPE": {"funding": -0.0002, "openInterest": 50.0,
                        "markPx": 40.0, "dayNtlVlm": 10_000_000.0}}
    t = await tel.build_one("HYPE", ctx_map)
    # surviving (ctx-derived) metrics
    assert t.funding_live == -0.0002
    assert t.oi_usd == pytest.approx(50.0 * 40.0)
    assert t.oi_vol_ratio == pytest.approx(2000.0 / 10_000_000.0)
    # failed feeds → n/d (None), never fabricated
    assert t.funding_avg7d is None and t.funding_samples == 0
    assert t.low7d is None and t.dist_low_pct is None
    assert t.bid_05 is None and t.ask_10 is None
    assert t.squeeze_state is None and t.z is None
    block = tel.format_token(t)
    assert "n/d" in block and "PAYS (short)" in block  # live funding<0 short pays


async def test_build_one_ticker_absent_from_hl(monkeypatch):
    # ticker not present in metaAndAssetCtxs → all HL ctx metrics n/d, no crash.
    async def _avg(coin):
        return None, 0

    async def _low(coin):
        return None

    async def _depth(coin):
        return {"bid_05": None, "ask_05": None, "bid_10": None, "ask_10": None}

    async def _gate(coin):
        return {"squeeze_state": None, "fails_first": "no tradeable (HL/VAR)",
                "z": None, "hurst": None, "venue_label": None}

    monkeypatch.setattr(tel, "fetch_funding_avg_7d", _avg)
    monkeypatch.setattr(tel, "fetch_low_7d", _low)
    monkeypatch.setattr(tel, "fetch_depth", _depth)
    monkeypatch.setattr(tel, "fetch_gate", _gate)

    t = await tel.build_one("NOTACOIN", {})
    assert t.on_hl is False
    assert t.funding_live is None and t.oi_usd is None and t.mark is None
    assert "n/d" in tel.format_token(t)


# ─── (b) Depth banding + funding mean — real parsing via patched _hl_post ─────
async def test_fetch_depth_banding(monkeypatch):
    # mid = 100. bids at 99.6 (in 0.5% band, ≥99.5) and 99.0 (in 1% band, ≥99).
    # asks at 100.4 (in 0.5%) and 101.0 (in 1%, ≤101).
    book = {"levels": [
        [{"px": "99.6", "sz": "10"}, {"px": "99.0", "sz": "5"}, {"px": "98.0", "sz": "100"}],
        [{"px": "100.4", "sz": "20"}, {"px": "101.0", "sz": "5"}, {"px": "103.0", "sz": "100"}],
    ]}

    async def _post(payload, **kw):
        assert payload["type"] == "l2Book"
        return book

    monkeypatch.setattr(tel, "_hl_post", _post)
    d = await tel.fetch_depth("BTC")
    # best bid 99.6, best ask 100.4 → mid 100.0
    assert d["bid_05"] == pytest.approx(99.6 * 10)                 # only 99.6 within 0.5%
    assert d["bid_10"] == pytest.approx(99.6 * 10 + 99.0 * 5)      # 99.6 + 99.0 within 1%
    assert d["ask_05"] == pytest.approx(100.4 * 20)
    assert d["ask_10"] == pytest.approx(100.4 * 20 + 101.0 * 5)


async def test_fetch_depth_failure_returns_all_none(monkeypatch):
    async def _boom(payload, **kw):
        raise RuntimeError("429")

    monkeypatch.setattr(tel, "_hl_post", _boom)
    d = await tel.fetch_depth("BTC")
    assert d == {"bid_05": None, "ask_05": None, "bid_10": None, "ask_10": None}


async def test_fetch_funding_avg_mean(monkeypatch):
    rows = [{"fundingRate": "0.00001"}, {"fundingRate": "0.00003"}, {"fundingRate": "bad"}]

    async def _post(payload, **kw):
        assert payload["type"] == "fundingHistory"
        return rows

    monkeypatch.setattr(tel, "_hl_post", _post)
    avg, n = await tel.fetch_funding_avg_7d("BTC")
    assert n == 2  # the non-numeric row is excluded, never 0-filled
    assert avg == pytest.approx((0.00001 + 0.00003) / 2)


async def test_fetch_funding_avg_empty_is_nd(monkeypatch):
    async def _post(payload, **kw):
        return []

    monkeypatch.setattr(tel, "_hl_post", _post)
    assert await tel.fetch_funding_avg_7d("BTC") == (None, 0)


# ─── Full render integration (timestamp header + grouped blocks) ─────────────
async def test_format_telemetry_groups_and_timestamps(_patch_feeds, monkeypatch):
    async def _ctx():
        return {"BTC": {"funding": 0.0000125, "openInterest": 1000.0,
                        "markPx": 100000.0, "dayNtlVlm": 500_000_000.0}}

    monkeypatch.setattr(tel, "fetch_ctx_map", _ctx)
    tokens = await tel.build_telemetry(["BTC"])
    out = tel.format_telemetry(tokens, ["nota de prueba"])
    assert "TELEMETRY" in out and "UTC" in out and "BTC" in out
    assert "nota de prueba" in out
