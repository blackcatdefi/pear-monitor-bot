"""R-SCREEN — universal short/long screener regression tests.

Covers the four invariants the round spec requires:

  (a) RANKING — a known-squeeze HIGH-z name ranks BELOW a clean 5/5 name
      (squeeze is the inviolable exclusion, mirrored in the ranking).
  (b) LONG FLAG — fires on an oversold + funding<0 mean-reverting fixture and
      does NOT fire on an over-extended one.
  (c) /check — correct short + long verdicts for both a squeeze case (z high but
      RSI>=70 + higher-highs blow-off → SHORT NO-GO) and a clean 5/5 case.
  (d) DATA — insufficient-data tokens are EXCLUDED from ranking, not scored.

Everything is offline: the five-gate math is exercised via the real
``evaluate_name_gates`` (pure), and ``compute_screen`` is driven with the
network functions monkeypatched so the ranking/exclusion wiring is deterministic.
The five gate definitions/thresholds are NOT touched — this only tests the
ranking + long-flag + per-token layer built on top of them.
"""
from __future__ import annotations

import random

import pytest

from modules.unlock_monitor import constants, evaluate_name_gates
from modules import universal_screener as s


# ─── Deterministic close-series fixtures (verified to produce the intended
#     gate outcomes — see the round's bring-up smoke test) ──────────────────
def _clean_overbought_meanreverting() -> list[float]:
    """Zig-zag (mean-reverting → low Hurst) ending ABOVE its mean but STALLING
    (last bar is not a higher-high → no blow-off). z>+1, Hurst<=0.47, no squeeze."""
    random.seed(1)
    closes = [100 + (6 if i % 2 == 0 else -6) + random.uniform(-1, 1) for i in range(42)]
    closes += [112, 110]  # above mean, last (110) < prior (112) = stalling, not HH
    return closes


def _squeeze_highz() -> list[float]:
    """Parabolic ramp into a blow-off: very high z AND RSI>=70 + higher-highs AND
    Hurst>=0.5 trending → the squeeze gate excludes it (un-shortable)."""
    return [100 + i * 0.8 for i in range(40)] + [140, 150, 165, 182]


def _oversold_meanreverting() -> list[float]:
    """Zig-zag (mean-reverting) pushed strongly OVERSOLD at the end, NOT a
    lower-low capitulation blow-off. z<=-1, Hurst<=0.47."""
    random.seed(7)
    closes = [100 + (8 if i % 2 == 0 else -8) + random.uniform(-1, 1) for i in range(42)]
    closes[-1] = 78
    closes[-2] = 77
    return closes


def _gate(ticker, closes, funding, *, streak=2):
    return evaluate_name_gates(ticker, "L1", closes, funding, constants(), z_streak_prev=streak)


# ─── (a) RANKING: squeeze high-z below a clean 5/5 ───────────────────────────
def test_ranking_squeeze_highz_below_clean_5of5():
    k = constants()
    clean = _gate("CLEAN", _clean_overbought_meanreverting(), 0.00001)
    squeeze = _gate("SQUEEZE", _squeeze_highz(), 0.00001)

    # Sanity on the fixtures themselves.
    assert clean.data_ok and s.short_pass_count(clean) == 5 and not clean.squeeze_flag
    assert squeeze.squeeze_flag and squeeze.z is not None and squeeze.z > clean.z  # higher z

    # The inviolable rule: despite a HIGHER z, the squeezing name scores LOWER.
    assert s.short_score(squeeze) < s.short_score(clean)

    # And when ranked together it lands strictly below.
    ranked = sorted([clean, squeeze], key=s.short_score, reverse=True)
    assert [g.ticker for g in ranked] == ["CLEAN", "SQUEEZE"]


