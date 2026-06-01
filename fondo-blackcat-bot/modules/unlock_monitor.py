"""R-UNLOCK — basket-entry-unlock regime monitor (2026-06-01).

WHAT THIS IS
    Fondo Black Cat runs market-neutral SHORT baskets that require a strict
    5/5 per-leg screen (positive 4h z-score + Hurst<0.5 + Engle-Granger
    cointegration vs BTC 90d + funding>=0 + squeeze clear). As of today ZERO
    names pass 5/5: the market bifurcated — every positive-z name broke its BTC
    cointegration via an idiosyncratic catalyst, and every cointegrated name
    sits below its mean (negative z). The basket is a vol-COMPRESSION trade and
    we are in vol-EXPANSION.

    This monitor watches for that regime conflict starting to resolve and fires
    ONE escalating alert so BCD re-screens MANUALLY. It tracks three conditions:

        A) BTC STABILIZATION (primary)
        B) ALT RE-CORRELATION (core trigger — the 5/5-unlock signal)
        C) REGIME BREADTH (confirmation, soft)

    HARD BOUNDARY — the bot does NOT select tokens, design baskets, size, or
    execute. It only flags "conditions approaching, re-screen now." Token
    selection stays 100% with the human + AiPear.

HONESTY STANDARD
    Real Engle-Granger cointegration (ADF on the regression residual) needs a
    stats stack we don't ship and 90d of clean 4h bars. Where the true input is
    not directly fetchable we use the best obtainable PROXY — rolling Pearson
    correlation of 4h log-returns vs BTC — and LABEL every such metric as
    estimated/proxy. We NEVER fabricate a cointegration pass and we NEVER crash
    the scheduler: any data gap degrades that leg to "unknown" and pulls the
    confidence note, it can never silently manufacture an UNLOCK.

DATA SOURCES (all keyless, read-only, no custody, no keys)
    * 4h closes / OHLC: Hyperliquid `candleSnapshot` (interval 4h).
    * Funding sign: Hyperliquid `metaAndAssetCtxs` ("funding" field).
    * BTC dominance: CoinGecko `/global` (market_cap_percentage.btc).
    * Altcoin Season Index: best-effort fetch; degrades to "n/d (estimado)"
      when the source is unreachable (it is context-only, never standalone).
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
# liquid majors that historically cointegrate with BTC and are the natural
# candidate set for a market-neutral short basket; configurable via
# UNLOCK_WATCHLIST (comma list of HL tickers).
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
    """Snapshot of every R-UNLOCK threshold (env-overridable).

    Defaults + reasoning:
      z_lookback_bars 42    ~7d of 4h bars — long enough to define a mean,
                            short enough to react to a regime turn.
      z_positive_cutoff 0.0 z>0 == price back above its rolling mean.
      btc_z_deep -1.0       "deeply negative" arm point.
      btc_z_recover -0.5    z crossing above this (after being < deep) == BTC
                            recovering toward zero (primary A condition).
      corr_lookback_bars 180 ~30d of 4h bars (configurable up to 540 ≈ 90d) for
                            the cointegration proxy vs BTC.
      coint_threshold 0.6   rolling-corr proxy at/above this == "repairing
                            toward cointegration" (labelled estimated).
      names_required 4      >=4 independent names => UNLOCK (the 5/5 signal).
      names_approaching 2   2-3 names re-correlating => APPROACHING.
      vol_compression_readings 3  N consecutive realized-vol contractions.
      btc_band_pct 5.0      BTC inside +/-5% ...
      btc_band_bars 15      ...over ~60h (15×4h) == vol holding / compressing.
      funding_min 0.0       per-leg funding must be >= this (>=0 short-friendly).
    """
    return {
        "z_lookback_bars": float(_envi("UNLOCK_Z_LOOKBACK_BARS", 42)),
        "z_positive_cutoff": _envf("UNLOCK_Z_POSITIVE_CUTOFF", 0.0),
        "btc_z_deep": _envf("UNLOCK_BTC_Z_DEEP", -1.0),
        "btc_z_recover": _envf("UNLOCK_BTC_Z_RECOVER", -0.5),
        "corr_lookback_bars": float(_envi("UNLOCK_CORR_LOOKBACK_BARS", 180)),
        "coint_threshold": _envf("UNLOCK_COINT_THRESHOLD", 0.6),
        "names_required": float(_envi("UNLOCK_NAMES_REQUIRED", 4)),
        "names_approaching": float(_envi("UNLOCK_NAMES_APPROACHING", 2)),
        "vol_compression_readings": float(_envi("UNLOCK_VOL_COMPRESSION_READINGS", 3)),
        "btc_band_pct": _envf("UNLOCK_BTC_BAND_PCT", 5.0),
        "btc_band_bars": float(_envi("UNLOCK_BTC_BAND_BARS", 15)),
        "funding_min": _envf("UNLOCK_FUNDING_MIN", 0.0),
    }


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
    """Engle-Granger cointegration PROXY: rolling Pearson corr of 4h log-returns
    vs BTC over ``lookback`` bars. Labelled estimated everywhere it surfaces."""
    a = log_returns(alt_closes)
    b = log_returns(btc_closes)
    n = min(len(a), len(b), int(lookback))
    if n < 3:
        return None
    return pearson(a[-n:], b[-n:])


def corr_is_repairing(
    alt_closes: list[float], btc_closes: list[float], lookback: int
) -> Optional[bool]:
    """True when the recent-half correlation > older-half (repairing toward
    cointegration), False when deteriorating, None when undeterminable."""
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
    # Compare the last two local extremes crudely: split in halves, require the
    # recent half's max < older half's max AND recent min < older min.
    half = len(vals) // 2
    old, rec = vals[:half], vals[half:]
    if not old or not rec:
        return None
    return (max(rec) < max(old)) and (min(rec) < min(old))


# ─── Condition classifiers (pure) ────────────────────────────────────────────
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
    z_recovered = bool(
        z is not None and z_prev_deep and z >= recover
    )
    # Vol half of A: either N consecutive realized-vol contractions OR a held
    # +/-5% band counts as compression.
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


@dataclass
class AltState:
    ticker: str
    z: Optional[float]
    corr: Optional[float]          # PROXY (rolling Pearson vs BTC)
    repairing: Optional[bool]      # PROXY direction
    funding_sign: Optional[int]    # +1 / 0 / -1 / None
    positive_z: bool
    coint_ok: bool                 # corr >= threshold (proxy)
    funding_ok: bool
    triggered: bool                # the 5/5-unlock combo for this name
    sector: str = ""


def classify_alt(
    ticker: str,
    z: Optional[float],
    corr: Optional[float],
    repairing: Optional[bool],
    funding: Optional[float],
    k: dict[str, float],
    sector: str = "",
) -> AltState:
    positive_z = bool(z is not None and z > k["z_positive_cutoff"])
    coint_ok = bool(corr is not None and corr >= k["coint_threshold"])
    fsign: Optional[int]
    if funding is None:
        fsign = None
    elif funding > 0:
        fsign = 1
    elif funding < 0:
        fsign = -1
    else:
        fsign = 0
    funding_ok = bool(fsign is not None and funding is not None and funding >= k["funding_min"])
    # The 5/5-unlock combo (per spec): repairing cointegration WHILE positive z
    # AND funding>=0. "Repairing" is satisfied by being at/above the proxy
    # threshold OR by an improving-correlation direction.
    repairing_ok = bool(coint_ok or repairing)
    triggered = positive_z and repairing_ok and funding_ok
    return AltState(
        ticker=ticker, z=z, corr=corr, repairing=repairing, funding_sign=fsign,
        positive_z=positive_z, coint_ok=coint_ok, funding_ok=funding_ok,
        triggered=triggered, sector=sector,
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
    """Soft confirmation: ASI rising out of deep Bitcoin-Season (>floor) OR
    BTC.D rolling over (lower-high then lower-low). Context only."""
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


def aggregate_level(
    btc: BtcStab, alts: list[AltState], breadth: BreadthState, k: dict[str, float]
) -> str:
    """Compute the current level from A/B/C.

        UNLOCK      — B trigger met: >= names_required triggered names.
        APPROACHING — A fully met OR names_approaching..(req-1) triggered names.
        WATCH       — C soft-confirm OR A partially met.
        NONE        — otherwise.
    """
    n_trig = sum(1 for a in alts if a.triggered)
    req = int(k["names_required"])
    appr = int(k["names_approaching"])
    if n_trig >= req:
        return UNLOCK
    if btc.fully_met or (appr <= n_trig < req):
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
            key         TEXT PRIMARY KEY,
            level       TEXT NOT NULL DEFAULT 'NONE',
            updated_at  TEXT,
            btc_z_deep  INTEGER NOT NULL DEFAULT 0,
            vol_series  TEXT,
            btcd_series TEXT
        )
        """
    )
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
        return {"level": NONE, "btc_z_deep": False, "vol_series": [], "btcd_series": []}
    import json
    return {
        "level": r["level"] or NONE,
        "btc_z_deep": bool(r["btc_z_deep"]),
        "vol_series": json.loads(r["vol_series"] or "[]"),
        "btcd_series": json.loads(r["btcd_series"] or "[]"),
    }


