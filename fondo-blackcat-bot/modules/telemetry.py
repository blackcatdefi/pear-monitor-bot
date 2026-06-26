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
# HIP-3 builder-deployed perp dex prefix (e.g. ``xyz`` in ``xyz:SP500``). HL
# convention is a LOWER-case deployer name + UPPER-case symbol; the colon is the
# only structural separator we accept inside a token.
_VALID_DEPLOYER = re.compile(r"^[a-z0-9]{1,15}$")
# Natural-language connectors a human types between tickers ("SP500, NVDA, and
# HOOD"). These must NEVER be parsed as a ticker or consume one of the 8 slots.
_CONNECTORS = {"AND", "Y", "&", "PLUS", "+"}
# Preferred HIP-3 deployer when a BARE symbol resolves to several dexes. ``xyz``
# is the reference equities/RWA venue the fund uses (and the one the user types
# explicitly). Ties beyond this fall back to the most-liquid listing.
_PREFERRED_DEPLOYER = "xyz"


def _norm_coin(coin: str) -> str:
    """Canonical HL info-API coin string. Plain symbols are upper-cased
    (BTC, hype→HYPE); HIP-3 ``deployer:SYMBOL`` keeps the LOWER-case deployer
    prefix and UPPER-cases only the symbol (HL is case-sensitive here:
    ``xyz:SP500`` works, ``XYZ:SP500`` / ``xyz:sp500`` do NOT)."""
    coin = (coin or "").strip()
    if ":" in coin:
        dep, _, sym = coin.partition(":")
        return f"{dep.strip().lower()}:{sym.strip().upper()}"
    return coin.upper()

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
      2. natural-language connectors ("and", "y", "&", "plus") are DROPPED
         silently — they never become a ticker nor consume a slot,
      3. HIP-3 ``deployer:symbol`` tokens (e.g. ``xyz:SP500``) are parsed into a
         canonical lower-deployer / upper-symbol coin string and KEPT (the colon
         is NOT treated as invalid); plain tickers behave exactly as before,
      4. each part passed through ``_sanitize_untrusted`` (prompt-injection
         defang) and validated against its strict charset — invalid tokens are
         DROPPED (never silently coerced), with a note,
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
    connectors: list[str] = []

    for chunk in re.split(r"[\s,]+", joined):
        if not chunk:
            continue
        # Connector check on the RAW chunk (upper) BEFORE charset validation so a
        # bare "&"/"+" is labelled a connector, not "formato inválido".
        if chunk.strip().upper() in _CONNECTORS:
            connectors.append(chunk.strip()[:8])
            continue
        canon = _parse_one_token(chunk)
        if canon is None:
            # Keep a short, already-sanitized echo so the note can't be a vector.
            safe = _sanitize_untrusted(chunk).upper().strip()[:12]
            dropped.append(safe or "?")
            continue
        if canon in seen:
            continue
        seen.add(canon)
        out.append(canon)

    if connectors:
        notes.append(f"conectores ignorados: {', '.join(connectors[:8])}")
    if dropped:
        notes.append(f"ignorados (formato inválido): {', '.join(dropped[:8])}")
    if len(out) > MAX_TICKERS:
        notes.append(f"máx {MAX_TICKERS} por llamada — usando los primeros {MAX_TICKERS}")
        out = out[:MAX_TICKERS]
    return out, notes


def _parse_one_token(chunk: str) -> Optional[str]:
    """Parse ONE whitespace/comma-free token into a canonical coin string, or
    ``None`` if it is not a well-formed ticker.

    - ``deployer:symbol`` → ``deployer.lower():SYMBOL.upper()`` (HIP-3). Each side
      is sanitized + charset-checked independently so the colon survives intact.
    - plain ``symbol`` → ``SYMBOL.upper()`` (unchanged legacy behaviour).
    Tokens with more than one colon, or with an invalid deployer/symbol part, are
    rejected (``None``).

    SECURITY: the WHOLE chunk is run through ``_sanitize_untrusted`` FIRST. That
    guard only recognises a role marker like ``system:`` when its colon is intact
    (``system:DROP`` → ``[redacted-injection]DROP``), so splitting before
    sanitizing would smuggle it past. A legitimate HIP-3 token (``xyz:SP500``)
    survives sanitization byte-for-byte; a defanged injection loses its colon and
    then fails the strict charset below → dropped."""
    cleaned = _sanitize_untrusted(chunk).strip()
    if ":" in cleaned:
        dep_raw, _, sym_raw = cleaned.partition(":")
        if ":" in sym_raw:  # >1 colon → malformed
            return None
        dep = dep_raw.strip().lower().lstrip("$").strip()
        sym = sym_raw.strip().upper().lstrip("$").strip()
        if not _VALID_DEPLOYER.match(dep) or not _VALID_TICKER.match(sym):
            return None
        return f"{dep}:{sym}"
    cleaned = cleaned.upper().lstrip("$").strip()
    if not cleaned or not _VALID_TICKER.match(cleaned):
        return None
    return cleaned


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