def test_ranking_every_squeeze_below_every_nonsqueeze():
    """A squeezing name ranks below even a weak (low pass-count) non-squeezing
    name — squeeze is forced to the bottom band regardless of pass-count."""
    k = constants()
    squeeze = _gate("SQUEEZE", _squeeze_highz(), 0.00001)         # pc=3 but squeeze
    # A non-squeezing name that only passes data (z negative, funding fine):
    weak = _gate("WEAK", _oversold_meanreverting(), 0.00001, streak=0)
    assert squeeze.squeeze_flag and not weak.squeeze_flag
    assert s.short_score(weak) > s.short_score(squeeze)


# ─── (b) LONG FLAG: fires on oversold+funding<0, not on over-extended ────────
def test_long_flag_fires_on_oversold_funding_negative():
    k = constants()
    g = _gate("OVS", _oversold_meanreverting(), -0.00005)
    lower_lows = s.made_lower_lows(_oversold_meanreverting(), int(k["hh_lookback_bars"]))
    lr = s.long_read(g, lower_lows, k)
    assert g.z is not None and g.z <= -k["z_floor"]   # oversold
    assert g.hurst_ok                                  # mean-reverting
    assert g.funding is not None and g.funding <= 0    # shorts crowded
    assert lr.flag is True
    assert "LONG context" in lr.note


def test_long_flag_does_not_fire_on_overextended():
    k = constants()
    g = _gate("OVX", _clean_overbought_meanreverting(), 0.00001)
    lower_lows = s.made_lower_lows(_clean_overbought_meanreverting(), int(k["hh_lookback_bars"]))
    lr = s.long_read(g, lower_lows, k)
    assert g.z is not None and g.z > 0                 # over-extended UP, not oversold
    assert lr.flag is False


def test_long_flag_does_not_fire_when_funding_positive():
    """Oversold but funding>0 (no crowded shorts) → long not viable."""
    k = constants()
    g = _gate("OVP", _oversold_meanreverting(), 0.00050)  # funding POSITIVE
    lr = s.long_read(g, s.made_lower_lows(_oversold_meanreverting(), int(k["hh_lookback_bars"])), k)
    assert lr.flag is False
    assert "funding" in lr.note


# ─── (c) /check verdicts: squeeze case + clean case ──────────────────────────
def _row_for(ticker, closes, funding):
    g = _gate(ticker, closes, funding)
    return s.ScreenRow(
        ticker=ticker, sector="L1", venue_label="HL+VAR", liquidity_note="liq: HL",
        gate=g, data_ok=g.data_ok, pass_count=s.short_pass_count(g),
        score=s.short_score(g), short_verdict=s.short_verdict(g),
        long=s.long_read(g, s.made_lower_lows(closes, int(constants()["hh_lookback_bars"])), constants()),
        excluded_reason="",
    )


def test_check_squeeze_case_is_short_nogo():
    k = constants()
    row = _row_for("SQZ", _squeeze_highz(), 0.00001)
    text = s.format_check(row, "ok", "SQZ", k)
    assert "NO-GO" in text and "squeeze" in text.lower()
    # high RSI / blow-off cited; long not viable on an up-blow-off
    assert "RSI" in text
    assert "LONG: no viable" in text


def test_check_clean_case_is_5of5_go_candidate():
    k = constants()
    row = _row_for("CLN", _clean_overbought_meanreverting(), 0.00001)
    text = s.format_check(row, "ok", "CLN", k)
    assert "5/5 GO candidate" in text
    assert "AiPear" in text


def test_check_not_tradeable_message():
    text = s.format_check(None, "not_tradeable", "FOOBARZ")
    assert "no es tradeable" in text and "FOOBARZ" in text


# ─── (d) DATA-insufficient tokens excluded from ranking, not scored ──────────
def test_insufficient_data_token_excluded_not_scored():
    k = constants()
    # Too few candles → data-quality gate fails → data_ok False.
    thin = _gate("THIN", [100.0, 101.0, 99.0], 0.00001)
    assert thin.data_ok is False
    assert s.short_score(thin) == float("-inf")   # never scored