def save_state(level: str, btc_z_deep: bool, vol_series: list[float], btcd_series: list[float]) -> None:
    import json
    c = _conn()
    try:
        c.execute(
            """
            INSERT INTO unlock_state (key, level, updated_at, btc_z_deep, vol_series, btcd_series)
            VALUES ('singleton', ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                level=excluded.level, updated_at=excluded.updated_at,
                btc_z_deep=excluded.btc_z_deep, vol_series=excluded.vol_series,
                btcd_series=excluded.btcd_series
            """,
            (level, _now_iso(), 1 if btc_z_deep else 0,
             json.dumps(vol_series[-20:]), json.dumps(btcd_series[-20:])),
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
        c.commit()
        c.close()
    except Exception:  # noqa: BLE001
        pass


# ─── Network (wrapped, keyless) ──────────────────────────────────────────────
async def _hl_post(payload: dict[str, Any]) -> Any:
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


_funding_cache: dict[str, Any] = {"ts": 0.0, "by_coin": None}


async def fetch_funding_map() -> dict[str, float]:
    """{COIN: funding_rate} from HL metaAndAssetCtxs, cached 60s. {} on miss."""
    if _funding_cache["by_coin"] is not None and (time.time() - _funding_cache["ts"]) < 60.0:
        return _funding_cache["by_coin"]  # type: ignore[return-value]
    try:
        data = await _hl_post({"type": "metaAndAssetCtxs"})
        meta, ctxs = data[0], data[1]
        universe = meta.get("universe", [])
        out: dict[str, float] = {}
        for asset, ctx in zip(universe, ctxs):
            name = str(asset.get("name", "")).upper()
            f = _f(ctx.get("funding")) if isinstance(ctx, dict) else None
            if name and f is not None:
                out[name] = f
        _funding_cache["by_coin"] = out
        _funding_cache["ts"] = time.time()
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("unlock: HL funding map n/d (%s)", exc)
        return {}


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
        # The endpoint shape varies; be defensive. Accept a bare number, a
        # {"value": n} dict, or a list of {date,value} (take the last).
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


# ─── Sector hints (so UNLOCK can flag cross-sector breadth) ──────────────────
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
    alts: list[AltState]
    breadth: BreadthState
    n_triggered: int
    ts_utc: str
    constants: dict[str, float] = field(default_factory=dict)
    confidence: list[str] = field(default_factory=list)


async def compute_snapshot() -> UnlockSnapshot:
    """Fetch all inputs and compute A/B/C + level. NEVER raises.

    Advances the persisted btc_z_deep arm flag and the vol/btcd series, but does
    NOT itself fire or persist the level transition (the scheduler does that, so
    /unlockcheck is a pure read).
    """
    k = constants()
    names = watchlist()
    st = load_state()
    confidence: list[str] = [
        "Cointegración = PROXY (corr rolling de log-returns 4h vs BTC), NO Engle-Granger/ADF real.",
    ]

    # ── BTC stabilization (A) ──
    btc_closes = await fetch_4h_closes(BTC_COIN, int(max(k["z_lookback_bars"], k["btc_band_bars"]) + 4))
    btc_z = zscore(btc_closes, int(k["z_lookback_bars"])) if btc_closes else None
    band = band_hold(btc_closes, k["btc_band_pct"], int(k["btc_band_bars"])) if btc_closes else None
    rv = realized_vol(btc_closes, int(k["z_lookback_bars"])) if btc_closes else None

    vol_series = list(st.get("vol_series") or [])
    if rv is not None:
        vol_series.append(rv)
    vol_compressing = series_is_contracting(vol_series, int(k["vol_compression_readings"]))

    # Arm flag: latch "was deeply negative" once BTC z dips below the deep line.
    z_prev_deep = bool(st.get("btc_z_deep"))
    if btc_z is not None and btc_z <= k["btc_z_deep"]:
        z_prev_deep = True
    btc = classify_btc_stab(btc_z, z_prev_deep, vol_compressing, band, k)
    # Disarm only once recovery confirms, so the next deep dip can re-arm.
    if btc.z_recovered:
        z_prev_deep_next = False
    else:
        z_prev_deep_next = z_prev_deep

    # ── Alt re-correlation (B) ──
    funding_map = await fetch_funding_map()
    alts: list[AltState] = []
    fetched_ok = 0
    for name in names:
        closes = await fetch_4h_closes(name, int(k["corr_lookback_bars"] + 4))
        if closes:
            fetched_ok += 1
        z = zscore(closes, int(k["z_lookback_bars"])) if closes else None
        corr = (
            rolling_corr_vs_btc(closes, btc_closes, int(k["corr_lookback_bars"]))
            if (closes and btc_closes) else None
        )
        repairing = (
            corr_is_repairing(closes, btc_closes, int(k["corr_lookback_bars"]))
            if (closes and btc_closes) else None
        )
        funding = funding_map.get(name)
        alts.append(classify_alt(name, z, corr, repairing, funding, k, sector_of(name)))

    if fetched_ok < len(names):
        confidence.append(
            f"Datos 4h incompletos: {fetched_ok}/{len(names)} watchlist con velas (resto degradado)."
        )

    # ── Regime breadth (C) ──
    btc_d = await fetch_btc_dominance()
    btcd_series = list(st.get("btcd_series") or [])
    if btc_d is not None:
        btcd_series.append(btc_d)
    btcd_roll = btcd_rolling_over(btcd_series)
    asi, asi_est = await fetch_asi()
    breadth = classify_breadth(asi, asi_est, btc_d, btcd_roll)
    if asi is None:
        confidence.append("ASI no fetchable → C se apoya solo en BTC.D (soft).")

    level = aggregate_level(btc, alts, breadth, k)
    n_trig = sum(1 for a in alts if a.triggered)

    # Persist the rolling series + arm flag (NOT the level — scheduler owns that).
    try:
        save_state(st.get("level", NONE), z_prev_deep_next, vol_series, btcd_series)
    except Exception:  # noqa: BLE001
        log.exception("unlock: save_state (series) failed")

    return UnlockSnapshot(
        level=level, btc=btc, alts=alts, breadth=breadth, n_triggered=n_trig,
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


def _alt_line(a: AltState) -> str:
    flags = []
    flags.append(f"z {_fmt_z(a.z)}{'✅' if a.positive_z else '❌'}")
    rep = "↑" if a.repairing else ("↓" if a.repairing is False else "·")
    flags.append(f"coint~{_fmt_corr(a.corr)}{rep}{'✅' if a.coint_ok else '❌'}(est)")
    flags.append(f"fund {_fmt_funding(a.funding_sign)}")
    mark = "🔓" if a.triggered else "·"
    return f"  {mark} {a.ticker:<6} [{a.sector}] — " + " | ".join(flags)


def format_unlockcheck(s: UnlockSnapshot) -> str:
    """Render /unlockcheck — full A/B/C state + per-watchlist breakdown."""
    k = s.constants
    em = _LEVEL_EMOJI.get(s.level, "")
    lines = [
        f"🔓 R-UNLOCK — estado de desbloqueo de canasta  {em} {s.level}",
        f"{s.ts_utc}",
        "",
        "El bot NO selecciona tokens ni arma la canasta. Solo avisa "
        "'condiciones acercándose, re-screeneá'. La selección 5/5 es 100% tuya + AiPear.",
        "",
        "── A) ESTABILIZACIÓN BTC (primaria) ──",
        f"  z 4h BTC: {_fmt_z(s.btc.z)}  (armado deep≤{k['btc_z_deep']:.1f}: "
        f"{'sí' if s.btc.z_prev_deep else 'no'} → recover≥{k['btc_z_recover']:.1f}: "
        f"{'✅' if s.btc.z_recovered else '❌'})",
        f"  vol comprimiendo: {_tri(s.btc.vol_compressing)}  | "
        f"banda ±{k['btc_band_pct']:.0f}% {int(k['btc_band_bars'])}×4h: {_tri(s.btc.band_hold)}",
        f"  → A: {'COMPLETA ✅' if s.btc.fully_met else ('PARCIAL ⚠️' if s.btc.partial_met else 'no ❌')}",
        "",
        f"── B) RE-CORRELACIÓN ALTS (gatillo, {int(k['names_required'])} requeridos) ──",
    ]
    for a in s.alts:
        lines.append(_alt_line(a))
    lines.append(
        f"  → B: {s.n_triggered}/{len(s.alts)} nombres en combo 5/5-unlock "
        f"(req {int(k['names_required'])} → UNLOCK; {int(k['names_approaching'])}+ → APPROACHING)"
    )
    lines += [
        "",
        "── C) AMPLITUD DE RÉGIMEN (soft, contexto) ──",
        f"  ASI: {_fmt_num(s.breadth.asi)}{' (estimado)' if s.breadth.asi_estimated and s.breadth.asi is not None else ''}"
        f"  | BTC.D: {_fmt_num(s.breadth.btc_d)}%  rollover: {_tri(s.breadth.btcd_rolling_over)}",
        f"  → C soft-confirm: {'sí ✅' if s.breadth.soft_confirm else 'no ❌'}",
        "",
        f"NIVEL ACTUAL: {em} {s.level}",
    ]
    if s.confidence:
        lines.append("")
        lines.append("Confianza / proxies:")
        for c in s.confidence:
            lines.append(f"  • {c}")
    return "\n".join(lines)


def format_alert(s: UnlockSnapshot, prev_level: str) -> str:
    """Render the escalating transition alert for ``s.level``."""
    em = _LEVEL_EMOJI.get(s.level, "")
    head = f"{em} R-UNLOCK {s.level} (desde {prev_level})  ·  {s.ts_utc}"
    if s.level == WATCH:
        body = (
            "Régimen ablandándose — condiciones de desbloqueo formándose. "
            "Todavía NO es gatillo de re-screen."
        )
    elif s.level == APPROACHING:
        body = (
            "BTC estabilizándose / re-correlación parcial de alts. "
            "Re-screeneá el set de candidatos pronto."
        )
    elif s.level == UNLOCK:
        trig = [a for a in s.alts if a.triggered]
        sectors = sorted({a.sector for a in trig})
        body_lines = [
            "GATILLO DURO — combo 5/5-unlock en estos tickers "
            f"({len(trig)}, sectores: {', '.join(sectors) if sectors else 'n/d'}):",
        ]
        for a in trig:
            body_lines.append(
                f"  🔓 {a.ticker} [{a.sector}] — z {_fmt_z(a.z)} (+), "
                f"coint~{_fmt_corr(a.corr)} (proxy, repairing), funding {_fmt_funding(a.funding_sign)}"
            )
        body_lines.append("")
        body_lines.append(
            "CORRÉ EL SCREEN 5/5 COMPLETO AHORA (z + Hurst + Engle-Granger + funding + squeeze)."
        )
        body_lines.append(
            "El bot NO selecciona tokens — screen manual obligatorio."
        )
        body = "\n".join(body_lines)
    else:
        body = ""
    parts = [head, "", body]
    if s.confidence:
        parts.append("")
        parts.append("Nota de confianza: " + " ".join(s.confidence))
    return "\n".join(parts)


def _tri(v: Optional[bool]) -> str:
    return "sí ✅" if v else ("no ❌" if v is False else "n/d")


def _fmt_num(v: Optional[float]) -> str:
    if v is None:
        return "n/d"
    return f"{v:.1f}"
