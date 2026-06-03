"""R-SCREEN — universal short/long screener over the FULL tradeable universe.

WHAT THIS IS — A RANKING/CONTEXT LAYER ON TOP OF THE EXISTING 5-GATE ENGINE
    Fondo Black Cat runs a wide directional SHORT book of over-extended alts
    (V11), with HYPE as the structural long core. The manual entry screen is the
    SQUEEZE-FIRST 5-check already implemented in ``unlock_monitor`` (data, z,
    Hurst, squeeze/momentum guard, funding). R-UNLOCK ran that screen over an
    11-name hardcoded watchlist; R-SCREEN broadens EXACTLY THE SAME five gates
    over EVERY perp tradeable on Hyperliquid and Variational, ranks them
    most→least shortable, and adds a clearly-separated LONG-context read.

    NOTHING about the five gates or their thresholds changes here. This module
    NEVER re-implements an indicator — it imports ``evaluate_name_gates`` and the
    z/Hurst/RSI/squeeze/funding math verbatim from ``unlock_monitor`` and only
    layers (a) universe assembly + dedup + venue/liquidity annotation, (b) a
    deterministic shortability score for ordering, (c) a mirror long-viability
    flag (context only), and (d) a single-token query path for /check.

THE BOT STILL NEVER SELECTS OR EXECUTES
    Ranking is context. Only a 5/5 name is a "GO candidate (confirm with
    AiPear)"; everything else is information. The human + AiPear make the final
    5/5 call and execute. The long read is explicitly NOT a mandate to go long
    alts — the fund mandate is HYPE-core long + alt-short book; alt longs are
    tactical/your call + AiPear.

SQUEEZE IS INVIOLABLE — IN GATING **AND** IN RANKING
    A name in an active squeeze / blow-off is un-shortable. The squeeze gate
    already excludes it from COUNTING; here it is ALSO forced to the BOTTOM of
    the shortability ranking, below every non-squeezing name, regardless of how
    high its z is. (See ``short_score``.)

DATA-QUALITY FIRST — NO FABRICATED SCORES
    The data-quality gate runs first. Any asset with <90% real 4h candle
    coverage (or no Hyperliquid candle source at all — e.g. Variational-only
    listings) is EXCLUDED from scoring and listed separately as "data
    insuficiente". We never rank thin/missing data.

DATA SOURCES (all keyless, read-only — REUSED, no new integration)
    * Hyperliquid universe + funding + OI + markPx + 24h volume:
      ``metaAndAssetCtxs`` (same info API as everywhere else).
    * Hyperliquid 4h candles for z/Hurst/RSI/squeeze: ``unlock_monitor.fetch_4h_closes``.
    * Variational universe + funding + OI + 24h volume: ``variational.fetch_markets``
      (the SAME source /variationalfunding already uses).

HONESTY STANDARD (carried over verbatim)
    z and Hurst are ESTIMATED from 4h candles; cointegration is a context-only
    proxy that NEVER gates; ASI is frequently not fetchable; the bot does NOT
    select tokens — a 5/5 + AiPear is the human's call.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

# ── REUSE the existing engine verbatim — DO NOT re-implement any indicator ──
from modules.unlock_monitor import (
    DATA_DIR,
    AltGate,
    constants,
    evaluate_name_gates,
    fetch_4h_closes,
    hurst_count_cutoff,
    rolling_corr_vs_btc,
    sector_of,
    _f,
    _fmt_funding,
    _fmt_hurst,
    _fmt_z,
    _hl_post,
    BTC_COIN,
)
from modules import variational as _var

log = logging.getLogger(__name__)

DB_PATH = os.path.join(DATA_DIR, "intel_memory.db")


# ─── Env knobs (read live so Railway overrides take effect; ALL have safe
#     baked defaults so NO new Railway env var is required to ship) ───────────
def _envi(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(float(raw))
    except ValueError:
        log.warning("bad %s=%r → %s", name, raw, default)
        return default


def top_n_default() -> int:
    """How many names to render in the full-detail '🔴 MÁS SHORTEABLES' block."""
    return max(1, _envi("SCREENER_TOP_N", 15))


def fetch_concurrency() -> int:
    """Max concurrent HL candle fetches (bounded so a ~180-name universe stays
    responsive without tripping the keyless endpoint's rate limiter)."""
    return max(1, _envi("SCREENER_FETCH_CONCURRENCY", 5))


def fetch_retries() -> int:
    """Retries on a None candle result (HL 429/500 backoff). A genuine thin coin
    still returns None after retries → excluded by data-quality, never faked."""
    return max(0, _envi("SCREENER_FETCH_RETRIES", 2))


def max_assets() -> int:
    """Hard cap on assets evaluated (0 = no cap = full universe)."""
    return max(0, _envi("SCREENER_MAX_ASSETS", 0))


# ─── Pure helpers (mirror side; never touch the gate engine) ─────────────────
def made_lower_lows(closes: list[float], k: int) -> Optional[bool]:
    """Mirror of ``made_higher_highs``: True when the last close is the LOWEST of
    the last ``k+1`` bars (still making lower lows = falling). None when short."""
    cl = [c for c in closes if _f(c) is not None]
    k = int(k)
    if len(cl) < k + 1:
        return None
    return cl[-1] <= min(cl[-1 - k:-1])


def short_pass_count(g: AltGate) -> int:
    """How many of the five gates pass (data, z, Hurst, squeeze-clear, funding)."""
    return (
        int(g.data_ok)
        + int(g.z_ok)
        + int(g.hurst_ok)
        + int(not g.squeeze_flag)
        + int(g.funding_ok)
    )


def short_score(g: AltGate) -> float:
    """Deterministic shortability score (higher = more shortable).

    Built ONLY from the existing gate values — no new indicator:

      * dominant term = pass-count × 100, so 5/5 > 4/5 > … ;
      * SQUEEZE IS INVIOLABLE: a squeezing name is pushed below EVERY
        non-squeezing name (−1000) regardless of how high its z is — squeeze =
        un-shortable, so it must rank LOW even with a big z;
      * deterministic tiebreaks inside an equal (squeeze, pass-count) band:
        higher POSITIVE z (more over-extended) and lower Hurst (more
        mean-reverting) = more shortable.

    Data-insufficient names are never ranked (caller excludes them); guarded
    here with −inf for safety.
    """
    if not g.data_ok:
        return float("-inf")
    score = short_pass_count(g) * 100.0
    if g.squeeze_flag:
        score -= 1000.0
    z = g.z if g.z is not None else 0.0
    z = max(-3.0, min(3.0, z))
    h = g.hurst if g.hurst is not None else 0.5
    score += z * 5.0            # bounded ±15
    score += (0.5 - h) * 20.0   # ~±10 for h∈[0,1]
    return score


def short_verdict(g: AltGate) -> str:
    """Human verdict string for the short side."""
    if not g.data_ok:
        return f"SHORT: DATA INSUF ({g.coverage * 100:.0f}% velas)"
    pc = short_pass_count(g)
    if g.squeeze_flag:
        return "SHORT: NO-GO — squeeze activo (" + "/".join(g.squeeze_reasons) + ")"
    if pc == 5:
        return "SHORT: 5/5 GO candidate — confirmá con AiPear"
    return f"SHORT: {pc}/5 — contexto ({g.reason or 'no cumple gates'})"


@dataclass
class LongRead:
    """Mirror long-side read — INFORMATIONAL CONTEXT ONLY (never a mandate)."""
    flag: bool
    z_oversold: bool
    mean_reverting: bool
    funding_crowded_short: bool
    capitulating: bool
    capitulation_reasons: list[str]
    note: str


def long_read(g: AltGate, lower_lows: Optional[bool], k: dict[str, float]) -> LongRead:
    """Compute the mirror long-viability read from the SAME metrics the gates use.

    Fires when the structure is the mirror of a clean short:
        z strongly NEGATIVE (oversold, z ≤ −z_floor)  AND
        Hurst < 0.5 (mean-reverting — reuses the gate's hurst_ok)  AND
        NO downside-capitulation blow-off (mirror of the up-squeeze guard)  AND
        funding ≤ 0 (shorts crowded → squeeze-up fuel).

    This NEVER touches the five gates or their thresholds — it only reads
    metrics already computed by ``evaluate_name_gates`` plus a local lower-lows
    check, and is surfaced as context so the human is not blindly shorting an
    oversold bounce candidate.
    """
    if not g.data_ok:
        return LongRead(False, False, False, False, False, [],
                        "LONG: n/d (data insuficiente)")
    z_oversold = bool(g.z is not None and g.z <= -k["z_floor"])
    mean_reverting = bool(g.hurst_ok)  # same Hurst≤cutoff the short gate uses
    funding_crowded_short = bool(g.funding is not None and g.funding <= 0.0)

    # Downside-capitulation guard — the exact MIRROR of the up-side squeeze:
    #   oversold-AND-FALLING (RSI≤(100−overbought) WHILE lower-lows) = down blow-off
    #   parabolic DOWN (≤ −parabolic_pct over K bars)
    # Either means "still capitulating" → a long is catching a falling knife.
    cap: list[str] = []
    low_rsi_th = 100.0 - k["overbought_rsi"]
    if g.rsi is not None and g.rsi <= low_rsi_th and lower_lows is True:
        cap.append(f"RSI {g.rsi:.0f}≤{low_rsi_th:.0f}+LL (capitulación)")
    if g.pct_k is not None and g.pct_k <= -k["parabolic_pct"]:
        cap.append(f"caída parabólica {g.pct_k:.0f}%/{int(k['hh_lookback_bars'])}b")
    capitulating = bool(cap)

    flag = bool(z_oversold and mean_reverting and funding_crowded_short and not capitulating)

    if flag:
        note = (
            f"LONG context: z {_fmt_z(g.z)} sobrevendido + Hurst {_fmt_hurst(g.hurst)} "
            f"reversión + funding≤0 (shorts cargados → fuel de squeeze-up), sin capitulación"
        )
    elif z_oversold and capitulating:
        note = "LONG: no viable — sobrevendido pero CAPITULANDO (" + "/".join(cap) + ")"
    elif z_oversold and not funding_crowded_short:
        note = "LONG: no viable — sobrevendido pero funding>0 (no hay shorts cargados)"
    elif z_oversold and not mean_reverting:
        note = "LONG: no viable — sobrevendido pero Hurst trending (no reversión)"
    else:
        note = "LONG: no viable (no sobrevendido)"
    return LongRead(
        flag=flag, z_oversold=z_oversold, mean_reverting=mean_reverting,
        funding_crowded_short=funding_crowded_short, capitulating=capitulating,
        capitulation_reasons=cap, note=note,
    )


# ─── Per-name screener state (z-persistence over the FULL universe) ──────────
# Mirrors unlock_monitor.unlock_alt_state but in its OWN table so the watchlist
# trigger state is never touched. Advanced by the scheduler (advance_state=True);
# /unlockcheck and /check are PURE READS (advance_state=False).
def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS screener_alt_state (
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


def load_screen_state() -> dict[str, dict[str, Any]]:
    c = _conn()
    try:
        rows = c.execute("SELECT * FROM screener_alt_state").fetchall()
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


def save_screen_state(ticker: str, z_streak: int,
                      funding_last: Optional[float], oi_last: Optional[float]) -> None:
    c = _conn()
    try:
        c.execute(
            """
            INSERT INTO screener_alt_state (ticker, z_streak, funding_last, oi_last, updated_at)
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


def _reset_for_tests() -> None:
    try:
        c = _conn()
        c.execute("DELETE FROM screener_alt_state")
        c.commit()
        c.close()
    except Exception:  # noqa: BLE001
        pass


# ─── Universe assembly (HL + Variational, deduped by ticker) ─────────────────
@dataclass
class VenueInfo:
    ticker: str
    on_hl: bool
    on_var: bool
    hl_vol_usd: Optional[float]      # HL 24h notional volume (USD)
    var_vol_usd: Optional[float]     # Variational 24h volume (USD)
    hl_funding: Optional[float]
    hl_oi: Optional[float]           # HL open interest (base units)
    var_funding_ann: Optional[float] # Variational annualized funding %

    @property
    def venues(self) -> list[str]:
        v: list[str] = []
        if self.on_hl:
            v.append("HL")
        if self.on_var:
            v.append("VAR")
        return v

    @property
    def venue_label(self) -> str:
        return "+".join(self.venues) if self.venues else "—"

    @property
    def liquidity_note(self) -> str:
        """Best-liquidity venue by 24h USD volume (apples-to-apples). Soft."""
        hv = self.hl_vol_usd or 0.0
        vv = self.var_vol_usd or 0.0
        if self.on_hl and self.on_var:
            if hv <= 0 and vv <= 0:
                return "liq n/d"
            return f"mejor liq: {'HL' if hv >= vv else 'VAR'}"
        if self.on_hl:
            return "liq: HL"
        if self.on_var:
            return "liq: VAR"
        return "liq n/d"


async def fetch_hl_ctx_full() -> dict[str, VenueInfo]:
    """{COIN: VenueInfo(on_hl=True, …)} for every NON-delisted HL perp, with
    funding / OI / markPx-derived 24h USD volume. {} on miss. Never raises.

    This is the SINGLE critical call that defines the HL universe — if it 429s,
    the whole HL side would otherwise vanish and every name be mislabeled
    'VAR-only'. So it retries with backoff before giving up."""
    data = None
    retries = fetch_retries()
    for attempt in range(retries + 1):
        try:
            data = await _hl_post({"type": "metaAndAssetCtxs"})
            break
        except Exception as exc:  # noqa: BLE001
            if attempt < retries:
                await asyncio.sleep(0.6 * (attempt + 1))
                continue
            log.warning("screener: HL metaAndAssetCtxs n/d (%s)", exc)
            return {}
    try:
        meta, ctxs = data[0], data[1]
        universe = meta.get("universe", [])
    except Exception as exc:  # noqa: BLE001
        log.warning("screener: HL metaAndAssetCtxs malformed (%s)", exc)
        return {}
    out: dict[str, VenueInfo] = {}
    for asset, ctx in zip(universe, ctxs):
        if not isinstance(asset, dict) or not isinstance(ctx, dict):
            continue
        if asset.get("isDelisted"):
            continue
        name = str(asset.get("name", "")).strip().upper()
        if not name:
            continue
        out[name] = VenueInfo(
            ticker=name, on_hl=True, on_var=False,
            hl_vol_usd=_f(ctx.get("dayNtlVlm")),
            var_vol_usd=None,
            hl_funding=_f(ctx.get("funding")),
            hl_oi=_f(ctx.get("openInterest")),
            var_funding_ann=None,
        )
    return out


async def build_universe() -> tuple[dict[str, VenueInfo], list[str]]:
    """Union of HL + Variational tickers, deduped. Returns (venue_map, notes)."""
    notes: list[str] = []
    venue_map = await fetch_hl_ctx_full()
    if not venue_map:
        notes.append("Universo HL no fetchable — screener degradado.")

    # Merge Variational (the SAME source /variationalfunding uses).
    try:
        var_markets = await _var.fetch_markets()
    except Exception as exc:  # noqa: BLE001
        var_markets = []
        notes.append(f"Variational n/d ({type(exc).__name__}) — venues solo HL.")
    for m in var_markets:
        t = m.ticker.strip().upper()
        if t in venue_map:
            vi = venue_map[t]
            vi.on_var = True
            vi.var_vol_usd = m.volume_24h
            vi.var_funding_ann = m.annualized_pct
        else:
            venue_map[t] = VenueInfo(
                ticker=t, on_hl=False, on_var=True,
                hl_vol_usd=None, var_vol_usd=m.volume_24h,
                hl_funding=None, hl_oi=None, var_funding_ann=m.annualized_pct,
            )
    return venue_map, notes


# ─── Per-asset evaluation (reuses evaluate_name_gates verbatim) ──────────────
@dataclass
class ScreenRow:
    ticker: str
    sector: str
    venue_label: str
    liquidity_note: str
    gate: Optional[AltGate]          # None only when no HL candle source at all
    data_ok: bool
    pass_count: int
    score: float
    short_verdict: str
    long: LongRead
    excluded_reason: str             # set when data_ok is False

    @property
    def is_go_candidate(self) -> bool:
        return self.data_ok and self.pass_count == 5 and not (self.gate and self.gate.squeeze_flag)


async def _evaluate_one(
    vi: VenueInfo,
    k: dict[str, float],
    prev_state: dict[str, dict[str, Any]],
    btc_closes: Optional[list[float]],
    *,
    advance_state: bool,
    sem: asyncio.Semaphore,
) -> ScreenRow:
    """Evaluate ONE asset through the existing five-gate engine + score + long read."""
    ticker = vi.ticker
    sector = sector_of(ticker)

    # Candle source is HL only. Variational-only listings have no 4h candles →
    # data-quality gate fails by construction → excluded (never fabricated).
    closes: Optional[list[float]] = None
    if vi.on_hl:
        retries = fetch_retries()
        for attempt in range(retries + 1):
            async with sem:
                closes = await fetch_4h_closes(ticker, int(k["z_lookback_bars"]) + 6)
            if closes:
                break
            if attempt < retries:
                # Backoff OUTSIDE the semaphore so a retry never blocks peers;
                # absorbs transient HL 429/500 without ever fabricating candles.
                await asyncio.sleep(0.5 * (attempt + 1))

    funding = vi.hl_funding
    oi = vi.hl_oi
    prev = prev_state.get(ticker, {})
    z_streak_prev = int(prev.get("z_streak", 0) or 0)
    funding_prev = prev.get("funding_last")
    oi_prev = prev.get("oi_last")

    # cointegration proxy (CONTEXT ONLY — never gates)
    corr = (
        rolling_corr_vs_btc(closes, btc_closes, int(k["corr_lookback_bars"]))
        if (closes and btc_closes) else None
    )

    gate = evaluate_name_gates(
        ticker, sector, closes, funding, k,
        z_streak_prev=z_streak_prev, funding_prev=funding_prev,
        oi=oi, oi_prev=oi_prev, corr=corr, repairing=None,
    )

    if advance_state:
        try:
            save_screen_state(ticker, gate.z_streak, funding, oi)
        except Exception:  # noqa: BLE001
            log.exception("screener: save_screen_state failed for %s", ticker)

    lower_lows = made_lower_lows(closes, int(k["hh_lookback_bars"])) if closes else None
    lr = long_read(gate, lower_lows, k)

    excluded = ""
    if not gate.data_ok:
        if not vi.on_hl:
            excluded = "VAR-only, sin velas 4h en HL"
        else:
            excluded = f"data {gate.coverage * 100:.0f}%<{k['data_min_coverage'] * 100:.0f}%"

    return ScreenRow(
        ticker=ticker, sector=sector, venue_label=vi.venue_label,
        liquidity_note=vi.liquidity_note, gate=gate, data_ok=gate.data_ok,
        pass_count=short_pass_count(gate), score=short_score(gate),
        short_verdict=short_verdict(gate), long=lr, excluded_reason=excluded,
    )


@dataclass
class ScreenResult:
    ts_utc: str
    ranked: list[ScreenRow]          # data_ok, sorted most→least shortable
    long_context: list[ScreenRow]    # subset where long.flag fired
    excluded: list[ScreenRow]        # data-insufficient (not scored)
    universe_size: int
    n_hl: int
    n_var: int
    n_both: int
    notes: list[str]
    constants: dict[str, float] = field(default_factory=dict)


async def compute_screen(advance_state: bool = False) -> ScreenResult:
    """Assemble the universe, run the five-gate engine over EVERY asset, rank by
    shortability, and collect the long-context + data-insufficient buckets.

    NEVER raises. ``advance_state=True`` (scheduler) persists each name's
    z-persistence streak over the whole universe so 5/5 can accrue exactly the
    way the watchlist does today; /unlockcheck and /check pass ``advance_state=
    False`` (pure read)."""
    k = constants()
    venue_map, notes = await build_universe()
    prev_state = load_screen_state()

    # Count venue breakdown.
    n_hl = sum(1 for v in venue_map.values() if v.on_hl)
    n_var = sum(1 for v in venue_map.values() if v.on_var)
    n_both = sum(1 for v in venue_map.values() if v.on_hl and v.on_var)

    # BTC closes once (for the context-only cointegration proxy).
    btc_closes = await fetch_4h_closes(BTC_COIN, int(k["corr_lookback_bars"]) + 6)

    # Evaluate every asset (bounded concurrency on the HL candle fetches).
    items = list(venue_map.values())
    cap = max_assets()
    if cap and len(items) > cap:
        # Keep the most-liquid first so a cap never silently drops majors.
        items.sort(key=lambda v: (v.hl_vol_usd or v.var_vol_usd or 0.0), reverse=True)
        items = items[:cap]
        notes.append(f"Universo limitado a {cap} por SCREENER_MAX_ASSETS (más líquidos primero).")

    sem = asyncio.Semaphore(fetch_concurrency())
    rows = await asyncio.gather(*[
        _evaluate_one(vi, k, prev_state, btc_closes, advance_state=advance_state, sem=sem)
        for vi in items
    ])

    ranked = [r for r in rows if r.data_ok]
    ranked.sort(key=lambda r: r.score, reverse=True)
    excluded = [r for r in rows if not r.data_ok]
    excluded.sort(key=lambda r: r.ticker)
    long_context = [r for r in ranked if r.long.flag]
    # Most-oversold first inside the long bucket (deterministic).
    long_context.sort(key=lambda r: (r.gate.z if r.gate and r.gate.z is not None else 0.0))

    return ScreenResult(
        ts_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        ranked=ranked, long_context=long_context, excluded=excluded,
        universe_size=len(venue_map), n_hl=n_hl, n_var=n_var, n_both=n_both,
        notes=notes, constants=k,
    )


async def advance_universe_state() -> int:
    """SILENT scheduler hook: advance z-persistence over the full universe so the
    screen can reach 5/5 over time. Emits NOTHING (R-SILENT safe). Returns the
    number of ranked assets for the log. Never raises."""
    try:
        res = await compute_screen(advance_state=True)
        return len(res.ranked)
    except Exception:  # noqa: BLE001
        log.exception("screener: advance_universe_state failed (non-fatal)")
        return 0


async def check_single(ticker: str) -> tuple[Optional[ScreenRow], str]:
    """Run the SAME five-gate engine on ONE requested token. Returns
    (row, status) where status ∈ {"ok", "not_tradeable", "no_data"}. Pure read."""
    want = (ticker or "").strip().upper().lstrip("$")
    if not want:
        return None, "not_tradeable"
    k = constants()
    venue_map, _notes = await build_universe()
    vi = venue_map.get(want)
    if vi is None:
        return None, "not_tradeable"
    prev_state = load_screen_state()
    btc_closes = await fetch_4h_closes(BTC_COIN, int(k["corr_lookback_bars"]) + 6) if vi.on_hl else None
    sem = asyncio.Semaphore(1)
    row = await _evaluate_one(vi, k, prev_state, btc_closes, advance_state=False, sem=sem)
    return row, ("ok" if row.data_ok else "no_data")


# ─── Formatting ──────────────────────────────────────────────────────────────
_DISCLAIMERS = [
    "PRE-FILTRO de alta precisión — el bot NO selecciona tokens. Un 5/5 + AiPear es decisión 100% tuya.",
    "z y Hurst ESTIMADOS de velas 4h; cointegración = PROXY de contexto (NO gatea); ASI no siempre fetchable.",
    "squeeze = exclusión INVIOLABLE en gating Y ranking — un nombre en squeeze rankea ABAJO aunque tenga z alto.",
    "LONG = solo contexto. Mandato del fondo = HYPE-core long + libro short de alts; longs en alts son tácticos/tu decisión + AiPear.",
    "Liquidez = por volumen USD 24h (soft). Listings VAR-only sin velas 4h en HL → EXCLUIDOS por data-quality.",
]


def _short_gate_detail(g: AltGate, k: dict[str, float]) -> str:
    """Full per-gate one-liner for a single ranked row (reused by /check)."""
    cutoff = hurst_count_cutoff(k)
    ztag = "✅" if g.z_ok else "❌"
    htag = "✅" if g.hurst_ok else "❌"
    sqtag = "✅" if not g.squeeze_flag else "❌"
    ftag = "✅" if g.funding_ok else "❌"
    sqtxt = "clear" if not g.squeeze_flag else "/".join(g.squeeze_reasons)
    parts = [
        f"z {_fmt_z(g.z)}≥+{k['z_floor']:.2f}{ztag}",
        f"Hurst {_fmt_hurst(g.hurst)}≤{cutoff:.2f}{htag}",
        f"squeeze {sqtxt}{sqtag}",
        f"fund {_fmt_funding(g.funding_sign)}{ftag}",
    ]
    if g.z_floor_ok and not g.z_persistent:
        parts[0] += f"(persist {g.z_streak}/{int(k['z_persist_readings'])})"
    return " | ".join(parts)


def _ranked_block(r: ScreenRow, rank: int, k: dict[str, float]) -> str:
    g = r.gate
    mark = "🎯" if r.is_go_candidate else ("🚫" if (g and g.squeeze_flag) else "·")
    head = (
        f"{rank:>2}. {mark} {r.ticker:<7} [{r.sector}] {r.venue_label} · {r.liquidity_note} "
        f"— {r.pass_count}/5"
    )
    detail = "      " + _short_gate_detail(g, k) if g else ""
    verdict = f"      → {r.short_verdict}"
    lines = [head, detail, verdict]
    if r.long.flag:
        lines.append(f"      🟢 {r.long.note}")
    return "\n".join([ln for ln in lines if ln])


def format_screen(res: ScreenResult, top_n: Optional[int] = None) -> str:
    """Render /unlockcheck — universal short/long screener, sectioned + compressed
    tail so send_long_message can paginate it under Telegram's limit."""
    k = res.constants
    n = top_n if top_n is not None else top_n_default()
    cutoff = hurst_count_cutoff(k)
    n_go = sum(1 for r in res.ranked if r.is_go_candidate)

    lines = [
        "🔭 R-SCREEN — screener universal SHORT/LONG (PRE-FILTRO 5-gates)",
        f"{res.ts_utc}",
        "",
        f"Universo: {res.universe_size} perps · "
        f"qualifican {len(res.ranked)} / excluidos x data {len(res.excluded)}",
        f"Venues: HL {res.n_hl} · VAR {res.n_var} · ambos {res.n_both}",
        f"5/5 GO candidates: {n_go} · LONG-context: {len(res.long_context)}",
        "",
        "── SUB-GATES (un nombre es 5/5 solo si pasa los 5) ──",
        f"  1) data ≥{k['data_min_coverage'] * 100:.0f}% velas | "
        f"2) z ≥+{k['z_floor']:.2f} persist ≥{int(k['z_persist_readings'])} | "
        f"3) Hurst ≤{cutoff:.2f} | 4) squeeze CLEAR | 5) funding ≥0",
        "  Ranking = pass-count → (squeeze al fondo) → z+ alto / Hurst bajo.",
        "",
        f"🔴 MÁS SHORTEABLES (top {min(n, len(res.ranked))} en detalle, más→menos shorteable)",
    ]
    if not res.ranked:
        lines.append("  (ninguno con datos suficientes)")
    for i, r in enumerate(res.ranked[:n], start=1):
        lines.append(_ranked_block(r, i, k))

    # Compressed tail — ticker + pass-count + one-line reason, no full breakdown.
    tail = res.ranked[n:]
    if tail:
        lines += ["", f"⚪ RESTO / NO-GO ({len(tail)}) — compacto"]
        for i, r in enumerate(tail, start=n + 1):
            g = r.gate
            reason = (
                "squeeze:" + "/".join(g.squeeze_reasons) if (g and g.squeeze_flag)
                else (g.reason if g and g.reason else "—")
            )
            lines.append(f"  {i:>3}. {r.ticker:<7} {r.pass_count}/5 · {r.venue_label} — {reason}")

    # LONG context — separated, with the explicit tactical disclaimer.
    lines += ["", f"🟢 LONG CONTEXT ({len(res.long_context)}) — táctico / tu decisión + AiPear"]
    lines.append("  NO es mandato. Mandato = HYPE-core long + libro short de alts.")
    if not res.long_context:
        lines.append("  (ningún nombre dispara la lectura long ahora)")
    for r in res.long_context:
        g = r.gate
        lines.append(
            f"  • {r.ticker:<7} [{r.sector}] {r.venue_label} — "
            f"z {_fmt_z(g.z if g else None)} · Hurst {_fmt_hurst(g.hurst if g else None)} · "
            f"fund {_fmt_funding(g.funding_sign if g else None)} · {r.long.note}"
        )

    # Data-insufficient bucket.
    if res.excluded:
        lines += ["", f"🚫 DATA INSUFICIENTE ({len(res.excluded)}) — no rankeados"]
        # compress: list tickers with reason, capped display so it never explodes
        show = res.excluded[:40]
        for r in show:
            lines.append(f"  · {r.ticker:<7} {r.venue_label} — {r.excluded_reason}")
        if len(res.excluded) > len(show):
            lines.append(f"  … +{len(res.excluded) - len(show)} más")

    if res.notes:
        lines += ["", "Notas:"]
        for nx in res.notes:
            lines.append(f"  • {nx}")

    lines += ["", "Confianza / proxies:"]
    for d in _DISCLAIMERS:
        lines.append(f"  • {d}")
    return "\n".join(lines)


def format_check(row: Optional[ScreenRow], status: str, ticker: str,
                 k: Optional[dict[str, float]] = None) -> str:
    """Render /check <TICKER> — single-row short + long verdict, same engine."""
    want = (ticker or "").strip().upper().lstrip("$")
    if status == "not_tradeable" or row is None:
        return (
            f"🔭 /check {want}\n\n"
            f"❌ {want} no es tradeable en Hyperliquid ni en Variational "
            f"(o el ticker no existe). No hay nada que screenear."
        )
    k = k or row.gate and constants() or constants()
    g = row.gate

    head = [
        f"🔭 /check {row.ticker} [{row.sector}] — {row.venue_label} · {row.liquidity_note}",
        "",
    ]
    if status == "no_data" or not row.data_ok:
        head += [
            f"🚫 DATA INSUFICIENTE — {row.excluded_reason}.",
            "Sin ≥90% de velas 4h en HL no se puntúa (no fabricamos score).",
            "",
            "SHORT: n/d · LONG: n/d",
        ]
        head += ["", "Confianza / proxies:"]
        for d in _DISCLAIMERS:
            head.append(f"  • {d}")
        return "\n".join(head)

    cutoff = hurst_count_cutoff(k)
    body = [
        "── SUB-GATES ──",
        f"  1) data {g.coverage * 100:.0f}% ≥{k['data_min_coverage'] * 100:.0f}% "
        + ("✅" if g.data_ok else "❌"),
        f"  2) z {_fmt_z(g.z)} ≥+{k['z_floor']:.2f} persist {g.z_streak}/{int(k['z_persist_readings'])} "
        + ("✅" if g.z_ok else "❌"),
        f"  3) Hurst {_fmt_hurst(g.hurst)} ≤{cutoff:.2f} " + ("✅" if g.hurst_ok else "❌"),
        f"  4) squeeze " + ("clear ✅" if not g.squeeze_flag else "❌ " + "/".join(g.squeeze_reasons)),
        f"  5) funding {_fmt_funding(g.funding_sign)} " + ("✅" if g.funding_ok else "❌"),
        f"  (RSI {g.rsi:.0f} · Δ{int(k['hh_lookback_bars'])}b {('%+.0f%%' % g.pct_k) if g.pct_k is not None else 'n/d'} · "
        f"coint~{('%.2f' % g.corr) if g.corr is not None else 'n/d'} ctx)",
        "",
        f"🔴 {row.short_verdict}",
        f"🟢 {row.long.note}",
    ]
    out = head + body + ["", "Confianza / proxies:"]
    for d in _DISCLAIMERS:
        out.append(f"  • {d}")
    return "\n".join(out)
