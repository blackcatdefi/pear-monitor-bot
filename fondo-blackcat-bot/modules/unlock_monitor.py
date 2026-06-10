"""R-UNLOCK — basket-entry-unlock regime monitor (R-UNLOCK-PRECISION, 2026-06-01).

WHAT THIS IS — A HIGH-PRECISION PRE-FILTER, NOT A SELECTOR
    Fondo Black Cat runs DIRECTIONAL relative-value SHORT legs against the HYPE
    long (V11 book). The manual entry screen is a SQUEEZE-FIRST 5-check on each
    candidate short:

        1) squeeze / momentum CLEAR (not being squeezed, not accelerating up),
        2) positive 4h z-score ABOVE a magnitude floor (overbought vs own mean),
        3) Hurst < 0.5 (mean-reverting, not trending),
        4) funding >= 0 (longs paying — short side not crowded),
        5) Bollinger / overbought posture.

    This monitor is the FIRST STAGE of a pipeline:

        bot UNLOCK alert  ->  human confirms with AiPear (full 5/5)  ->  execute

    The bot NEVER selects the final tokens, NEVER sizes, and NEVER auto-executes.
    It only flags "enough names are simultaneously clearing the squeeze-first
    screen — re-screen now with AiPear."

CONSERVATIVE BIAS — PREFER FALSE NEGATIVES
    The book that previously fired a hard UNLOCK on BNB/XLM/HBAR/WLD was wrong:
    three of those four FAIL the real screen (BNB RSI 71 + short-squeeze, HBAR
    z +0.39 noise, XLM trending on a DTCC catalyst). Root causes: (a) the trigger
    trusted a cointegration PROXY that is NOT the fund's filter, (b) no Hurst
    gate, (c) no squeeze/momentum gate and no z magnitude floor.

    This module re-architects the trigger so the alert is trustworthy enough to
    hand straight to a confirmation step. When ANY input is uncertain, degraded,
    or borderline, the name does NOT count toward the trigger. We strongly prefer
    to MISS a setup over to FIRE on a bad one.

THE FIVE SUB-GATES (a watchlist name COUNTS toward UNLOCK only if ALL pass)
    1) DATA-QUALITY — >= DATA_MIN_COVERAGE of the z/Hurst lookback window must be
       real 4h candles. Degraded/missing/short -> EXCLUDED, "data insufficient".
    2) Z-SCORE      — 4h z >= Z_FLOOR (default +1.00) AND persistent for
       >= Z_PERSIST_READINGS cron cycles (rejects single-bar transients & noise).
    3) HURST        — estimated 4h Hurst (rescaled-range) <= 0.50 - HURST_MARGIN
       (default <= 0.47). Mean-reverting only; borderline-trending excluded.
    4) SQUEEZE/MOMENTUM GUARD (inviolable, multi-signal) — EXCLUDE if Hurst>=0.50,
       OR overbought-AND-RISING (RSI>=OVERBOUGHT_RSI WHILE higher highs = blow-off),
       OR parabolic ramp (>= PARABOLIC_PCT over K bars), OR OI-spike + funding
       ramp (crowded long). We want overbought-AND-STALLING (reversion), NOT
       overbought-AND-ACCELERATING (squeeze).
    5) FUNDING      — funding >= FUNDING_MIN (>=0) re-pulled at evaluation time.
       Crowded-negative funding is excluded (the inviolable squeeze rule).

    COINTEGRATION is CONTEXT ONLY. The current book is directional vs the HYPE
    long, not market-neutral, so cointegration is NOT a hard gate. The rolling
    Pearson-correlation proxy is DISPLAYED (labelled "proxy, not a gate") but
    NEVER affects whether a name counts.

UNLOCK TRIGGER
    Hard UNLOCK fires ONLY when >= NAMES_REQUIRED names pass ALL five sub-gates
    simultaneously, AND those names span >= MIN_SECTORS distinct narratives (so
    the set is not one repeated bet), AND that condition holds for
    >= UNLOCK_PERSIST_READINGS consecutive readings (debounce). If >= required
    pass gates but cluster in < MIN_SECTORS sectors (or have not persisted), the
    level stays APPROACHING and notes the concentration. Edge-triggered +
    R-SILENT-aware + SQLite state: silent reset on retreat, UNLOCK breaks
    silence, re-arm only after a genuine drop-then-recross.

HONESTY STANDARD
    Real Engle-Granger cointegration (ADF on the regression residual) needs a
    stats stack we don't ship; Hurst and z are ESTIMATED from 4h bars; ASI is
    frequently not fetchable; per-name short-liquidation feeds are not keyless.
    Every UNLOCK alert states it is a proxy-based PRE-FILTER requiring AiPear
    confirmation and surfaces per-name data confidence. We NEVER fabricate a pass
    and NEVER crash the scheduler: any data gap degrades that leg and pulls a
    confidence note — it can never silently manufacture an UNLOCK.

DATA SOURCES (all keyless, read-only, no custody, no keys)
    * 4h closes / OHLC: Hyperliquid `candleSnapshot` (interval 4h).
    * Funding sign + open interest: Hyperliquid `metaAndAssetCtxs`.
    * BTC dominance: CoinGecko `/global` (market_cap_percentage.btc).
    * Altcoin Season Index: best-effort; degrades to "n/d (estimado)" — context
      only, never standalone.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

try:
    import httpx
    _HTTPX_OK = True
except Exception:  # noqa: BLE001 — stay import-safe even without httpx
    _HTTPX_OK = False

log = logging.getLogger(__name__)

# ─── Persistent DATA_DIR (Railway Volume at /app/data in prod) ────────────────
try:
    from config import DATA_DIR  # type: ignore
except Exception:  # noqa: BLE001
    DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
    os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "intel_memory.db")

# ─── Level ladder (NONE < WATCH < APPROACHING < UNLOCK) ───────────────────────
NONE = "NONE"
WATCH = "WATCH"
APPROACHING = "APPROACHING"
UNLOCK = "UNLOCK"
_LEVEL_RANK = {NONE: 0, WATCH: 1, APPROACHING: 2, UNLOCK: 3}
_LEVEL_EMOJI = {NONE: "🟢", WATCH: "🟡", APPROACHING: "🟠", UNLOCK: "🔴"}

# ─── Hyperliquid keyless info endpoint ───────────────────────────────────────
HL_INFO_URL = os.getenv("HL_INFO_URL", "https://api.hyperliquid.xyz/info").rstrip("/")
COINGECKO_GLOBAL_URL = os.getenv(
    "UNLOCK_COINGECKO_GLOBAL_URL", "https://api.coingecko.com/api/v3/global"
)
ASI_URL = os.getenv(
    "UNLOCK_ASI_URL",
    "https://www.blockchaincenter.net/api/altcoin-season-index/",
)
_HTTP_TIMEOUT_S = 15.0
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

BTC_COIN = os.getenv("UNLOCK_BTC_COIN", "BTC").strip().upper()


# ─── Env-tunable constants (read live so Railway overrides take effect) ──────
def _envf(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("bad %s=%r → %s", name, raw, default)
        return default


def _envi(name: str, default: int) -> int:
    return int(_envf(name, float(default)))


# Default watchlist — the 11 seed names from the round spec. These are the
# liquid majors that are the natural candidate set for a directional short leg;
# configurable via UNLOCK_WATCHLIST (comma list of HL tickers).
DEFAULT_WATCHLIST = "MORPHO,BNB,XLM,HBAR,ALGO,UNI,MKR,NEAR,INJ,TAO,WLD"


def watchlist() -> list[str]:
    raw = os.getenv("UNLOCK_WATCHLIST", DEFAULT_WATCHLIST)
    names = [p.strip().upper() for p in raw.split(",") if p.strip()]
    # de-dupe, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def constants() -> dict[str, float]:
    """Snapshot of every R-UNLOCK-PRECISION threshold (env-overridable).

    Defaults baked in so NO new Railway env var is required to ship; each can be
    overridden live. Reasoning per default:

      DATA-QUALITY GATE
        z_lookback_bars 42        ~7d of 4h bars — long enough to define a mean,
                                  short enough to react to a regime turn. This is
                                  the lookback the coverage requirement is measured
                                  against.
        data_min_coverage 0.90    a name needs >=90% real candles in that window
                                  to be eval'd; below that its metrics rest on
                                  degraded data and it is EXCLUDED (never counted).
        hurst_min_returns 16      floor of 4h log-returns for a 2-point R/S Hurst
                                  regression; below it Hurst is "unknown" -> NO.

      Z-SCORE GATE
        z_floor 1.00              positive 4h z must clear +1.00 (RAISED from the
                                  old +0.75 cutoff that let HBAR +0.39-class noise
                                  through). +1.0σ == genuinely overbought vs mean.
        z_persist_readings 2      z must hold >= floor for >=2 cron cycles, so a
                                  single transient 4h bar does not arm a name.

      HURST GATE
        hurst_max 0.50            < 0.5 == mean-reverting (the screen's req).
        hurst_margin 0.03         uncertainty buffer: count only if Hurst<=0.47,
                                  so borderline-trending names (XLM/BNB-style,
                                  Hurst ~0.57-0.60) do NOT pass.

      SQUEEZE / MOMENTUM GUARD (inviolable)
        overbought_rsi 70         RSI(14) 4h at/above this is overbought; combined
                                  with higher-highs == blow-off (accelerating),
                                  which we EXCLUDE. Overbought WITHOUT higher-highs
                                  (stalling/rolling) is the reversion case we want.
        hh_lookback_bars 6        ~24h on 4h — window for the higher-highs check.
        parabolic_pct 25.0        a >=+25% ramp over the last 6 4h bars is a
                                  vertical move, not an exhaustion top -> EXCLUDE.
        oi_spike_pct 0.15         open-interest up >=15% reading-over-reading...
        funding_ramp_delta 5e-06  ...WHILE funding rises this fast == crowded-long
                                  momentum (squeeze risk) -> EXCLUDE. Both halves
                                  required; absent data never fabricates a squeeze.

      FUNDING GATE
        funding_min 0.0           funding must be >= 0 (longs paying). Crowded-
                                  negative funding is the inviolable squeeze rule.

      TRIGGER
        names_required 4          >=4 names clearing ALL 5 gates simultaneously.
        names_approaching 2       2-3 names clearing all gates -> APPROACHING.
        min_sectors 3             the >=4 set must span >=3 narratives, so the
                                  basket is not one repeated bet; else APPROACHING.
        unlock_persist_readings 2 the >=4-AND->=3-sector condition must hold for
                                  >=2 readings before a hard UNLOCK (hysteresis).

      CONDITION-A CONTEXT (BTC stabilization — feeds WATCH/APPROACHING only)
        btc_z_deep -1.0 / btc_z_recover -0.5 / corr_lookback_bars 180 /
        coint_threshold 0.6 (PROXY, context only) / vol_compression_readings 3 /
        btc_band_pct 5.0 / btc_band_bars 15.
    """
    return {
        # data-quality
        "z_lookback_bars": float(_envi("UNLOCK_Z_LOOKBACK_BARS", 42)),
        "data_min_coverage": _envf("UNLOCK_DATA_MIN_COVERAGE", 0.90),
        "hurst_min_returns": float(_envi("UNLOCK_HURST_MIN_RETURNS", 16)),
        # z-score gate
        "z_floor": _envf("UNLOCK_Z_FLOOR", 1.00),
        "z_persist_readings": float(_envi("UNLOCK_Z_PERSIST_READINGS", 2)),
        # hurst gate
        "hurst_max": _envf("UNLOCK_HURST_MAX", 0.50),
        "hurst_margin": _envf("UNLOCK_HURST_MARGIN", 0.03),
        # squeeze / momentum guard
        "overbought_rsi": _envf("UNLOCK_OVERBOUGHT_RSI", 70.0),
        "hh_lookback_bars": float(_envi("UNLOCK_HH_LOOKBACK_BARS", 6)),
        "parabolic_pct": _envf("UNLOCK_PARABOLIC_PCT", 25.0),
        "oi_spike_pct": _envf("UNLOCK_OI_SPIKE_PCT", 0.15),
        "funding_ramp_delta": _envf("UNLOCK_FUNDING_RAMP_DELTA", 5e-06),
        # funding gate
        "funding_min": _envf("UNLOCK_FUNDING_MIN", 0.0),
        # trigger
        "names_required": float(_envi("UNLOCK_NAMES_REQUIRED", 4)),
        "names_approaching": float(_envi("UNLOCK_NAMES_APPROACHING", 2)),
        "min_sectors": float(_envi("UNLOCK_MIN_SECTORS", 3)),
        "unlock_persist_readings": float(_envi("UNLOCK_UNLOCK_PERSIST_READINGS", 2)),
        # condition-A context (BTC stabilization)
        "btc_z_deep": _envf("UNLOCK_BTC_Z_DEEP", -1.0),
        "btc_z_recover": _envf("UNLOCK_BTC_Z_RECOVER", -0.5),
        "corr_lookback_bars": float(_envi("UNLOCK_CORR_LOOKBACK_BARS", 180)),
        "coint_threshold": _envf("UNLOCK_COINT_THRESHOLD", 0.6),
        "vol_compression_readings": float(_envi("UNLOCK_VOL_COMPRESSION_READINGS", 3)),
        "btc_band_pct": _envf("UNLOCK_BTC_BAND_PCT", 5.0),
        "btc_band_bars": float(_envi("UNLOCK_BTC_BAND_BARS", 15)),
    }


def hurst_count_cutoff(k: dict[str, float]) -> float:
    """Effective Hurst ceiling a name must be at/below to COUNT (<=0.47 default)."""
    return k["hurst_max"] - k["hurst_margin"]


def alert_breaks_silence_level() -> str:
    """Minimum level that is allowed to BREAK R-SILENT (default UNLOCK).

    Below this level, alerts are suppressed while silent mode is on. State is
    still advanced silently so transitions keep tracking.
    """
    lv = os.getenv("UNLOCK_ALERT_BREAKS_SILENCE_LEVEL", UNLOCK).strip().upper()
    return lv if lv in _LEVEL_RANK else UNLOCK


# ─── Pure math (unit-tested, no network) ─────────────────────────────────────
def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x or x in (float("inf"), float("-inf")):
        return None
    return x


def zscore(closes: list[float], lookback: int) -> Optional[float]:
    """Mean-reversion z of the LAST close vs its rolling SMA over ``lookback``.

    z = (last - mean) / stdev.  None when there is not enough data or the
    window is flat (stdev 0). Negative z = below mean, positive = above mean.
    """
    if not closes:
        return None
    window = [c for c in closes[-int(lookback):] if c is not None]
    if len(window) < 3:
        return None
    mean = sum(window) / len(window)
    var = sum((c - mean) ** 2 for c in window) / len(window)
    sd = math.sqrt(var)
    if sd == 0:
        return None
    return (window[-1] - mean) / sd


def log_returns(closes: list[float]) -> list[float]:
    out: list[float] = []
    prev: Optional[float] = None
    for c in closes:
        c = _f(c)
        if c is None or c <= 0:
            prev = None
            continue
        if prev is not None and prev > 0:
            out.append(math.log(c / prev))
        prev = c
    return out


def pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    """Pearson correlation of two equal-length series. None if undefined."""
    n = min(len(xs), len(ys))
    if n < 3:
        return None
    xs = xs[-n:]
    ys = ys[-n:]
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def rolling_corr_vs_btc(
    alt_closes: list[float], btc_closes: list[float], lookback: int
) -> Optional[float]:
    """CONTEXT-ONLY proxy: rolling Pearson corr of 4h log-returns vs BTC over
    ``lookback`` bars. Displayed, labelled "proxy, not a gate"; NEVER affects
    whether a name counts toward UNLOCK."""
    a = log_returns(alt_closes)
    b = log_returns(btc_closes)
    n = min(len(a), len(b), int(lookback))
    if n < 3:
        return None
    return pearson(a[-n:], b[-n:])


def corr_is_repairing(
    alt_closes: list[float], btc_closes: list[float], lookback: int
) -> Optional[bool]:
    """CONTEXT-ONLY: True when recent-half correlation >= older-half (repairing
    toward cointegration), False when deteriorating, None when undeterminable."""
    a = log_returns(alt_closes)
    b = log_returns(btc_closes)
    n = min(len(a), len(b), int(lookback))
    if n < 8:
        return None
    a = a[-n:]
    b = b[-n:]
    half = n // 2
    old = pearson(a[:half], b[:half])
    rec = pearson(a[half:], b[half:])
    if old is None or rec is None:
        return None
    return rec >= old


def rsi(closes: list[float], period: int = 14) -> Optional[float]:
    """Wilder-style RSI (simple-average variant) of 4h closes. None if short."""
    period = max(2, int(period))
    cl = [c for c in closes if _f(c) is not None]
    if len(cl) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(cl)):
        d = cl[i] - cl[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def hurst_rs(returns: list[float], min_returns: int = 16) -> Optional[float]:
    """Estimate the Hurst exponent of a return series via RESCALED-RANGE (R/S)
    analysis. H<0.5 mean-reverting, H~0.5 random walk, H>0.5 trending/persistent.

    Chunk the series at sizes 8,16,32,... ; for each size average R/S across the
    non-overlapping chunks; regress log(R/S) on log(size); the slope is H. None
    when there is not enough data for a >=2-point regression (never fabricated)."""
    rets = [r for r in returns if _f(r) is not None]
    n = len(rets)
    if n < int(min_returns) or n < 16:
        return None
    sizes: list[int] = []
    s = 8
    while s <= n:
        sizes.append(s)
        s *= 2
    if len(sizes) < 2:
        return None
    xs: list[float] = []
    ys: list[float] = []
    for size in sizes:
        rs_vals: list[float] = []
        for start in range(0, n - size + 1, size):
            chunk = rets[start:start + size]
            m = sum(chunk) / size
            # cumulative deviation from the chunk mean
            cum = 0.0
            dev: list[float] = []
            for c in chunk:
                cum += (c - m)
                dev.append(cum)
            rng = max(dev) - min(dev)
            sd = math.sqrt(sum((c - m) ** 2 for c in chunk) / size)
            if sd > 0 and rng > 0:
                rs_vals.append(rng / sd)
        if rs_vals:
            xs.append(math.log(size))
            ys.append(math.log(sum(rs_vals) / len(rs_vals)))
    if len(xs) < 2:
        return None
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return num / den


def pct_change_last_k(closes: list[float], k: int) -> Optional[float]:
    """Percent change of the last close vs the close ``k`` bars ago."""
    cl = [c for c in closes if _f(c) is not None]
    k = int(k)
    if len(cl) < k + 1 or cl[-1 - k] <= 0:
        return None
    return (cl[-1] - cl[-1 - k]) / cl[-1 - k] * 100.0


def made_higher_highs(closes: list[float], k: int) -> Optional[bool]:
    """True when the last close is the highest of the last ``k+1`` bars (still
    making higher highs = rising). None when too short."""
    cl = [c for c in closes if _f(c) is not None]
    k = int(k)
    if len(cl) < k + 1:
        return None
    return cl[-1] >= max(cl[-1 - k:-1])


def coverage_fraction(n_received: int, lookback: int) -> float:
    """Fraction of the z/Hurst lookback window that is covered by real candles."""
    lookback = int(lookback)
    if lookback <= 0:
        return 0.0
    return min(int(n_received), lookback) / float(lookback)


def realized_vol(closes: list[float], window: int) -> Optional[float]:
    """Stdev of log-returns over the last ``window`` bars (realized vol proxy)."""
    rets = log_returns(closes[-(int(window) + 1):])
    if len(rets) < 3:
        return None
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / len(rets)
    return math.sqrt(var)


def band_hold(closes: list[float], pct: float, bars: int) -> Optional[bool]:
    """True when BTC stayed within a ``pct``% band over the last ``bars`` bars."""
    window = [c for c in closes[-int(bars):] if _f(c) is not None]
    if len(window) < max(3, int(bars) // 2):
        return None
    lo, hi = min(window), max(window)
    if lo <= 0:
        return None
    return ((hi - lo) / lo) * 100.0 <= pct


def series_is_contracting(series: list[float], readings: int) -> Optional[bool]:
    """True when the last ``readings`` values are monotonically non-increasing
    (each <= the previous) — N consecutive realized-vol contractions."""
    vals = [v for v in series if v is not None]
    if len(vals) < int(readings) or int(readings) < 2:
        return None
    tail = vals[-int(readings):]
    return all(tail[i] <= tail[i - 1] for i in range(1, len(tail)))


def btcd_rolling_over(series: list[float]) -> Optional[bool]:
    """Lower-high then lower-low check on BTC.D history (>=4 points)."""
    vals = [v for v in series if v is not None]
    if len(vals) < 4:
        return None
    half = len(vals) // 2
    old, rec = vals[:half], vals[half:]
    if not old or not rec:
        return None
    return (max(rec) < max(old)) and (min(rec) < min(old))


# ─── Condition-A context classifier (BTC stabilization → WATCH/APPROACHING) ──
@dataclass
class BtcStab:
    z: Optional[float]
    z_prev_deep: bool          # was z below "deep" on a prior reading
    z_recovered: bool          # z now >= recover threshold
    vol_compressing: Optional[bool]
    band_hold: Optional[bool]
    fully_met: bool
    partial_met: bool
    note: str = ""


def classify_btc_stab(
    z: Optional[float],
    z_prev_deep: bool,
    vol_compressing: Optional[bool],
    band: Optional[bool],
    k: dict[str, float],
) -> BtcStab:
    recover = k["btc_z_recover"]
    z_recovered = bool(z is not None and z_prev_deep and z >= recover)
    vol_ok = bool(vol_compressing) or bool(band)
    fully = z_recovered and vol_ok
    partial = (z_recovered or vol_ok) and not fully
    notes = []
    if z is None:
        notes.append("z BTC n/d (proxy)")
    if vol_compressing is None and band is None:
        notes.append("vol/band n/d")
    return BtcStab(
        z=z, z_prev_deep=z_prev_deep, z_recovered=z_recovered,
        vol_compressing=vol_compressing, band_hold=band,
        fully_met=fully, partial_met=partial, note="; ".join(notes),
    )


# ─── Per-name sub-gate evaluation (THE CORE — pure, unit-tested) ─────────────
@dataclass
class AltGate:
    """The five-sub-gate verdict for one watchlist name. ``counts`` is True only
    when data_ok AND z_ok AND hurst_ok AND (not squeeze_flag) AND funding_ok."""
    ticker: str
    sector: str
    # raw metrics
    z: Optional[float]
    z_streak: int
    hurst: Optional[float]
    rsi: Optional[float]
    pct_k: Optional[float]
    higher_highs: Optional[bool]
    funding: Optional[float]
    funding_sign: Optional[int]
    corr: Optional[float]            # CONTEXT ONLY (proxy)
    repairing: Optional[bool]        # CONTEXT ONLY (proxy)
    coverage: float
    # sub-gate booleans
    data_ok: bool
    z_floor_ok: bool                 # z >= floor this reading
    z_persistent: bool               # streak >= persist readings
    z_ok: bool                       # z_floor_ok AND z_persistent
    hurst_ok: bool
    squeeze_flag: bool
    squeeze_reasons: list[str]
    funding_ok: bool
    # final
    counts: bool
    reason: str                      # binding reason when NOT counted


def evaluate_name_gates(
    ticker: str,
    sector: str,
    closes: Optional[list[float]],
    funding: Optional[float],
    k: dict[str, float],
    *,
    z_streak_prev: int = 0,
    funding_prev: Optional[float] = None,
    oi: Optional[float] = None,
    oi_prev: Optional[float] = None,
    corr: Optional[float] = None,
    repairing: Optional[bool] = None,
) -> AltGate:
    """Run the five sub-gates on one name. Pure: all inputs supplied by caller.

    ``z_streak_prev`` is the persisted count of consecutive prior readings with
    z >= floor. The returned ``z_streak`` includes THIS reading (caller persists
    it). Cointegration ``corr``/``repairing`` are context only and never gate.
    """
    lookback = int(k["z_lookback_bars"])
    n_recv = len(closes) if closes else 0
    coverage = coverage_fraction(n_recv, lookback)

    # ── Gate 1: DATA QUALITY (first — never evaluate metrics on degraded data) ──
    data_ok = bool(
        closes is not None
        and coverage >= k["data_min_coverage"]
        and n_recv >= int(k["hurst_min_returns"]) + 1
    )

    # Derive metrics only when there is enough real data.
    if data_ok and closes:
        z = zscore(closes, lookback)
        hurst = hurst_rs(log_returns(closes), int(k["hurst_min_returns"]))
        rsi_v = rsi(closes, 14)
        pct_k = pct_change_last_k(closes, int(k["hh_lookback_bars"]))
        hh = made_higher_highs(closes, int(k["hh_lookback_bars"]))
    else:
        z = hurst = rsi_v = pct_k = None
        hh = None

    # ── Gate 2: Z-SCORE (magnitude floor + persistence) ──
    z_floor_ok = bool(z is not None and z >= k["z_floor"])
    z_streak = (z_streak_prev + 1) if z_floor_ok else 0
    z_persistent = z_streak >= int(k["z_persist_readings"])
    z_ok = z_floor_ok and z_persistent

    # ── Gate 3: HURST (mean-reverting, with uncertainty buffer) ──
    cutoff = hurst_count_cutoff(k)
    hurst_ok = bool(hurst is not None and hurst <= cutoff)

    # ── Gate 4: SQUEEZE / MOMENTUM GUARD (inviolable, multi-signal) ──
    # EXCLUDE on any momentum/squeeze signature. The distinction we encode:
    #   overbought-AND-RISING (RSI>=th WHILE higher highs) = blow-off  -> exclude
    #   overbought-AND-STALLING (RSI>=th but NOT higher highs)         -> allowed
    squeeze_reasons: list[str] = []
    if hurst is not None and hurst >= k["hurst_max"]:
        squeeze_reasons.append(f"Hurst {hurst:.2f}≥{k['hurst_max']:.2f} trending")
    if (
        rsi_v is not None and rsi_v >= k["overbought_rsi"]
        and hh is True
    ):
        squeeze_reasons.append(f"RSI {rsi_v:.0f}≥{k['overbought_rsi']:.0f}+HH (blow-off)")
    if pct_k is not None and pct_k >= k["parabolic_pct"]:
        squeeze_reasons.append(f"parabólico +{pct_k:.0f}%/{int(k['hh_lookback_bars'])}b")
    # OI spike + funding ramp = crowded-long momentum. BOTH halves required and
    # both data points must be present — absent data NEVER fabricates a squeeze.
    if (
        oi is not None and oi_prev is not None and oi_prev > 0
        and (oi - oi_prev) / oi_prev >= k["oi_spike_pct"]
        and funding is not None and funding_prev is not None
        and funding > 0 and (funding - funding_prev) >= k["funding_ramp_delta"]
    ):
        squeeze_reasons.append("OI+funding ramp (crowded-long)")
    squeeze_flag = bool(squeeze_reasons)

    # ── Gate 5: FUNDING (>=0, re-pulled at eval time) ──
    if funding is None:
        funding_sign: Optional[int] = None
    elif funding > 0:
        funding_sign = 1
    elif funding < 0:
        funding_sign = -1
    else:
        funding_sign = 0
    funding_ok = bool(funding is not None and funding >= k["funding_min"])

    # ── Final verdict + binding reason ──
    counts = bool(data_ok and z_ok and hurst_ok and (not squeeze_flag) and funding_ok)
    reason = ""
    if not counts:
        fails: list[str] = []
        if not data_ok:
            fails.append(f"data insuf ({coverage * 100:.0f}% cov)")
        else:
            if not z_floor_ok:
                fails.append(
                    f"z {_fmt_z(z)}<+{k['z_floor']:.2f}" if z is not None and z > 0
                    else f"z {_fmt_z(z)} no overbought"
                )
            elif not z_persistent:
                fails.append(f"z+ no persistente ({z_streak}/{int(k['z_persist_readings'])})")
            if not hurst_ok:
                fails.append(
                    f"Hurst {hurst:.2f}>{cutoff:.2f}" if hurst is not None
                    else "Hurst n/d"
                )
            if squeeze_flag:
                fails.append("squeeze:" + "/".join(squeeze_reasons))
            if not funding_ok:
                fails.append("funding<0" if funding_sign == -1 else "funding n/d")
        reason = " · ".join(fails) if fails else "no cumple gates"

    return AltGate(
        ticker=ticker, sector=sector, z=z, z_streak=z_streak, hurst=hurst,
        rsi=rsi_v, pct_k=pct_k, higher_highs=hh, funding=funding,
        funding_sign=funding_sign, corr=corr, repairing=repairing,
        coverage=coverage, data_ok=data_ok, z_floor_ok=z_floor_ok,
        z_persistent=z_persistent, z_ok=z_ok, hurst_ok=hurst_ok,
        squeeze_flag=squeeze_flag, squeeze_reasons=squeeze_reasons,
        funding_ok=funding_ok, counts=counts, reason=reason,
    )


@dataclass
class BreadthState:
    asi: Optional[float]
    asi_estimated: bool
    btc_d: Optional[float]
    btcd_rolling_over: Optional[bool]
    soft_confirm: bool
    note: str = ""


def classify_breadth(
    asi: Optional[float],
    asi_estimated: bool,
    btc_d: Optional[float],
    btcd_roll: Optional[bool],
    asi_floor: float = 40.0,
) -> BreadthState:
    """Soft confirmation (context only): ASI rising out of deep Bitcoin-Season
    (>floor) OR BTC.D rolling over (lower-high then lower-low)."""
    asi_confirm = bool(asi is not None and asi >= asi_floor)
    btcd_confirm = bool(btcd_roll)
    soft = asi_confirm or btcd_confirm
    notes = []
    if asi is None:
        notes.append("ASI n/d")
    elif asi_estimated:
        notes.append("ASI estimado")
    if btc_d is None:
        notes.append("BTC.D n/d")
    return BreadthState(
        asi=asi, asi_estimated=asi_estimated, btc_d=btc_d,
        btcd_rolling_over=btcd_roll, soft_confirm=soft, note="; ".join(notes),
    )


def count_summary(alts: list[AltGate]) -> tuple[int, int]:
    """Return (n_counts, n_distinct_sectors) over the names that COUNT."""
    counting = [a for a in alts if a.counts]
    n = len(counting)
    sectors = {a.sector for a in counting if a.sector not in ("", "—")}
    return n, len(sectors)


def aggregate_level(
    btc: BtcStab,
    alts: list[AltGate],
    breadth: BreadthState,
    k: dict[str, float],
    unlock_streak_eff: int,
) -> str:
    """Compute the current level from the five-sub-gate counts + sector breadth
    + persistence (UNLOCK), with A/C as soft WATCH/APPROACHING context.

        UNLOCK      — >=names_required names pass ALL gates AND span >=min_sectors
                      AND the condition has persisted >=unlock_persist_readings.
        APPROACHING — names_approaching..(req-1) names pass all gates, OR >=req
                      pass but fail sector-independence / persistence, OR BTC
                      stabilization (A) fully met.
        WATCH       — regime breadth (C) soft-confirm OR A partially met.
        NONE        — otherwise.
    """
    n_counts, sectors = count_summary(alts)
    req = int(k["names_required"])
    appr = int(k["names_approaching"])
    min_sec = int(k["min_sectors"])
    persist = int(k["unlock_persist_readings"])

    if n_counts >= req and sectors >= min_sec and unlock_streak_eff >= persist:
        return UNLOCK
    if (appr <= n_counts < req) or (n_counts >= req) or btc.fully_met:
        return APPROACHING
    if breadth.soft_confirm or btc.partial_met:
        return WATCH
    return NONE


# ─── State machine (SQLite-backed, edge-triggered) ───────────────────────────
def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS unlock_state (
            key           TEXT PRIMARY KEY,
            level         TEXT NOT NULL DEFAULT 'NONE',
            updated_at    TEXT,
            btc_z_deep    INTEGER NOT NULL DEFAULT 0,
            vol_series    TEXT,
            btcd_series   TEXT,
            unlock_streak INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # Migration: add unlock_streak to a pre-R-UNLOCK-PRECISION table if missing.
    try:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(unlock_state)").fetchall()}
        if "unlock_streak" not in cols:
            c.execute("ALTER TABLE unlock_state ADD COLUMN unlock_streak INTEGER NOT NULL DEFAULT 0")
    except Exception:  # noqa: BLE001
        log.exception("unlock: unlock_streak migration failed (non-fatal)")
    # Per-name z-persistence streaks (R-UNLOCK-PRECISION gate 2).
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS unlock_alt_state (
            ticker       TEXT PRIMARY KEY,
            z_streak     INTEGER NOT NULL DEFAULT 0,
            funding_last REAL,
            oi_last      REAL,
            updated_at   TEXT
        )
        """
    )
    c.commit()
    return c


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state() -> dict[str, Any]:
    c = _conn()
    try:
        r = c.execute("SELECT * FROM unlock_state WHERE key='singleton'").fetchone()
    finally:
        c.close()
    if r is None:
        return {
            "level": NONE, "btc_z_deep": False, "vol_series": [],
            "btcd_series": [], "unlock_streak": 0,
        }
    import json
    keys = r.keys()
    return {
        "level": r["level"] or NONE,
        "btc_z_deep": bool(r["btc_z_deep"]),
        "vol_series": json.loads(r["vol_series"] or "[]"),
        "btcd_series": json.loads(r["btcd_series"] or "[]"),
        "unlock_streak": int(r["unlock_streak"]) if "unlock_streak" in keys and r["unlock_streak"] is not None else 0,
    }