# ─── HIP-3 builder-deployed dex discovery + per-dex context ───────────────────
async def fetch_perp_dexes() -> list[str]:
    """Names of all HIP-3 builder-deployed perp dexes (e.g. ``xyz``, ``flx`` …).
    The leading ``null`` entry (HL core) is dropped. [] on any failure."""
    try:
        dexs = await _hl_post({"type": "perpDexs"})
    except Exception as exc:  # noqa: BLE001
        log.warning("telemetry: perpDexs n/d (%s)", exc)
        return []
    out: list[str] = []
    for d in dexs or []:
        if isinstance(d, dict):
            nm = str(d.get("name", "")).strip().lower()
            if nm:
                out.append(nm)
    return out


async def fetch_dex_ctx(dex: str) -> dict[str, dict[str, Any]]:
    """{EXACT_NAME: ctx} for ONE HIP-3 deployer via ``metaAndAssetCtxs`` with the
    ``dex`` argument. Keys are the exact HL coin strings (e.g. ``xyz:SP500``) —
    NOT upper-cased — because HL is case-sensitive for HIP-3 coins. {} on any
    failure; callers degrade per-metric to n/d."""
    dex = (dex or "").strip().lower()
    if not dex:
        return {}
    try:
        data = await _hl_post({"type": "metaAndAssetCtxs", "dex": dex})
        meta, ctxs = data[0], data[1]
        universe = meta.get("universe", [])
    except Exception as exc:  # noqa: BLE001
        log.warning("telemetry: HIP-3 metaAndAssetCtxs n/d for dex=%s (%s)", dex, exc)
        return {}
    out: dict[str, dict[str, Any]] = {}
    for asset, ctx in zip(universe, ctxs):
        if not isinstance(asset, dict) or not isinstance(ctx, dict):
            continue
        name = str(asset.get("name", "")).strip()  # exact case, e.g. 'xyz:SP500'
        if name:
            out[name] = ctx
    return out


# ─── Venue resolution (HIP-3 deployer → HL core → VAR) ────────────────────────
@dataclass
class Resolution:
    """How ONE requested coin maps to a venue + how telemetry should query it.

    ``query_coin`` is the exact string handed to the HL info API and used to look
    up the merged ctx map. ``kind`` ∈ {hl_core, hip3, hip3_missing, unknown}:
    ``unknown`` means "not on HL core nor any HIP-3 dex" → let the R-SCREEN gate
    engine decide VAR-vs-nothing. ``run_gate`` is False for HIP-3 (the 5-gate
    engine only covers HL-core + VAR), so squeeze/z/Hurst show n/d with a note."""
    query_coin: str
    kind: str
    venue_label: Optional[str] = None
    deployer: Optional[str] = None
    run_gate: bool = True
    note: Optional[str] = None


def _pick_hip3(symbol: str, index: dict[str, list[tuple[str, dict]]]
               ) -> Optional[tuple[str, list[str]]]:
    """Resolve a BARE symbol (no deployer) to a single HIP-3 coin. Prefers the
    reference deployer ``xyz``; otherwise the most-liquid listing (max 24h vol).
    Returns ``(canonical_coin, all_listings)`` or ``None`` if unlisted."""
    cands = index.get(symbol.upper())
    if not cands:
        return None
    listings = [name for name, _ in cands]
    for name, _ctx in cands:
        if name.split(":", 1)[0] == _PREFERRED_DEPLOYER:
            return name, listings
    best = max(cands, key=lambda nc: _f(nc[1].get("dayNtlVlm")) or 0.0)
    return best[0], listings


