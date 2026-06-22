"""R-TELEMETRY — per-token telemetry block for AiPear basket sizing.

WHAT THIS IS — A READ-ONLY METRIC AGGREGATOR, NEVER A SECOND ENGINE
    ``/telemetry TICKER1 TICKER2 …`` (space- or comma-separated, 1-8 tickers)
    pulls a dense, monospace telemetry block per token straight from the
    Hyperliquid info API, routed through the SAME shared rate-limited + TTL
    cached client (``modules.hl_client`` via ``unlock_monitor._hl_post``) the
    R-SCREEN screener already uses. It feeds AiPear basket-sizing decisions, so
    the standard is ACCURACY OVER COMPLETENESS: every metric is fetched in its
    own guarded path and any single feed that fails prints ``n/d`` for THAT
    metric only — we never fabricate a number and never zero-fill.

METRICS PER TICKER (all from POST https://api.hyperliquid.xyz/info)
    1. Funding — live hourly rate (``metaAndAssetCtxs`` ctx ``funding``) + the
       trailing 7-day arithmetic mean (``fundingHistory``). Reported as hourly
       percent AND annualized percent, flagged PAYS / RECEIVES for a SHORT
       (short + funding > 0 = receives, < 0 = pays, 0 = flat).
    2. OI vs 24h volume — OI notional = ``openInterest`` × ``markPx``; 24h vol =
       ``dayNtlVlm`` (already USD); ratio OI/vol24h.
    3. Distance from 7-day low — ``candleSnapshot`` 4h over the trailing 7d, min
       ``l`` (low); current ``markPx`` as percent above that low.
    4. Top-of-book depth — ``l2Book``; resting notional (sz × px) summed on bid
       and ask sides within ±0.5% and ±1.0% of mid → four USD numbers.
    5. Squeeze + fails-first — REUSES the R-SCREEN 5-gate engine verbatim
       (``universal_screener.check_single``): squeeze state (CLEAR or reason),
       the first gate to fail in evaluation order (data → z → Hurst → squeeze →
       funding, or "none — 5/5 GO"), plus the z and Hurst already computed.

SECURITY — every user-supplied ticker is run through ``_sanitize_untrusted``
    (the SAME prompt-injection guard applied to scraped X content) and then
    hard-restricted to ``[A-Z0-9]`` so a hostile "ticker" can neither inject
    instructions nor smuggle control structure into the output block or logs.

The bot NEVER selects tokens, sizes, or trades — this is telemetry the human +
AiPear read. Pure read; never advances any screener persistence counter.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from modules.unlock_monitor import _f, _fmt_hurst, _fmt_z, _hl_post
from modules.universal_screener import check_single
from modules.x_intel import _sanitize_untrusted

log = logging.getLogger(__name__)

MAX_TICKERS = 8
# Strict post-sanitization ticker charset. HL perp symbols are upper-alnum
# (e.g. BTC, HYPE, kPEPE → "KPEPE", 1000BONK). Anything else is dropped.
_VALID_TICKER = re.compile(r"^[A-Z0-9]{1,15}$")

# HL funding is charged HOURLY (see funding_tracker.funding_8h_bps).
_HOURS_PER_YEAR = 24.0 * 365.0
_SEVEN_DAYS_MS = 7 * 24 * 3600 * 1000


# ─── Ticker parsing + sanitization ───────────────────────────────────────────
def parse_tickers(raw: Any) -> tuple[list[str], list[str]]:
    """Parse a space/comma-separated ticker request into a clean, deduped,
    capped, injection-sanitized list.

    Returns ``(tickers, notes)``. ``raw`` may be a list of args (Telegram
    ``context.args``) or a single string. Every candidate is:
      1. split on whitespace AND commas,
      2. upper-cased, ``$``/whitespace-stripped,
      3. passed through ``_sanitize_untrusted`` (prompt-injection defang),
      4. validated against ``[A-Z0-9]{1,15}`` — invalid tokens are DROPPED
         (never silently coerced), with a note,
      5. de-duplicated preserving order,
      6. capped at ``MAX_TICKERS`` (overflow noted, never truncated silently).
    """
    if raw is None:
        return [], []
    if isinstance(raw, (list, tuple)):
        joined = " ".join(str(x) for x in raw)
    else:
        joined = str(raw)

    notes: list[str] = []
    out: list[str] = []
    seen: set[str] = set()
    dropped: list[str] = []

    for chunk in re.split(r"[\s,]+", joined):
        if not chunk:
            continue
        # Sanitize FIRST (defang injection), then normalize shape.
        cleaned = _sanitize_untrusted(chunk).upper().strip().lstrip("$").strip()
        if not cleaned or not _VALID_TICKER.match(cleaned):
            # Keep a short, already-sanitized echo so the note can't be a vector.
            dropped.append(cleaned[:12] or "?")
            continue
        if cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)

    if dropped:
        notes.append(f"ignorados (formato inválido): {', '.join(dropped[:8])}")
    if len(out) > MAX_TICKERS:
        notes.append(f"máx {MAX_TICKERS} por llamada — usando los primeros {MAX_TICKERS}")
        out = out[:MAX_TICKERS]
    return out, notes


# ─── Per-token telemetry container (Optional everywhere → n/d on miss) ────────
@dataclass
class TokenTelemetry:
    ticker: str
    on_hl: bool = False
    venue_label: str = "n/d"
    # 1. funding (hourly fractions; None = n/d)
    funding_live: Optional[float] = None
    funding_avg7d: Optional[float] = None
    funding_samples: int = 0
    # 2. OI vs vol
    oi_usd: Optional[float] = None
    vol24h_usd: Optional[float] = None
    oi_vol_ratio: Optional[float] = None
    # 3. distance from 7d low
    mark: Optional[float] = None
    low7d: Optional[float] = None
    dist_low_pct: Optional[float] = None
    # 4. top-of-book depth (USD notional)
    bid_05: Optional[float] = None
    ask_05: Optional[float] = None
    bid_10: Optional[float] = None
    ask_10: Optional[float] = None
    # 5. squeeze + fails-first (reused R-SCREEN engine)
    squeeze_state: Optional[str] = None
    fails_first: Optional[str] = None
    z: Optional[float] = None
    hurst: Optional[float] = None
    notes: list[str] = field(default_factory=list)


# ─── HL info fetchers (each isolated; None/empty on any failure) ──────────────
async def fetch_ctx_map() -> dict[str, dict[str, Any]]:
    """{COIN: ctx} from HL ``metaAndAssetCtxs`` (one shared, cached call for the
    whole request). {} on any failure — callers degrade per-metric to n/d."""
    try:
        data = await _hl_post({"type": "metaAndAssetCtxs"})
        meta, ctxs = data[0], data[1]
        universe = meta.get("universe", [])
    except Exception as exc:  # noqa: BLE001
        log.warning("telemetry: metaAndAssetCtxs n/d (%s)", exc)
        return {}
    out: dict[str, dict[str, Any]] = {}
    for asset, ctx in zip(universe, ctxs):
        if not isinstance(asset, dict) or not isinstance(ctx, dict):
            continue
        name = str(asset.get("name", "")).strip().upper()
        if name:
            out[name] = ctx
    return out


async def fetch_funding_avg_7d(coin: str) -> tuple[Optional[float], int]:
    """Arithmetic mean of HL ``fundingHistory`` hourly rates over the trailing
    7 days. Returns (mean_rate, n_samples). (None, 0) on miss — never 0-filled."""
    coin = (coin or "").strip().upper()
    start_ms = int(time.time() * 1000) - _SEVEN_DAYS_MS
    try:
        rows = await _hl_post(
            {"type": "fundingHistory", "coin": coin, "startTime": start_ms}
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("telemetry: fundingHistory n/d for %s (%s)", coin, exc)
        return None, 0
    rates = [
        _f(r.get("fundingRate"))
        for r in (rows or [])
        if isinstance(r, dict)
    ]
    rates = [r for r in rates if r is not None]
    if not rates:
        return None, 0
    return sum(rates) / len(rates), len(rates)


async def fetch_low_7d(coin: str) -> Optional[float]:
    """Minimum 4h candle low over the trailing 7 days from ``candleSnapshot``.
    None on miss. 7d of 4h candles = 42 bars."""
    coin = (coin or "").strip().upper()
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - _SEVEN_DAYS_MS
    try:
        candles = await _hl_post({
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": "4h",
                    "startTime": start_ms, "endTime": now_ms},
        })
        lows = [_f(c.get("l")) for c in (candles or []) if isinstance(c, dict)]
        lows = [x for x in lows if x is not None and x > 0]
        return min(lows) if lows else None
    except Exception as exc:  # noqa: BLE001
        log.warning("telemetry: candleSnapshot low n/d for %s (%s)", coin, exc)
        return None


async def fetch_depth(coin: str) -> dict[str, Optional[float]]:
    """Top-of-book resting notional (Σ sz×px, USD) on each side within ±0.5% and
    ±1.0% of mid from ``l2Book``. Keys bid_05/ask_05/bid_10/ask_10; each None on
    miss — partial books still report whatever side succeeds."""
    empty: dict[str, Optional[float]] = {
        "bid_05": None, "ask_05": None, "bid_10": None, "ask_10": None
    }
    coin = (coin or "").strip().upper()
    try:
        book = await _hl_post({"type": "l2Book", "coin": coin})
        levels = book.get("levels") if isinstance(book, dict) else None
        bids, asks = levels[0], levels[1]
    except Exception as exc:  # noqa: BLE001
        log.warning("telemetry: l2Book n/d for %s (%s)", coin, exc)
        return empty

    def _best(side: list) -> Optional[float]:
        for lvl in side:
            px = _f(lvl.get("px")) if isinstance(lvl, dict) else None
            if px is not None and px > 0:
                return px
        return None

    best_bid = _best(bids or [])
    best_ask = _best(asks or [])
    if best_bid is None or best_ask is None:
        return empty
    mid = (best_bid + best_ask) / 2.0
    if mid <= 0:
        return empty

    def _side_notional(side: list, *, is_bid: bool, band: float) -> Optional[float]:
        lo = mid * (1.0 - band)
        hi = mid * (1.0 + band)
        total = 0.0
        seen = False
        for lvl in side or []:
            if not isinstance(lvl, dict):
                continue
            px = _f(lvl.get("px"))
            sz = _f(lvl.get("sz"))
            if px is None or sz is None or px <= 0 or sz < 0:
                continue
            # bids sit at/below mid → within band means px >= lo; asks → px <= hi
            if (is_bid and px >= lo) or (not is_bid and px <= hi):
                total += px * sz
                seen = True
        return total if seen else 0.0

    return {
        "bid_05": _side_notional(bids, is_bid=True, band=0.005),
        "ask_05": _side_notional(asks, is_bid=False, band=0.005),
        "bid_10": _side_notional(bids, is_bid=True, band=0.010),
        "ask_10": _side_notional(asks, is_bid=False, band=0.010),
    }


# ─── Squeeze + fails-first via the EXISTING R-SCREEN engine ───────────────────
_GATE_ORDER = ("data", "z", "Hurst", "squeeze", "funding")


def _fails_first(gate) -> str:
    """First of the five gates that fails in canonical evaluation order, or
    'none — 5/5 GO' when all five pass. Reads ONLY the engine-computed booleans
    (never re-derives an indicator)."""
    if gate is None:
        return "n/d"
    checks = (
        ("data", gate.data_ok),
        ("z", gate.z_ok),
        ("Hurst", gate.hurst_ok),
        ("squeeze", not gate.squeeze_flag),
        ("funding", gate.funding_ok),
    )
    for name, ok in checks:
        if not ok:
            return name
    return "none — 5/5 GO"


async def fetch_gate(ticker: str) -> dict[str, Any]:
    """Run the SAME R-SCREEN five-gate engine on ONE ticker (pure read,
    advance_state=False) and extract squeeze/fails-first/z/Hurst. All n/d on
    any failure — telemetry of the other metrics still renders."""
    out: dict[str, Any] = {
        "squeeze_state": None, "fails_first": None,
        "z": None, "hurst": None, "venue_label": None,
    }
    try:
        row, status = await check_single(ticker)
    except Exception as exc:  # noqa: BLE001
        log.warning("telemetry: check_single n/d for %s (%s)", ticker, exc)
        return out
    if row is None:
        out["fails_first"] = "no tradeable (HL/VAR)"
        return out
    out["venue_label"] = row.venue_label
    g = row.gate
    if g is None:
        return out
    out["squeeze_state"] = "CLEAR" if not g.squeeze_flag else "/".join(g.squeeze_reasons or ["squeeze"])
    out["fails_first"] = _fails_first(g)
    out["z"] = g.z
    out["hurst"] = g.hurst
    return out


# ─── Per-token assembly (each metric independent → granular n/d) ──────────────
async def build_one(ticker: str, ctx_map: dict[str, dict[str, Any]]) -> TokenTelemetry:
    """Assemble one ticker's telemetry. Every metric block is independently
    guarded: a failure in one NEVER blanks the others, and nothing is faked."""
    t = TokenTelemetry(ticker=ticker)
    ctx = ctx_map.get(ticker)
    t.on_hl = ctx is not None

    # 1. Funding (live from ctx; 7d avg from fundingHistory)
    if ctx is not None:
        t.funding_live = _f(ctx.get("funding"))
    avg, n = await fetch_funding_avg_7d(ticker)
    t.funding_avg7d, t.funding_samples = avg, n

    # 2. OI notional + 24h vol + ratio
    mark = _f(ctx.get("markPx")) if ctx is not None else None
    t.mark = mark
    if ctx is not None:
        oi_base = _f(ctx.get("openInterest"))
        if oi_base is not None and mark is not None:
            t.oi_usd = oi_base * mark
        t.vol24h_usd = _f(ctx.get("dayNtlVlm"))
        if t.oi_usd is not None and t.vol24h_usd not in (None, 0):
            t.oi_vol_ratio = t.oi_usd / t.vol24h_usd

    # 3. Distance from 7d low
    low = await fetch_low_7d(ticker)
    t.low7d = low
    if mark is not None and low is not None and low > 0:
        t.dist_low_pct = (mark - low) / low * 100.0

    # 4. Top-of-book depth
    depth = await fetch_depth(ticker)
    t.bid_05, t.ask_05 = depth["bid_05"], depth["ask_05"]
    t.bid_10, t.ask_10 = depth["bid_10"], depth["ask_10"]

    # 5. Squeeze + fails-first + z + Hurst (reused engine)
    gate = await fetch_gate(ticker)
    t.squeeze_state = gate["squeeze_state"]
    t.fails_first = gate["fails_first"]
    t.z = gate["z"]
    t.hurst = gate["hurst"]
    if gate["venue_label"]:
        t.venue_label = gate["venue_label"]
    elif t.on_hl:
        t.venue_label = "HL"
    return t


async def build_telemetry(tickers: list[str]) -> list[TokenTelemetry]:
    """Assemble telemetry for all requested tickers concurrently. The shared HL
    client de-dupes the single ``metaAndAssetCtxs`` call across them and rate
    limits the rest; nothing here ever raises."""
    ctx_map = await fetch_ctx_map()
    return await asyncio.gather(*[build_one(t, ctx_map) for t in tickers])


# ─── Rendering (dense monospace, one block per ticker) ────────────────────────
def _pct(v: Optional[float], digits: int = 4) -> str:
    return f"{v * 100:.{digits}f}%" if v is not None else "n/d"


def _ann(v: Optional[float]) -> str:
    return f"{v * _HOURS_PER_YEAR * 100:+.1f}%" if v is not None else "n/d"


def _usd(v: Optional[float]) -> str:
    if v is None:
        return "n/d"
    a = abs(v)
    if a >= 1e9:
        return f"${v / 1e9:.2f}B"
    if a >= 1e6:
        return f"${v / 1e6:.2f}M"
    if a >= 1e3:
        return f"${v / 1e3:.1f}K"
    return f"${v:.0f}"


def _ratio(v: Optional[float]) -> str:
    return f"{v:.2f}x" if v is not None else "n/d"


def _short_funding_flag(rate: Optional[float]) -> str:
    """PAYS / RECEIVES / FLAT for a SHORT position. Short receives when funding
    is positive (longs pay shorts), pays when negative."""
    if rate is None:
        return "n/d"
    if rate > 0:
        return "RECEIVES (short)"
    if rate < 0:
        return "PAYS (short)"
    return "FLAT"


def format_token(t: TokenTelemetry) -> str:
    """One compact monospace block for a single ticker."""
    lines = [
        f"━━ {t.ticker}  ·  {t.venue_label} ━━",
        f"funding live : {_pct(t.funding_live)} h · {_ann(t.funding_live)} APR · {_short_funding_flag(t.funding_live)}",
        f"funding 7d   : {_pct(t.funding_avg7d)} h · {_ann(t.funding_avg7d)} APR"
        + (f" · n={t.funding_samples}" if t.funding_samples else "")
        + f" · {_short_funding_flag(t.funding_avg7d)}",
        f"OI / vol24h  : OI {_usd(t.oi_usd)} · vol {_usd(t.vol24h_usd)} · {_ratio(t.oi_vol_ratio)}",
        f"vs 7d low    : low {(f'{t.low7d:g}' if t.low7d is not None else 'n/d')} · "
        f"mark {(f'{t.mark:g}' if t.mark is not None else 'n/d')} · "
        f"{(f'+{t.dist_low_pct:.1f}%' if t.dist_low_pct is not None else 'n/d')}",
        f"depth ±0.5%  : bid {_usd(t.bid_05)} · ask {_usd(t.ask_05)}",
        f"depth ±1.0%  : bid {_usd(t.bid_10)} · ask {_usd(t.ask_10)}",
        f"squeeze      : {t.squeeze_state or 'n/d'}",
        f"fails-first  : {t.fails_first or 'n/d'} · z {_fmt_z(t.z)} · H {_fmt_hurst(t.hurst)}",
    ]
    for nx in t.notes:
        lines.append(f"  · {nx}")
    return "\n".join(lines)


def format_telemetry(tokens: list[TokenTelemetry], parse_notes: Optional[list[str]] = None) -> str:
    """Render the full grouped response: UTC timestamp header + one block per
    ticker, all monospace, no prose padding."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    header = [
        "📟 TELEMETRY — Hyperliquid info API (per-token, AiPear sizing)",
        f"{ts} · {len(tokens)} ticker(s)",
    ]
    for nx in (parse_notes or []):
        header.append(f"  · {nx}")
    blocks = [format_token(t) for t in tokens]
    body = "\n\n".join(blocks) if blocks else "(sin tickers válidos)"
    footer = (
        "n/d = feed no disponible (nunca fabricado/0-fill) · "
        "funding hourly→APR ×8760 · short: +funding=cobra, −=paga · "
        "squeeze/fails-first/z/Hurst = motor R-SCREEN. Telemetría, no mandato."
    )
    return "\n".join(header) + "\n\n```\n" + body + "\n```\n" + footer