def save_state(
    level: str,
    btc_z_deep: bool,
    vol_series: list[float],
    btcd_series: list[float],
    unlock_streak: int = 0,
) -> None:
    import json
    c = _conn()
    try:
        c.execute(
            """
            INSERT INTO unlock_state (key, level, updated_at, btc_z_deep, vol_series, btcd_series, unlock_streak)
            VALUES ('singleton', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                level=excluded.level, updated_at=excluded.updated_at,
                btc_z_deep=excluded.btc_z_deep, vol_series=excluded.vol_series,
                btcd_series=excluded.btcd_series, unlock_streak=excluded.unlock_streak
            """,
            (level, _now_iso(), 1 if btc_z_deep else 0,
             json.dumps(vol_series[-20:]), json.dumps(btcd_series[-20:]),
             int(unlock_streak)),
        )
        c.commit()
    finally:
        c.close()


def load_alt_state() -> dict[str, dict[str, Any]]:
    """{TICKER: {z_streak, funding_last, oi_last}} from SQLite ({} when empty)."""
    c = _conn()
    try:
        rows = c.execute("SELECT * FROM unlock_alt_state").fetchall()
    finally:
        c.close()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        out[str(r["ticker"]).upper()] = {
            "z_streak": int(r["z_streak"] or 0),
            "funding_last": _f(r["funding_last"]),
            "oi_last": _f(r["oi_last"]),
        }
    return out