async def resolve_markets(coins: list[str], core_ctx_map: dict[str, dict[str, Any]]
                          ) -> tuple[dict[str, Resolution], dict[str, dict[str, Any]]]:
    """Route every requested coin to a venue and gather any HIP-3 context needed.

    Order per coin (TASK 3): (a) explicit ``deployer:symbol`` → that HIP-3
    deployer; (b) HL core perp; (c) bare-symbol HIP-3 fallback (across all
    deployers); (d) ``unknown`` → R-SCREEN gate decides VAR / not-found.

    HIP-3 dex contexts are fetched ONCE per run (only when actually needed): the
    explicit deployers named in the request, plus every deployer if any plain
    ticker is missing from HL core (so the bare-symbol index can be built).
    Returns ``(resolutions, hip3_ctx)`` where ``hip3_ctx`` is keyed by exact coin
    string and merges into the ctx map the build path looks up."""
    explicit_deployers: set[str] = set()
    need_bare_index = False
    for c in coins:
        if ":" in c:
            explicit_deployers.add(c.split(":", 1)[0].lower())
        elif c.upper() not in core_ctx_map:
            need_bare_index = True

    hip3_ctx: dict[str, dict[str, Any]] = {}
    dexes_to_fetch: set[str] = set(explicit_deployers)
    if need_bare_index:
        all_dexes = await fetch_perp_dexes()
        dexes_to_fetch |= set(all_dexes)
    for dex in sorted(dexes_to_fetch):
        hip3_ctx.update(await fetch_dex_ctx(dex))

    # Bare-symbol index across whatever HIP-3 ctx we pulled.
    index: dict[str, list[tuple[str, dict]]] = {}
    for name, ctx in hip3_ctx.items():
        if ":" in name:
            index.setdefault(name.split(":", 1)[1].upper(), []).append((name, ctx))

    resolutions: dict[str, Resolution] = {}
    for c in coins:
        if ":" in c:
            dep = c.split(":", 1)[0].lower()
            if c in hip3_ctx:
                resolutions[c] = Resolution(
                    query_coin=c, kind="hip3", venue_label=f"HIP-3 {dep}",
                    deployer=dep, run_gate=False,
                    note="HIP-3: squeeze/z/H n/d (motor R-SCREEN cubre solo HL-core/VAR)",
                )
            else:
                resolutions[c] = Resolution(
                    query_coin=c, kind="hip3_missing", venue_label=f"HIP-3 {dep}?",
                    deployer=dep, run_gate=False,
                    note=f"deployer HIP-3 '{dep}' no lista el símbolo o no expone datos",
                )
            continue
        up = c.upper()
        if up in core_ctx_map:
            resolutions[c] = Resolution(query_coin=up, kind="hl_core", run_gate=True)
            continue
        picked = _pick_hip3(up, index)
        if picked is not None:
            name, listings = picked
            dep = name.split(":", 1)[0]
            alt = [x for x in listings if x != name]
            note = f"símbolo {up} → {name} (HIP-3 {dep})"
            if alt:
                note += f"; también en {', '.join(alt[:5])}"
            note += " · squeeze/z/H n/d (motor R-SCREEN HL-core/VAR)"
            resolutions[c] = Resolution(
                query_coin=name, kind="hip3", venue_label=f"HIP-3 {dep}",
                deployer=dep, run_gate=False, note=note,
            )
            continue
        # Not on HL core nor any HIP-3 dex → let the gate engine probe VAR.
        resolutions[c] = Resolution(query_coin=up, kind="unknown", run_gate=True)
    return resolutions, hip3_ctx