@pytest.mark.asyncio
async def test_compute_screen_excludes_thin_and_ranks_clean_above_squeeze(tmp_path, monkeypatch):
    """End-to-end (offline): a thin HL name and a VAR-only name land in the
    EXCLUDED bucket (not ranked); CLEAN ranks above SQUEEZE in the ranked bucket."""
    monkeypatch.setattr(s, "DB_PATH", str(tmp_path / "screener.db"))
    s._reset_for_tests()

    clean_series = _clean_overbought_meanreverting()
    squeeze_series = _squeeze_highz()
    btc_series = [100 + i * 0.1 for i in range(190)]

    venue_map = {
        "CLEAN": s.VenueInfo("CLEAN", True, True, 5e6, 1e6, 0.00001, 1000.0, 50.0),
        "SQUEEZE": s.VenueInfo("SQUEEZE", True, False, 4e6, None, 0.00001, 2000.0, None),
        "THINHL": s.VenueInfo("THINHL", True, False, 1e3, None, 0.00001, 5.0, None),
        "VARONLY": s.VenueInfo("VARONLY", False, True, None, 2e5, None, None, -300.0),
        "BTC": s.VenueInfo("BTC", True, True, 9e9, 9e8, 0.00001, 30000.0, 10.0),
    }

    async def fake_build_universe():
        return venue_map, []

    async def fake_fetch(coin, bars):
        coin = coin.upper()
        if coin == "CLEAN":
            return clean_series
        if coin == "SQUEEZE":
            return squeeze_series
        if coin == "BTC":
            return btc_series
        if coin == "THINHL":
            return [100.0, 101.0]          # too few → data-quality fail
        return None                         # VARONLY never fetched (on_hl False)

    monkeypatch.setattr(s, "build_universe", fake_build_universe)
    monkeypatch.setattr(s, "fetch_4h_closes", fake_fetch)
    # Pre-seed z-persistence so CLEAN can reach 5/5 (mirrors scheduler advance).
    s.save_screen_state("CLEAN", 2, 0.00001, 1000.0)
    s.save_screen_state("SQUEEZE", 2, 0.00001, 2000.0)

    res = await s.compute_screen(advance_state=False)

    ranked_tickers = [r.ticker for r in res.ranked]
    excluded_tickers = {r.ticker for r in res.excluded}

    # Thin HL + VAR-only are excluded, not ranked.
    assert "THINHL" in excluded_tickers and "VARONLY" in excluded_tickers
    assert "THINHL" not in ranked_tickers and "VARONLY" not in ranked_tickers
    # VAR-only reason is explicit.
    var_row = next(r for r in res.excluded if r.ticker == "VARONLY")
    assert "VAR-only" in var_row.excluded_reason

    # CLEAN (5/5) ranks strictly above SQUEEZE.
    assert "CLEAN" in ranked_tickers and "SQUEEZE" in ranked_tickers
    assert ranked_tickers.index("CLEAN") < ranked_tickers.index("SQUEEZE")

    # The clean name is a GO candidate; the squeeze name is NOT.
    clean_row = next(r for r in res.ranked if r.ticker == "CLEAN")
    squeeze_row = next(r for r in res.ranked if r.ticker == "SQUEEZE")
    assert clean_row.is_go_candidate is True
    assert squeeze_row.is_go_candidate is False


@pytest.mark.asyncio
async def test_check_single_not_tradeable(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "DB_PATH", str(tmp_path / "screener.db"))
    s._reset_for_tests()

    async def fake_build_universe():
        return {"BTC": s.VenueInfo("BTC", True, True, 9e9, 9e8, 0.00001, 30000.0, 10.0)}, []

    monkeypatch.setattr(s, "build_universe", fake_build_universe)
    row, status = await s.check_single("NOSUCHTOKEN")
    assert row is None and status == "not_tradeable"