def save_alt_state(ticker: str, z_streak: int, funding_last: Optional[float], oi_last: Optional[float]) -> None:
    c = _conn()
    try:
        c.execute(
            """
            INSERT INTO unlock_alt_state (ticker, z_streak, funding_last, oi_last, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                z_streak=excluded.z_streak, funding_last=excluded.funding_last,
                oi_last=excluded.oi_last, updated_at=excluded.updated_at
            """,
            (ticker.upper(), int(z_streak), funding_last, oi_last, _now_iso()),
        )
        c.commit()
    finally:
        c.close()


def should_fire(new_level: str, last_level: str) -> bool:
    """Edge-trigger: fire only on an ESCALATION to a higher level. A retreat
    (lower level) updates state silently so the next genuine flip can fire."""
    return _LEVEL_RANK.get(new_level, 0) > _LEVEL_RANK.get(last_level, 0)


def _reset_for_tests() -> None:
    try:
        c = _conn()
        c.execute("DELETE FROM unlock_state")
        c.execute("DELETE FROM unlock_alt_state")
        c.commit()
        c.close()
    except Exception:  # noqa: BLE001
        pass


# ─── Network (wrapped, keyless) ──────────────────────────────────────────────
async def _hl_post(payload: dict[str, Any]) -> Any:
    # R-BOT-DEFINITIVE WI-4: route through the SHARED rate-limited + TTL-cached
    # HL client unless the test/env points at a non-default HL_INFO_URL.
    if HL_INFO_URL.rstrip("/").endswith("hyperliquid.xyz/info"):
        try:
            from modules.hl_client import post_info
            return await post_info(payload)
        except ImportError:  # pragma: no cover
            pass
    if not _HTTPX_OK:
        raise RuntimeError("httpx unavailable")
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
        resp = await client.post(
            HL_INFO_URL, json=payload,
            headers={"User-Agent": _UA, "Accept": "application/json"},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"HL HTTP {resp.status_code}")
    return resp.json()


