"""R-VARIATIONAL — Variational Omni perp funding scanner ("Farm the DUMP").

STRATEGY THIS SERVES
    Fondo Black Cat shorts coins whose annualized funding went *extremely*
    negative (≤ -500%) and has since reverted toward the mean (~±50%). The
    SHORT signal is the **reversion**, not the extreme. This module is the
    keyless, read-only data layer that powers:
        /variationalfunding   — scan all perps for funding ≤ threshold
        /variationalalerts     — watch a ticker for the mean-reversion trigger

DATA SOURCE
    Variational Omni public read-only REST API (no key, no custody):
        GET {BASE}/metadata/stats
    Returns platform-wide stats + a `listings` array. Per listing we use:
        ticker, mark_price, volume_24h, funding_rate, funding_interval_s,
        open_interest.{long,short}_open_interest
    Docs: https://docs.variational.io/technical-documentation/api
    Rate limits: 10 req / 10 s per IP, 1000 req/min global. One call returns
    EVERY market, so a single 60s-cached request covers both features.

ANNUALIZATION (documented + empirically verified 2026-05-30)
    The API returns `funding_rate` as the rate **per funding interval**, in
    PERCENT units already (NOT a 0–1 fraction). Verified against the two most
    liquid markets: BTC funding_rate=0.086232 with funding_interval_s=28800
    (8h) → 0.086232 % × (1 yr / 8h) = 0.086232 × 1095 ≈ 94 % annualized, which
    is the same order of magnitude as Hyperliquid's live BTC funding (~11 %
    baseline, elevated on a thinner venue). Reading it as a fraction (×100)
    would give an impossible 9 442 % for BTC, so the percent-per-interval
    reading is the correct one.

        intervals_per_year = SECONDS_PER_YEAR / funding_interval_s
        annualized_pct      = funding_rate * intervals_per_year

    This matches the task's own example formulas (8h_rate × 3 × 365,
    hourly_rate × 24 × 365): 3 × 365 = 1095 = SECONDS_PER_YEAR / 28800.

ROBUSTNESS
    Every network call is wrapped; on failure callers get a clear
    ``VariationalError`` they can render as "n/a — <reason>" without crashing
    the bot. Missing/malformed per-listing fields are skipped, never faked.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

try:
    import httpx
    _HTTPX_OK = True
except Exception:  # noqa: BLE001 — keep import-safe even if httpx is missing
    _HTTPX_OK = False

log = logging.getLogger(__name__)

# ─── Config ──────────────────────────────────────────────────────────────────
BASE_URL = os.getenv(
    "VARIATIONAL_API_BASE",
    "https://omni-client-api.prod.ap-northeast-1.variational.io",
).rstrip("/")
STATS_PATH = "/metadata/stats"

SECONDS_PER_YEAR = 365 * 24 * 3600  # 31_536_000

_CACHE_TTL_S = 60.0
_HTTP_TIMEOUT_S = 20.0
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def funding_threshold() -> float:
    """Annualized-% threshold for /variationalfunding (default -500)."""
    raw = os.getenv("VARIATIONAL_FUNDING_THRESHOLD", "-500").strip()
    try:
        return float(raw)
    except ValueError:
        log.warning("bad VARIATIONAL_FUNDING_THRESHOLD=%r → -500", raw)
        return -500.0


class VariationalError(Exception):
    """Raised when Variational data cannot be fetched/parsed. Message is
    safe to surface to the user as the ``n/a`` reason."""


# ─── Models ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class VariationalMarket:
    ticker: str
    funding_rate: float          # raw per-interval rate (percent units)
    funding_interval_s: int      # seconds per funding interval
    annualized_pct: float        # annualized funding, percent
    mark_price: Optional[float]
    volume_24h: Optional[float]
    open_interest_usd: Optional[float]


# ─── Pure math (unit-tested) ─────────────────────────────────────────────────
def annualize_funding(funding_rate: float, funding_interval_s: int) -> float:
    """Annualize a per-interval funding rate (percent units) to percent/yr.

    See module docstring for the empirical verification. ``funding_rate`` is
    taken as percent-per-interval (the value the API returns directly).

    Raises ``ValueError`` on a non-positive interval (cannot annualize).
    """
    if funding_interval_s is None or funding_interval_s <= 0:
        raise ValueError(f"funding_interval_s must be > 0, got {funding_interval_s!r}")
    intervals_per_year = SECONDS_PER_YEAR / float(funding_interval_s)
    return float(funding_rate) * intervals_per_year


def _to_float(v: Any) -> Optional[float]:
    """Best-effort float; the API returns numbers as strings. Returns None on
    any failure so callers can render ``n/a`` instead of crashing."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf guard
        return None
    return f