async def fetch_funding_avg_7d(coin: str) -> tuple[Optional[float], int]:
    """Arithmetic mean of HL ``fundingHistory`` hourly rates over the trailing
    7 days. Returns (mean_rate, n_samples). (None, 0) on miss — never 0-filled."""
    coin = _norm_coin(coin)
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
    coin = _norm_coin(coin)
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
    coin = _norm_coin(coin)
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
async def build_one(ticker: str, ctx_map: dict[str, dict[str, Any]],
                    resolution: Optional["Resolution"] = None) -> TokenTelemetry:
    """Assemble one ticker's telemetry. Every metric block is independently
    guarded: a failure in one NEVER blanks the others, and nothing is faked.

    ``resolution`` (new, optional) is the venue-routing overlay produced by
    ``resolve_markets``: it supplies the exact ``query_coin`` (so HIP-3 markets
    like ``xyz:SP500`` are fetched correctly), the venue label, whether to run the
    R-SCREEN gate, and an explicit note. When ``None`` the legacy behaviour is
    preserved byte-for-byte (plain ticker, gate always run) so existing callers /
    tests are unaffected."""
    t = TokenTelemetry(ticker=ticker)
    q = resolution.query_coin if resolution is not None else ticker
    ctx = ctx_map.get(q)
    t.on_hl = ctx is not None

    # 1. Funding (live from ctx; 7d avg from fundingHistory)
    if ctx is not None:
        t.funding_live = _f(ctx.get("funding"))
    avg, n = await fetch_funding_avg_7d(q)
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
    low = await fetch_low_7d(q)
    t.low7d = low
    if mark is not None and low is not None and low > 0:
        t.dist_low_pct = (mark - low) / low * 100.0

    # 4. Top-of-book depth
    depth = await fetch_depth(q)
    t.bid_05, t.ask_05 = depth["bid_05"], depth["ask_05"]
    t.bid_10, t.ask_10 = depth["bid_10"], depth["ask_10"]

    # 5. Squeeze + fails-first + z + Hurst (reused engine) — skipped for HIP-3,
    #    where the 5-gate R-SCREEN engine (HL-core + VAR only) does not apply.
    run_gate = True if resolution is None else resolution.run_gate
    gate_vl: Optional[str] = None
    if run_gate:
        gate = await fetch_gate(q)
        t.squeeze_state = gate["squeeze_state"]
        t.fails_first = gate["fails_first"]
        t.z = gate["z"]
        t.hurst = gate["hurst"]
        gate_vl = gate["venue_label"]

    # Venue label: explicit resolution wins; else gate-derived; else plain HL.
    if resolution is not None and resolution.venue_label:
        t.venue_label = resolution.venue_label
    elif gate_vl:
        t.venue_label = gate_vl
    elif t.on_hl:
        t.venue_label = "HL"

    # Explicit messaging (TASK 5) — only on the routed path so legacy is intact.
    if resolution is not None:
        _annotate_explicit(t, resolution, gate_vl)
    return t


def _annotate_explicit(t: TokenTelemetry, resolution: "Resolution",
                       gate_vl: Optional[str]) -> None:
    """Append a single, human-readable reason a market has the data (or n/d) it
    does, so the user never sees a bare ``formato inválido`` / unexplained n/d:
      - HIP-3 resolved (with or without exposed metrics) → router note,
      - plain ticker found only on Variational → say VAR exposes no liq data,
      - plain ticker found nowhere → say not on HL core / HIP-3 / VAR."""
    kind = resolution.kind
    if kind in ("hip3", "hip3_missing"):
        if resolution.note:
            t.notes.append(resolution.note)
        return
    # hl_core: nothing to explain (full data path).
    if kind == "hl_core":
        return
    # unknown: the gate engine just told us where (if anywhere) it lives.
    vl = (gate_vl or "").upper()
    if "VAR" in vl:
        t.venue_label = gate_vl or "VAR"
        t.notes.append("mercado en Variational (VAR); el HL info API no expone "
                       "funding/OI/depth → n/d")
    else:
        t.venue_label = "—"
        t.notes.append("no encontrado en HL core / HIP-3 / VAR")


async def build_telemetry(tickers: list[str]) -> list[TokenTelemetry]:
    """Assemble telemetry for all requested tickers concurrently. The shared HL
    client de-dupes the single ``metaAndAssetCtxs`` call across them and rate
    limits the rest; nothing here ever raises.

    Routing: HL core ctx is fetched once; ``resolve_markets`` then pulls any
    HIP-3 deployer contexts needed and tags each coin's venue. The merged ctx map
    is keyed by HL-core UPPER names AND exact HIP-3 coin strings, so ``build_one``
    looks each up by its ``query_coin``."""
    core_ctx_map = await fetch_ctx_map()
    try:
        resolutions, hip3_ctx = await resolve_markets(tickers, core_ctx_map)
    except Exception:  # noqa: BLE001 — routing must never break telemetry
        log.exception("telemetry: resolve_markets failed (non-fatal) — HL-core only")
        resolutions, hip3_ctx = {}, {}
    merged = {**core_ctx_map, **hip3_ctx}
    return await asyncio.gather(
        *[build_one(t, merged, resolutions.get(t)) for t in tickers]
    )