async def fetch_4h_closes(coin: str, bars: int) -> Optional[list[float]]:
    """Last ``bars`` 4h closes (oldest→newest) from HL candleSnapshot. None on
    miss. Never raises."""
    bars = max(4, int(bars))
    coin = (coin or "").strip().upper()
    now_ms = int(time.time() * 1000)
    interval_ms = 4 * 3600 * 1000
    start_ms = now_ms - (bars + 4) * interval_ms
    try:
        candles = await _hl_post({
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": "4h", "startTime": start_ms, "endTime": now_ms},
        })
        closes = [_f(c.get("c")) for c in candles if isinstance(c, dict)]
        closes = [c for c in closes if c is not None]
        if len(closes) < 3:
            return None
        return closes[-bars:]
    except Exception as exc:  # noqa: BLE001
        log.warning("unlock: 4h candles n/d for %s (%s)", coin, exc)
        return None


_ctx_cache: dict[str, Any] = {"ts": 0.0, "by_coin": None}


async def fetch_asset_ctx_map() -> dict[str, dict[str, Optional[float]]]:
    """{COIN: {"funding": rate, "oi": open_interest}} from HL metaAndAssetCtxs,
    cached 60s. {} on miss. Funding re-pulled at evaluation time (gate 5)."""
    if _ctx_cache["by_coin"] is not None and (time.time() - _ctx_cache["ts"]) < 60.0:
        return _ctx_cache["by_coin"]  # type: ignore[return-value]
    try:
        data = await _hl_post({"type": "metaAndAssetCtxs"})
        meta, ctxs = data[0], data[1]
        universe = meta.get("universe", [])
        out: dict[str, dict[str, Optional[float]]] = {}
        for asset, ctx in zip(universe, ctxs):
            name = str(asset.get("name", "")).upper()
            if not name or not isinstance(ctx, dict):
                continue
            out[name] = {
                "funding": _f(ctx.get("funding")),
                "oi": _f(ctx.get("openInterest")),
            }
        _ctx_cache["by_coin"] = out
        _ctx_cache["ts"] = time.time()
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("unlock: HL asset-ctx map n/d (%s)", exc)
        return {}