def parse_listing(listing: dict[str, Any]) -> Optional[VariationalMarket]:
    """Parse one raw `listings[]` entry into a VariationalMarket.

    Returns ``None`` (skip) when the entry lacks the fields required to
    compute annualized funding — we never fabricate numbers.
    """
    if not isinstance(listing, dict):
        return None
    ticker = listing.get("ticker")
    fr = _to_float(listing.get("funding_rate"))
    iv = listing.get("funding_interval_s")
    try:
        iv = int(iv)
    except (TypeError, ValueError):
        iv = 0
    if not ticker or fr is None or iv <= 0:
        return None
    try:
        ann = annualize_funding(fr, iv)
    except ValueError:
        return None

    oi = listing.get("open_interest") or {}
    oi_usd: Optional[float] = None
    if isinstance(oi, dict):
        lo = _to_float(oi.get("long_open_interest")) or 0.0
        sh = _to_float(oi.get("short_open_interest")) or 0.0
        if lo or sh:
            oi_usd = lo + sh
    elif oi is not None:
        oi_usd = _to_float(oi)

    return VariationalMarket(
        ticker=str(ticker).upper(),
        funding_rate=fr,
        funding_interval_s=iv,
        annualized_pct=ann,
        mark_price=_to_float(listing.get("mark_price")),
        volume_24h=_to_float(listing.get("volume_24h")),
        open_interest_usd=oi_usd,
    )


def parse_stats(raw: dict[str, Any]) -> list[VariationalMarket]:
    """Parse the full /metadata/stats payload into VariationalMarket list."""
    if not isinstance(raw, dict):
        raise VariationalError("unexpected stats payload (not an object)")
    listings = raw.get("listings")
    if not isinstance(listings, list):
        raise VariationalError("stats payload has no 'listings' array")
    out: list[VariationalMarket] = []
    for entry in listings:
        m = parse_listing(entry)
        if m is not None:
            out.append(m)
    return out


# ─── Fetch + cache ───────────────────────────────────────────────────────────
# Simple in-memory TTL cache (NEVER browser storage). Keyed by nothing — the
# endpoint returns the whole market set, so one slot suffices.
_cache: dict[str, Any] = {"ts": 0.0, "markets": None, "raw_ts": None}
_lock = asyncio.Lock()


def _now() -> float:
    return time.time()


async def _http_get_stats() -> dict[str, Any]:
    if not _HTTPX_OK:
        raise VariationalError("httpx unavailable in runtime")
    url = f"{BASE_URL}{STATS_PATH}"
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": _UA, "Accept": "application/json"})
        if resp.status_code == 429:
            raise VariationalError("rate limited (HTTP 429) — try again shortly")
        if resp.status_code >= 500:
            raise VariationalError(f"Variational server error (HTTP {resp.status_code})")
        if resp.status_code != 200:
            raise VariationalError(f"unexpected HTTP {resp.status_code}")
        return resp.json()
    except VariationalError:
        raise
    except httpx.TimeoutException:
        raise VariationalError("request timed out")
    except httpx.HTTPError as exc:  # connection/transport errors
        raise VariationalError(f"network error ({type(exc).__name__})")
    except ValueError:  # JSON decode
        raise VariationalError("invalid JSON from Variational")