# ─── Screener-attached telemetry (R-SCREEN-TELEMETRY) ─────────────────────────
# The standalone /telemetry path (above) re-runs the FULL 5-gate engine per
# ticker via ``fetch_gate``→``check_single`` (which itself rebuilds the universe
# + re-fetches candles). When telemetry is attached to the internal screener
# output, that gate work is ALREADY DONE: every GO candidate is a
# ``universal_screener.ScreenRow`` carrying a fully-evaluated ``AltGate`` (z,
# Hurst, squeeze, funding). So we read squeeze/fails-first/z/Hurst straight off
# ``row.gate`` and fire ONLY the incremental per-token calls
# (fundingHistory 7d, candleSnapshot 4h low, l2Book depth) — sharing one
# ``metaAndAssetCtxs`` map and a per-run cache so no token is ever fetched twice.

async def _cached(cache: Optional[dict], key: Any, factory):
    """Await ``factory()`` once per ``key`` within a run, caching the result so a
    repeated ticker (e.g. across /reporte sections) never re-fetches. ``cache``
    None → no caching (always fetch)."""
    if cache is None:
        return await factory()
    if key in cache:
        return cache[key]
    val = await factory()
    cache[key] = val
    return val


async def build_one_from_row(row: Any, ctx_map: dict[str, dict[str, Any]],
                             cache: Optional[dict] = None) -> TokenTelemetry:
    """Assemble telemetry for ONE pre-screened ``ScreenRow`` WITHOUT re-running
    the gate engine: squeeze/fails-first/z/Hurst are read from the row's already
    computed ``AltGate``; only fundingHistory/candle-low/depth fire (deduped via
    ``cache``). Every metric is independently guarded → granular n/d, nothing
    faked. The ticker is re-run through the SAME injection guard even though it
    came from the internal screener (defence in depth)."""
    raw_ticker = str(getattr(row, "ticker", "") or "")
    ticker = _sanitize_untrusted(raw_ticker).upper().strip().lstrip("$").strip()
    t = TokenTelemetry(ticker=ticker or (raw_ticker[:12] or "?"))
    if not ticker or not _VALID_TICKER.match(ticker):
        t.notes.append("ticker inválido — telemetría omitida")
        return t

    ctx = ctx_map.get(ticker)
    t.on_hl = ctx is not None

    # 1. Funding (live from ctx; 7d avg incremental, cached)
    if ctx is not None:
        t.funding_live = _f(ctx.get("funding"))
    avg, n = await _cached(cache, ("fund7d", ticker),
                           lambda: fetch_funding_avg_7d(ticker))
    t.funding_avg7d, t.funding_samples = avg, n

    # 2. OI notional + 24h vol + ratio (from shared ctx)
    mark = _f(ctx.get("markPx")) if ctx is not None else None
    t.mark = mark
    if ctx is not None:
        oi_base = _f(ctx.get("openInterest"))
        if oi_base is not None and mark is not None:
            t.oi_usd = oi_base * mark
        t.vol24h_usd = _f(ctx.get("dayNtlVlm"))
        if t.oi_usd is not None and t.vol24h_usd not in (None, 0):
            t.oi_vol_ratio = t.oi_usd / t.vol24h_usd

    # 3. Distance from 7d low (incremental, cached)
    low = await _cached(cache, ("low7d", ticker), lambda: fetch_low_7d(ticker))
    t.low7d = low
    if mark is not None and low is not None and low > 0:
        t.dist_low_pct = (mark - low) / low * 100.0

    # 4. Top-of-book depth (incremental, cached)
    depth = await _cached(cache, ("depth", ticker), lambda: fetch_depth(ticker))
    t.bid_05, t.ask_05 = depth["bid_05"], depth["ask_05"]
    t.bid_10, t.ask_10 = depth["bid_10"], depth["ask_10"]

    # 5. Squeeze + fails-first + z + Hurst — READ from the precomputed gate
    #    (NO check_single, NO engine re-run, NO candle re-fetch).
    g = getattr(row, "gate", None)
    if g is not None:
        t.squeeze_state = "CLEAR" if not g.squeeze_flag else "/".join(g.squeeze_reasons or ["squeeze"])
        t.fails_first = _fails_first(g)
        t.z = g.z
        t.hurst = g.hurst
    vl = getattr(row, "venue_label", None)
    if vl:
        t.venue_label = vl
    elif t.on_hl:
        t.venue_label = "HL"
    return t