async def fetch_funding_map() -> dict[str, float]:
    """Back-compat shim: {COIN: funding_rate} (funding only). {} on miss."""
    ctx = await fetch_asset_ctx_map()
    return {k: v["funding"] for k, v in ctx.items() if v.get("funding") is not None}  # type: ignore[misc]


async def fetch_btc_dominance() -> Optional[float]:
    """BTC.D % from CoinGecko /global (keyless). None on miss."""
    if not _HTTPX_OK:
        return None
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
            resp = await client.get(COINGECKO_GLOBAL_URL, headers={"User-Agent": _UA, "Accept": "application/json"})
        if resp.status_code != 200:
            return None
        data = resp.json().get("data") or {}
        return _f((data.get("market_cap_percentage") or {}).get("btc"))
    except Exception as exc:  # noqa: BLE001
        log.warning("unlock: BTC.D n/d (%s)", exc)
        return None


async def fetch_asi() -> tuple[Optional[float], bool]:
    """Altcoin Season Index (0-100). Returns (value, estimated_flag).

    Context-only and frequently blocked; on any failure returns (None, True)
    so the caller labels it n/d (estimado) and never treats it as authoritative.
    """
    if not _HTTPX_OK:
        return None, True
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
            resp = await client.get(ASI_URL, headers={"User-Agent": _UA, "Accept": "application/json"})
        if resp.status_code != 200:
            return None, True
        data = resp.json()
        if isinstance(data, (int, float)):
            return _f(data), True
        if isinstance(data, dict):
            for key in ("value", "index", "altcoinSeasonIndex"):
                if key in data:
                    return _f(data[key]), True
        if isinstance(data, list) and data:
            last = data[-1]
            if isinstance(last, dict):
                for key in ("value", "index"):
                    if key in last:
                        return _f(last[key]), True
        return None, True
    except Exception as exc:  # noqa: BLE001
        log.warning("unlock: ASI n/d (%s)", exc)
        return None, True