async def fetch_markets(force: bool = False) -> list[VariationalMarket]:
    """Return all parsed markets, cached for 60s. Raises VariationalError.

    Concurrency-safe: a single in-flight request is shared via ``_lock``.
    """
    async with _lock:
        if (
            not force
            and _cache["markets"] is not None
            and (_now() - _cache["ts"]) < _CACHE_TTL_S
        ):
            return _cache["markets"]  # type: ignore[return-value]
        raw = await _http_get_stats()
        markets = parse_stats(raw)
        _cache["markets"] = markets
        _cache["ts"] = _now()
        return markets


async def get_market(ticker: str) -> Optional[VariationalMarket]:
    """Return the market for ``ticker`` (case-insensitive) or None. Uses cache.

    Raises VariationalError only on a hard fetch failure; a successful fetch
    that simply doesn't list the ticker returns None.
    """
    want = (ticker or "").strip().upper()
    if not want:
        return None
    markets = await fetch_markets()
    for m in markets:
        if m.ticker == want:
            return m
    return None


def scan_negative_funding(
    markets: list[VariationalMarket], threshold: float
) -> list[VariationalMarket]:
    """Return markets with annualized funding ≤ threshold, most-negative first."""
    qualifying = [m for m in markets if m.annualized_pct <= threshold]
    return sorted(qualifying, key=lambda m: m.annualized_pct)


# ─── Formatting ──────────────────────────────────────────────────────────────
def _fmt_pct(v: float) -> str:
    return f"{v:,.1f}%"


def _fmt_usd_compact(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    try:
        a = abs(v)
        if a >= 1_000_000_000:
            return f"${v/1_000_000_000:.2f}B"
        if a >= 1_000_000:
            return f"${v/1_000_000:.2f}M"
        if a >= 1_000:
            return f"${v/1_000:.2f}K"
        return f"${v:,.2f}"
    except Exception:  # noqa: BLE001
        return "n/a"


def _fmt_price(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    if v >= 1:
        return f"${v:,.4f}".rstrip("0").rstrip(".")
    return f"${v:.8f}".rstrip("0").rstrip(".")


def format_funding_scan(
    qualifying: list[VariationalMarket],
    threshold: float,
    total_markets: int,
    ts_utc: Optional[str] = None,
) -> str:
    """Render the /variationalfunding Telegram message."""
    ts = ts_utc or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "🐱‍⬛ VARIATIONAL — Funding Scan (Farm the DUMP)",
        f"Umbral: funding anualizado ≤ {_fmt_pct(threshold)}",
        f"Mercados escaneados: {total_markets}  ·  {ts}",
        "",
    ]
    if not qualifying:
        lines.append(
            f"✅ Ningún activo por debajo de {_fmt_pct(threshold)} ahora mismo."
        )
        lines.append("La señal de SHORT es la REVERSIÓN, no el extremo —")
        lines.append("registrá un watch con /variationalalerts <TICKER> cuando aparezca.")
        return "\n".join(lines)

    lines.append(f"🔻 {len(qualifying)} activo(s) en zona extrema (más negativo primero):")
    lines.append("")
    for m in qualifying:
        lines.append(f"• {m.ticker}  {_fmt_pct(m.annualized_pct)} anual")
        details = (
            f"   mark {_fmt_price(m.mark_price)}  ·  "
            f"24h vol {_fmt_usd_compact(m.volume_24h)}  ·  "
            f"OI {_fmt_usd_compact(m.open_interest_usd)}"
        )
        lines.append(details)
    lines.append("")
    lines.append("Recordá: NO shortear el extremo. Registrá el watch y esperá")
    lines.append("la reversión (~mitad del baseline) con /variationalalerts <TICKER>.")
    return "\n".join(lines)