async def _safe_build_from_row(row: Any, ctx_map: dict[str, dict[str, Any]],
                               cache: Optional[dict] = None) -> TokenTelemetry:
    """``build_one_from_row`` wrapped so a hard failure on ONE GO can never break
    the screener render or the report — degrades to a gate-only block."""
    try:
        return await build_one_from_row(row, ctx_map, cache)
    except Exception as exc:  # noqa: BLE001
        log.warning("telemetry: build_one_from_row n/d for %s (%s)",
                    getattr(row, "ticker", "?"), exc)
        t = TokenTelemetry(ticker=str(getattr(row, "ticker", "?")))
        g = getattr(row, "gate", None)
        if g is not None:
            t.squeeze_state = "CLEAR" if not g.squeeze_flag else "/".join(g.squeeze_reasons or ["squeeze"])
            t.fails_first = _fails_first(g)
            t.z, t.hurst = g.z, g.hurst
        t.notes.append("telemetría parcial (fallo de feed)")
        return t


async def render_go_telemetry(res: Any, *, cap: int = MAX_TICKERS,
                              ctx_map: Optional[dict] = None,
                              cache: Optional[dict] = None,
                              ) -> tuple[dict[str, str], Optional[str], int]:
    """Build + render the compact telemetry block for the GO candidates of a
    ``ScreenResult``. GO = ``row.is_go_candidate`` (5/5, non-squeeze), taken in
    the engine's existing ranking order. Returns ``(blocks, note, n_go)`` where
    ``blocks`` maps ticker→rendered monospace block (≤``cap`` entries), ``note``
    is a "top N of M" line when more than ``cap`` GO exist (else None), and
    ``n_go`` is the total GO count. NEVER raises."""
    try:
        ranked = list(getattr(res, "ranked", []) or [])
        go = [r for r in ranked if getattr(r, "is_go_candidate", False)]
        n_go = len(go)
        if n_go == 0:
            return {}, None, 0
        note: Optional[str] = None
        if n_go > cap:
            go = go[:cap]
            note = f"telemetría: top {cap} de {n_go} GO"
        if ctx_map is None:
            ctx_map = await fetch_ctx_map()
        if cache is None:
            cache = {}
        tels = await asyncio.gather(
            *[_safe_build_from_row(r, ctx_map, cache) for r in go]
        )
        blocks = {t.ticker: format_token_compact(t) for t in tels}
        return blocks, note, n_go
    except Exception:  # noqa: BLE001
        log.exception("telemetry: render_go_telemetry failed (non-fatal)")
        return {}, None, 0


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


def _short_flag_compact(rate: Optional[float]) -> str:
    """Compact PAYS/RECEIVES/FLAT for a SHORT (cobra/paga/flat/n-d)."""
    if rate is None:
        return "n/d"
    if rate > 0:
        return "cobra"
    if rate < 0:
        return "paga"
    return "flat"


def _low_mark(v: Optional[float]) -> str:
    return f"{v:g}" if v is not None else "n/d"


def format_token_compact(t: TokenTelemetry) -> str:
    """Dense 3-line telemetry block, designed to sit DIRECTLY under a screener GO
    candidate line. Carries the same five metrics as ``format_token`` (funding
    now/7d, OI vs vol, distance from 7d low, depth ±0.5%/±1%, squeeze/
    fails-first/z/Hurst) with zero prose padding. n/d everywhere a feed missed."""
    fl = _short_flag_compact(t.funding_live)
    f7 = _short_flag_compact(t.funding_avg7d)
    n7 = f" n{t.funding_samples}" if t.funding_samples else ""
    dist = f"+{t.dist_low_pct:.1f}%" if t.dist_low_pct is not None else "n/d"
    return "\n".join([
        f"   📟 fund {_pct(t.funding_live)}h {_ann(t.funding_live)} {fl} · "
        f"7d {_pct(t.funding_avg7d)}h {_ann(t.funding_avg7d)} {f7}{n7}",
        f"      OI {_usd(t.oi_usd)}/vol {_usd(t.vol24h_usd)} {_ratio(t.oi_vol_ratio)} · "
        f"vs7dlow {dist} (low {_low_mark(t.low7d)}/mark {_low_mark(t.mark)}) · "
        f"depth ±0.5 b{_usd(t.bid_05)}/a{_usd(t.ask_05)} ±1 b{_usd(t.bid_10)}/a{_usd(t.ask_10)}",
        f"      sq {t.squeeze_state or 'n/d'} · fails {t.fails_first or 'n/d'} · "
        f"z {_fmt_z(t.z)} · H {_fmt_hurst(t.hurst)}"
        + ("" if not t.notes else " · " + "; ".join(t.notes)),
    ])


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