# ─── Sector hints (so UNLOCK can require cross-sector breadth) ───────────────
_SECTORS = {
    "MORPHO": "DeFi/Lending", "UNI": "DeFi/DEX", "MKR": "DeFi/CDP", "INJ": "DeFi/L1",
    "BNB": "Exchange/L1", "XLM": "Payments", "HBAR": "Enterprise/DAG",
    "ALGO": "L1", "NEAR": "L1", "TAO": "AI", "WLD": "AI/Identity",
}


def sector_of(ticker: str) -> str:
    return _SECTORS.get((ticker or "").strip().upper(), "—")


# ─── Orchestration ───────────────────────────────────────────────────────────
@dataclass
class UnlockSnapshot:
    level: str
    btc: BtcStab
    alts: list[AltGate]
    breadth: BreadthState
    n_counts: int
    n_sectors: int
    unlock_streak: int
    ts_utc: str
    constants: dict[str, float] = field(default_factory=dict)
    confidence: list[str] = field(default_factory=list)


async def compute_snapshot(advance_state: bool = True) -> UnlockSnapshot:
    """Fetch all inputs and compute the five-sub-gate verdict + level. NEVER raises.

    ``advance_state=True`` (scheduler) advances and persists the rolling series,
    per-name z-persistence streaks, and the unlock-condition streak. The
    /unlockcheck command passes ``advance_state=False`` so it is a PURE READ that
    reflects "if this reading counted" without inflating the persistence counters.
    """
    k = constants()
    names = watchlist()
    st = load_state()
    alt_prev = load_alt_state()
    confidence: list[str] = [
        "PRE-FILTRO de alta precisión — confirmá 5/5 con AiPear antes de ejecutar. El bot NO selecciona tokens.",
        "z y Hurst ESTIMADOS de velas 4h; cointegración = PROXY de contexto (NO gatea); velas degradadas se EXCLUYEN; ASI no siempre fetchable.",
    ]

    # ── Condition-A context: BTC stabilization (feeds WATCH/APPROACHING) ──
    btc_closes = await fetch_4h_closes(
        BTC_COIN, int(max(k["z_lookback_bars"], k["btc_band_bars"]) + 4)
    )
    btc_z = zscore(btc_closes, int(k["z_lookback_bars"])) if btc_closes else None
    band = band_hold(btc_closes, k["btc_band_pct"], int(k["btc_band_bars"])) if btc_closes else None
    rv = realized_vol(btc_closes, int(k["z_lookback_bars"])) if btc_closes else None

    vol_series = list(st.get("vol_series") or [])
    if rv is not None:
        vol_series.append(rv)
    vol_compressing = series_is_contracting(vol_series, int(k["vol_compression_readings"]))

    z_prev_deep = bool(st.get("btc_z_deep"))
    if btc_z is not None and btc_z <= k["btc_z_deep"]:
        z_prev_deep = True
    btc = classify_btc_stab(btc_z, z_prev_deep, vol_compressing, band, k)
    z_prev_deep_next = False if btc.z_recovered else z_prev_deep

    # ── Per-name five-sub-gate evaluation (the trigger) ──
    ctx_map = await fetch_asset_ctx_map()
    alts: list[AltGate] = []
    fetched_ok = 0
    for name in names:
        closes = await fetch_4h_closes(name, int(k["corr_lookback_bars"] + 4))
        if closes:
            fetched_ok += 1
        ctx = ctx_map.get(name, {})
        funding = ctx.get("funding")
        oi = ctx.get("oi")
        prev = alt_prev.get(name, {})
        z_streak_prev = int(prev.get("z_streak", 0) or 0)
        funding_prev = prev.get("funding_last")
        oi_prev = prev.get("oi_last")
        # cointegration proxy (CONTEXT ONLY)
        corr = (
            rolling_corr_vs_btc(closes, btc_closes, int(k["corr_lookback_bars"]))
            if (closes and btc_closes) else None
        )
        repairing = (
            corr_is_repairing(closes, btc_closes, int(k["corr_lookback_bars"]))
            if (closes and btc_closes) else None
        )
        gate = evaluate_name_gates(
            name, sector_of(name), closes, funding, k,
            z_streak_prev=z_streak_prev, funding_prev=funding_prev,
            oi=oi, oi_prev=oi_prev, corr=corr, repairing=repairing,
        )
        alts.append(gate)
        if advance_state:
            try:
                save_alt_state(name, gate.z_streak, funding, oi)
            except Exception:  # noqa: BLE001
                log.exception("unlock: save_alt_state failed for %s", name)

    if fetched_ok < len(names):
        confidence.append(
            f"Datos 4h incompletos: {fetched_ok}/{len(names)} watchlist con velas (resto EXCLUIDO por data-quality)."
        )

    # ── Condition-C context: regime breadth ──
    btc_d = await fetch_btc_dominance()
    btcd_series = list(st.get("btcd_series") or [])
    if btc_d is not None:
        btcd_series.append(btc_d)
    btcd_roll = btcd_rolling_over(btcd_series)
    asi, asi_est = await fetch_asi()
    breadth = classify_breadth(asi, asi_est, btc_d, btcd_roll)
    if asi is None:
        confidence.append("ASI no fetchable → C se apoya solo en BTC.D (soft, contexto).")

    # ── Unlock-condition persistence (debounce) ──
    n_counts, n_sectors = count_summary(alts)
    req = int(k["names_required"])
    min_sec = int(k["min_sectors"])
    cond = (n_counts >= req and n_sectors >= min_sec)
    unlock_streak_prev = int(st.get("unlock_streak", 0) or 0)
    unlock_streak_eff = (unlock_streak_prev + 1) if cond else 0

    level = aggregate_level(btc, alts, breadth, k, unlock_streak_eff)

    # Persist rolling series + arm flag + streak (scheduler owns the LEVEL).
    if advance_state:
        try:
            save_state(st.get("level", NONE), z_prev_deep_next, vol_series,
                       btcd_series, unlock_streak_eff)
        except Exception:  # noqa: BLE001
            log.exception("unlock: save_state (series) failed")

    return UnlockSnapshot(
        level=level, btc=btc, alts=alts, breadth=breadth,
        n_counts=n_counts, n_sectors=n_sectors, unlock_streak=unlock_streak_eff,
        ts_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        constants=k, confidence=confidence,
    )