# ─── (e) LEVERAGE NEUTRALITY: max-leverage NEVER gates, scores, or ranks ──────
# The fund decided max-leverage must not exclude or down-rank any asset. The
# five gates (data, z, Hurst, squeeze, funding) are the ONLY admission criteria.
# These guards fail loudly if anyone ever reintroduces a leverage filter — e.g.
# excluding 3x-capped Hyperliquid names (NOT, IO, GOAT, PNUT, POPCAT, VIRTUAL).
def test_leverage_is_not_an_input_to_scoring_or_passcount():
    """Structural guard: the score/pass-count functions take ONLY the gate verdict.
    There is no venue/leverage parameter, so max-leverage CANNOT influence either."""
    import inspect

    assert list(inspect.signature(s.short_score).parameters) == ["g"]
    assert list(inspect.signature(s.short_pass_count).parameters) == ["g"]
    # VenueInfo (the only place per-asset venue metadata lives) carries no leverage
    # field — there is nothing for a leverage filter to read.
    venue_fields = set(s.VenueInfo.__dataclass_fields__)
    assert not any("lev" in f.lower() for f in venue_fields)


@pytest.mark.parametrize("ticker", ["NOT", "IO", "GOAT", "PNUT", "POPCAT", "VIRTUAL"])
def test_3x_capped_name_passing_all_gates_is_5of5_go(ticker):
    """A 3x-leverage-capped name fed a clean-short series passes all five gates and
    is a 5/5 GO candidate — identical to any 5x+ name with the same metrics. Max
    leverage neither excludes it nor changes its pass-count or verdict."""
    g = _gate(ticker, _clean_overbought_meanreverting(), 0.00001)
    assert g.data_ok and not g.squeeze_flag
    assert s.short_pass_count(g) == 5
    assert s.short_verdict(g) == "SHORT: 5/5 GO candidate — confirmá con AiPear"
    # Its score equals a non-3x-capped name's score for the same metrics: the
    # 3x cap costs it nothing (no leverage term anywhere in the ranking).
    control = _gate("CTRL5X", _clean_overbought_meanreverting(), 0.00001)
    assert s.short_score(g) == s.short_score(control)


@pytest.mark.asyncio
async def test_compute_screen_ranks_3x_capped_names_as_go_not_excluded(tmp_path, monkeypatch):
    """End-to-end (offline): NOT and IO (both 3x-capped on Hyperliquid) fed clean
    data land in the RANKED bucket as GO candidates — never in the excluded bucket
    and never down-ranked below an otherwise-identical name for their leverage cap."""
    monkeypatch.setattr(s, "DB_PATH", str(tmp_path / "screener.db"))
    s._reset_for_tests()

    clean = _clean_overbought_meanreverting()
    venue_map = {
        # NOT / IO are 3x-capped HL perps; CTRL is the higher-leverage control.
        "NOT": s.VenueInfo("NOT", True, False, 5e6, None, 0.00001, 1000.0, None),
        "IO": s.VenueInfo("IO", True, False, 5e6, None, 0.00001, 1000.0, None),
        "CTRL": s.VenueInfo("CTRL", True, False, 5e6, None, 0.00001, 1000.0, None),
    }

    async def fake_build_universe():
        return venue_map, []

    async def fake_fetch(coin, bars):
        return clean  # identical clean-short series for every name

    monkeypatch.setattr(s, "build_universe", fake_build_universe)
    monkeypatch.setattr(s, "fetch_4h_closes", fake_fetch)
    for t in venue_map:
        s.save_screen_state(t, 2, 0.00001, 1000.0)  # seed z-persistence → 5/5 reachable

    res = await s.compute_screen(advance_state=False)
    ranked = {r.ticker: r for r in res.ranked}
    excluded = {r.ticker for r in res.excluded}

    for t in ("NOT", "IO"):
        assert t in ranked and t not in excluded
        assert ranked[t].pass_count == 5
        assert ranked[t].is_go_candidate is True
        # No leverage down-rank: a 3x-capped name scores exactly like the control.
        assert ranked[t].score == ranked["CTRL"].score