# ─── Formatting ──────────────────────────────────────────────────────────────
def _fmt_z(z: Optional[float]) -> str:
    if z is None:
        return "n/d"
    return f"{'+' if z >= 0 else ''}{z:.2f}"


def _fmt_corr(c: Optional[float]) -> str:
    if c is None:
        return "n/d"
    return f"{c:.2f}"


def _fmt_funding(sign: Optional[int]) -> str:
    return {1: "≥0 ✅", 0: "=0 ✅", -1: "<0 ❌", None: "n/d"}[sign]


def _fmt_hurst(h: Optional[float]) -> str:
    return f"{h:.2f}" if h is not None else "n/d"


def _tri(v: Optional[bool]) -> str:
    return "sí ✅" if v else ("no ❌" if v is False else "n/d")


def _fmt_num(v: Optional[float]) -> str:
    if v is None:
        return "n/d"
    return f"{v:.1f}"


def _gate_line(a: AltGate, k: dict[str, float]) -> str:
    """One per-name sub-gate row for /unlockcheck and the UNLOCK alert."""
    cutoff = hurst_count_cutoff(k)
    mark = "🔓" if a.counts else "·"
    if not a.data_ok:
        body = (
            f"data {a.coverage * 100:.0f}%<{k['data_min_coverage'] * 100:.0f}% ❌ "
            f"→ NO (data insuficiente, no contado)"
        )
        return f"  {mark} {a.ticker:<6} [{a.sector}] — {body}"
    ztag = "✅" if a.z_ok else "❌"
    ztxt = f"z {_fmt_z(a.z)}≥+{k['z_floor']:.2f}"
    if a.z_floor_ok and not a.z_persistent:
        ztxt += f"(persist {a.z_streak}/{int(k['z_persist_readings'])})"
    htag = "✅" if a.hurst_ok else "❌"
    sqtag = "✅" if not a.squeeze_flag else "❌"
    sqtxt = "clear" if not a.squeeze_flag else ("/".join(a.squeeze_reasons))
    ftag = "✅" if a.funding_ok else "❌"
    coint = f"coint~{_fmt_corr(a.corr)}(ctx)"
    parts = [
        f"{ztxt}{ztag}",
        f"Hurst {_fmt_hurst(a.hurst)}≤{cutoff:.2f}{htag}",
        f"squeeze {sqtxt}{sqtag}",
        f"fund {_fmt_funding(a.funding_sign)}{ftag}",
        coint,
    ]
    verdict = "COUNTS: SÍ" if a.counts else f"NO ({a.reason})"
    return f"  {mark} {a.ticker:<6} [{a.sector}] — " + " | ".join(parts) + f"  → {verdict}"


def format_unlockcheck(s: UnlockSnapshot) -> str:
    """Render /unlockcheck — full state + per-name five-sub-gate table."""
    k = s.constants
    em = _LEVEL_EMOJI.get(s.level, "")
    cutoff = hurst_count_cutoff(k)
    lines = [
        f"🔓 R-UNLOCK — desbloqueo de canasta (PRE-FILTRO 5-gates)  {em} {s.level}",
        f"{s.ts_utc}",
        "",
        "PRE-FILTRO de alta precisión. El bot NO selecciona tokens ni arma la "
        "canasta — solo avisa 'condiciones acercándose, re-screeneá'. La "
        "selección 5/5 final es 100% tuya + AiPear.",
        "",
        "── SUB-GATES (un nombre CUENTA solo si pasa los 5 a la vez) ──",
        f"  1) data ≥{k['data_min_coverage'] * 100:.0f}% velas | "
        f"2) z ≥+{k['z_floor']:.2f} y persistente ≥{int(k['z_persist_readings'])} lecturas | "
        f"3) Hurst ≤{cutoff:.2f} | 4) squeeze CLEAR | 5) funding ≥0",
        "  Cointegración = PROXY de contexto, NO gatea.",
        "",
        f"── NOMBRES ({len(s.alts)} watchlist) ──",
    ]
    for a in s.alts:
        lines.append(_gate_line(a, k))
    lines += [
        "",
        f"  → CUENTAN: {s.n_counts}/{len(s.alts)} nombres (req {int(k['names_required'])}) "
        f"en {s.n_sectors} sectores (req {int(k['min_sectors'])}); "
        f"persistencia {s.unlock_streak}/{int(k['unlock_persist_readings'])}",
        "",
        "── CONTEXTO ──",
        f"  A) BTC z 4h {_fmt_z(s.btc.z)} (armado deep≤{k['btc_z_deep']:.1f}: "
        f"{'sí' if s.btc.z_prev_deep else 'no'}, recover≥{k['btc_z_recover']:.1f}: "
        f"{'✅' if s.btc.z_recovered else '❌'}) | vol-comp {_tri(s.btc.vol_compressing)} | "
        f"banda {_tri(s.btc.band_hold)} → A {'COMPLETA' if s.btc.fully_met else ('PARCIAL' if s.btc.partial_met else 'no')}",
        f"  C) ASI {_fmt_num(s.breadth.asi)}"
        f"{' (estimado)' if s.breadth.asi_estimated and s.breadth.asi is not None else ''} | "
        f"BTC.D {_fmt_num(s.breadth.btc_d)}% rollover {_tri(s.breadth.btcd_rolling_over)} → "
        f"soft {'sí' if s.breadth.soft_confirm else 'no'}",
        "",
        f"NIVEL ACTUAL: {em} {s.level}",
    ]
    if s.confidence:
        lines.append("")
        lines.append("Confianza / proxies:")
        for c in s.confidence:
            lines.append(f"  • {c}")
    return "\n".join(lines)


def aipear_block(s: UnlockSnapshot) -> str:
    """Compact MACHINE-READABLE block of the qualifying names — paste straight
    into AiPear for the 5/5 confirmation screen."""
    counting = [a for a in s.alts if a.counts]
    lines = ["```", "AIPEAR_CONFIRM v1 (pre-filtro R-UNLOCK — confirmar 5/5):"]
    lines.append("ticker,sector,z4h,hurst,funding,data_conf")
    for a in counting:
        conf = f"{a.coverage * 100:.0f}%"
        fund = f"{a.funding:+.6f}" if a.funding is not None else "n/d"
        lines.append(
            f"{a.ticker},{a.sector},{_fmt_z(a.z)},{_fmt_hurst(a.hurst)},{fund},{conf}"
        )
    lines.append("```")
    return "\n".join(lines)


def format_alert(s: UnlockSnapshot, prev_level: str) -> str:
    """Render the escalating transition alert for ``s.level``."""
    k = s.constants
    em = _LEVEL_EMOJI.get(s.level, "")
    head = f"{em} R-UNLOCK {s.level} (desde {prev_level})  ·  {s.ts_utc}"
    if s.level == WATCH:
        body = (
            "Régimen ablandándose — condiciones de desbloqueo formándose. "
            "Todavía NO es gatillo de re-screen."
        )
    elif s.level == APPROACHING:
        why = []
        if s.n_counts >= int(k["names_required"]) and s.n_sectors < int(k["min_sectors"]):
            why.append(f"{s.n_counts} pasan gates pero solo {s.n_sectors} sectores (req {int(k['min_sectors'])}) — concentración")
        elif s.n_counts >= int(k["names_required"]) and s.unlock_streak < int(k["unlock_persist_readings"]):
            why.append(f"{s.n_counts} pasan gates pero falta persistencia ({s.unlock_streak}/{int(k['unlock_persist_readings'])})")
        elif s.n_counts >= int(k["names_approaching"]):
            why.append(f"{s.n_counts} nombres pasan los 5 gates (req {int(k['names_required'])})")
        else:
            why.append("BTC estabilizándose (contexto A)")
        body = ("Acercándose — " + "; ".join(why) + ". Re-screeneá el set pronto con AiPear.")
    elif s.level == UNLOCK:
        trig = [a for a in s.alts if a.counts]
        sectors = sorted({a.sector for a in trig})
        body_lines = [
            f"GATILLO DURO — {len(trig)} nombres pasan los 5 sub-gates a la vez "
            f"en {len(sectors)} sectores ({', '.join(sectors) if sectors else 'n/d'}); "
            f"persistido {s.unlock_streak}/{int(k['unlock_persist_readings'])}:",
            "",
        ]
        for a in trig:
            body_lines.append(_gate_line(a, k))
        body_lines += [
            "",
            aipear_block(s),
            "",
            "PRE-FILTRO ONLY — confirmá con AiPear 5/5 antes de ejecutar. El bot no selecciona tokens.",
            "Screen 5/5 = squeeze CLEAR + z+ sobre piso + Hurst<0.5 + funding≥0 + Bollinger/overbought.",
        ]
        body = "\n".join(body_lines)
    else:
        body = ""
    parts = [head, "", body]
    if s.confidence:
        parts.append("")
        parts.append("Nota de confianza: " + " ".join(s.confidence))
    return "\n".join(parts)
