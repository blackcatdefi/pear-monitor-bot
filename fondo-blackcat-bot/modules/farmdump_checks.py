"""R-FARMDUMP — automatic 5-check pre-trade filter for "Farm the DUMP" shorts.

WHAT THIS IS
    When a Variational mean-reversion watch fires (funding reverted to ~half the
    baseline extreme), Fondo Black Cat's rule is to run a mandatory 5-point short
    filter *before* entering. This module runs those checks automatically and
    produces a single GO / CAUTION / NO-GO verdict appended to the reversion
    alert, so BCD reads one message and decides.

    The verdict is a RECOMMENDATION ONLY. The bot never sizes or places an
    order — BCD makes the final call and executes manually.

THE 5 CHECKS (each → PASS / WARN / FAIL, with the actual number)
    1. Squeeze risk / funding not crowded  (HARD filter — funding source)
    2. Recent price action / 24h volatility (Hyperliquid 24h change)
    3. Open Interest vs Volume / liquidity   (Variational, HL fallback)
    4. Narrative priced / daily trend        (Hyperliquid daily candles → SMA)
    5. Documented before executing           (always — the audit one-liner)

VERDICT
    Computed from checks 1-4 (check 5 is a process gate, always documented):
        NO-GO  — any FAIL
        CAUTION — any WARN, no FAIL
        GO     — checks 1-4 all PASS

DATA SOURCES (all keyless, read-only, no custody, no keys)
    * Funding (check 1): the annualized funding already carried by the fired
      Variational watch (from modules.variational /metadata/stats). No extra call.
    * 24h % change (check 2): Hyperliquid `metaAndAssetCtxs` (markPx vs prevDayPx).
      Variational's /metadata/stats has no 24h-change field, so we fall back to
      Hyperliquid for the same ticker.
    * OI & 24h volume (check 3): primarily Variational (open_interest_usd,
      volume_24h carried on the market); Hyperliquid (dayNtlVlm, openInterest×mark)
      as a fallback when Variational omits them.
    * Daily trend (check 4): Hyperliquid `candleSnapshot` (1d). Method documented
      at `_eval_trend`.

ROBUSTNESS
    Every external call is wrapped. If a field is genuinely unavailable for an
    asset (e.g. not listed on Hyperliquid), that check degrades to WARN with the
    reason ``dato no disponible`` — we NEVER fabricate a number, and we NEVER
    crash the scheduler or the alert. A degraded check (WARN) pulls the verdict
    to at worst CAUTION; it can never silently flip a NO-GO into a GO.
"""
from __future__ import annotations

import asyncio
import logging
import os
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

# ─── Status constants ────────────────────────────────────────────────────────
PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

# Severity ordering so the verdict can take the worst status across checks.
_SEVERITY = {PASS: 0, WARN: 1, FAIL: 2}

GO = "GO"
CAUTION = "CAUTION"
NO_GO = "NO-GO"

_NA = "dato no disponible"

# ─── Hyperliquid keyless info endpoint ───────────────────────────────────────
HL_INFO_URL = os.getenv("HL_INFO_URL", "https://api.hyperliquid.xyz/info").rstrip("/")
_HTTP_TIMEOUT_S = 15.0
_CTX_CACHE_TTL_S = 60.0
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# metaAndAssetCtxs is one call covering every HL market → cache a single slot.
_ctx_cache: dict[str, Any] = {"ts": 0.0, "by_coin": None}
_ctx_lock = asyncio.Lock()


# ─── Env-tunable thresholds (read live so Railway overrides take effect) ─────
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


def thresholds() -> dict[str, float]:
    """Snapshot of every Farm-the-DUMP threshold (env-overridable)."""
    return {
        "funding_mean_ceil": _envf("FARMDUMP_FUNDING_MEAN_CEIL", -100.0),
        "funding_crowded_floor": _envf("FARMDUMP_FUNDING_CROWDED_FLOOR", -300.0),
        "funding_skip_high": _envf("FARMDUMP_FUNDING_SKIP_HIGH", 200.0),
        "uptrend_24h_warn": _envf("FARMDUMP_UPTREND_24H_WARN", 10.0),
        "uptrend_24h_fail": _envf("FARMDUMP_UPTREND_24H_FAIL", 20.0),
        "min_vol_usd": _envf("FARMDUMP_MIN_VOL_USD", 1_000_000.0),
        "trend_sma_days": float(_envi("FARMDUMP_TREND_SMA_DAYS", 7)),
        "trend_uptrend_pct": _envf("FARMDUMP_TREND_UPTREND_PCT", 10.0),
    }


# ─── Result models ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Check:
    n: int
    label: str
    status: str          # PASS | WARN | FAIL
    detail: str          # short human-readable reason w/ the actual number


@dataclass
class ChecksResult:
    ticker: str
    checks: list[Check] = field(default_factory=list)
    verdict: str = CAUTION
    doc_line: str = ""
    # Raw market numbers used by the checks, surfaced for the header line.
    price: Optional[float] = None
    chg_24h: Optional[float] = None
    oi_usd: Optional[float] = None
    vol_24h: Optional[float] = None

    @property
    def n_warn(self) -> int:
        return sum(1 for c in self.checks if c.status == WARN)

    @property
    def n_fail(self) -> int:
        return sum(1 for c in self.checks if c.status == FAIL)


# ─── Pure per-check logic (unit-tested, no network) ──────────────────────────
def eval_funding(current_funding: Optional[float], th: dict[str, float]) -> Check:
    """Check 1 — funding reverted toward mean, not still crowded / not overshoot.

    FAIL if still deeply negative (≤ crowded_floor) → squeeze risk; the whole
    point of the strategy is to wait for the reversion. FAIL if overshot to an
    extreme positive (≥ skip_high) → full reversal, skip-the-trade rule. PASS
    when funding is back near mean (mean_ceil ≤ f < skip_high). Partially
    reverted but still below mean_ceil → WARN.
    """
    label = "Funding revertido / no crowded"
    if current_funding is None:
        return Check(1, label, WARN, f"{_NA} (funding)")
    f = current_funding
    floor = th["funding_crowded_floor"]
    ceil = th["funding_mean_ceil"]
    high = th["funding_skip_high"]
    if f >= high:
        return Check(1, label, FAIL, f"overshoot +{f:,.0f}% (≥ +{high:,.0f}%), reversal completo → skip")
    if f <= floor:
        return Check(1, label, FAIL, f"aún {f:,.0f}% (≤ {floor:,.0f}%), crowded / squeeze risk")
    if f >= ceil:
        return Check(1, label, PASS, f"{f:,.0f}%, cerca de mean (≥ {ceil:,.0f}%)")
    return Check(1, label, WARN, f"{f:,.0f}%, revertido parcial (aún < {ceil:,.0f}%)")


def eval_price_action(chg_24h: Optional[float], th: dict[str, float]) -> Check:
    """Check 2 — entering a short into a vertical 24h pump is squeeze bait.

    FAIL when +%24h ≥ uptrend_24h_fail (ripping vertically). WARN when between
    warn and fail thresholds. PASS when stable or rolling over (below warn).
    """
    label = "Price action 24h"
    if chg_24h is None:
        return Check(2, label, WARN, f"{_NA} (24h)")
    warn = th["uptrend_24h_warn"]
    fail = th["uptrend_24h_fail"]
    sign = "+" if chg_24h >= 0 else ""
    if chg_24h >= fail:
        return Check(2, label, FAIL, f"{sign}{chg_24h:,.1f}% 24h, vertical → squeeze bait")
    if chg_24h >= warn:
        return Check(2, label, WARN, f"{sign}{chg_24h:,.1f}% 24h, subiendo fuerte")
    return Check(2, label, PASS, f"{sign}{chg_24h:,.1f}% 24h, estable / rolling over")


def eval_liquidity(
    vol_24h: Optional[float], oi_usd: Optional[float], th: dict[str, float]
) -> Check:
    """Check 3 — thin liquidity = self-liquidation / slippage risk.

    FAIL when 24h volume < min_vol_usd (illiquid). WARN when below 2× the floor
    (borderline) or when volume is unavailable. PASS when healthy. OI is shown
    for context (and used only if volume is missing entirely)."""
    label = "OI vs Volumen"
    min_vol = th["min_vol_usd"]
    if vol_24h is None and oi_usd is None:
        return Check(3, label, WARN, f"{_NA} (vol/OI)")
    # If volume is missing but OI exists, judge on OI against the same floor.
    metric = vol_24h if vol_24h is not None else oi_usd
    metric_name = "vol24h" if vol_24h is not None else "OI"
    oi_str = f", OI {_fmt_usd(oi_usd)}" if oi_usd is not None else ""
    if metric is None:
        return Check(3, label, WARN, f"{_NA} (vol/OI)")
    if metric < min_vol:
        return Check(3, label, FAIL, f"{metric_name} {_fmt_usd(metric)} (< {_fmt_usd(min_vol)}) ilíquido{oi_str}")
    if metric < 2 * min_vol:
        return Check(3, label, WARN, f"{metric_name} {_fmt_usd(metric)}, liquidez justa{oi_str}")
    return Check(3, label, PASS, f"{metric_name} {_fmt_usd(metric)}, líquido{oi_str}")


def eval_trend(closes: Optional[list[float]], th: dict[str, float]) -> Check:
    """Check 4 — only short into a daily-TF downtrend (strategy bonus rule).

    METHOD (documented): given the last N daily closes (N = trend_sma_days+1
    when available), compute the simple moving average (SMA) of the window and
    the multi-day % change from the first close to the last close.
        * Confirmed downtrend  → last close < SMA AND multi-day change < 0  → PASS
        * Strong uptrend       → last close > SMA AND change ≥ trend_uptrend_pct → FAIL
        * Mild uptrend / mixed → last close > SMA but change < threshold     → WARN
        * Otherwise (flat-ish below SMA but up over window, etc.)            → WARN
    Insufficient / missing candle data → WARN ``dato no disponible``.
    """
    label = "Daily downtrend"
    if not closes or len(closes) < 2:
        return Check(4, label, WARN, f"{_NA} (candles)")
    last = closes[-1]
    sma = sum(closes) / len(closes)
    first = closes[0]
    if first == 0:
        return Check(4, label, WARN, f"{_NA} (candles)")
    chg = (last / first - 1.0) * 100.0
    up_pct = th["trend_uptrend_pct"]
    below_sma = last < sma
    if below_sma and chg < 0:
        return Check(4, label, PASS, f"bajo SMA{len(closes)} y {chg:,.1f}% en ventana, rolling over")
    if (not below_sma) and chg >= up_pct:
        return Check(4, label, FAIL, f"sobre SMA{len(closes)} y +{chg:,.1f}%, uptrend fuerte")
    if not below_sma:
        return Check(4, label, WARN, f"sobre SMA{len(closes)} (+{chg:,.1f}%), no es downtrend")
    return Check(4, label, WARN, f"mixto: bajo SMA{len(closes)} pero {chg:,.1f}% en ventana")


def aggregate_verdict(checks: list[Check]) -> str:
    """GO / CAUTION / NO-GO from checks 1-4 (check 5 is documentation only)."""
    decisive = [c for c in checks if c.n in (1, 2, 3, 4)]
    if any(c.status == FAIL for c in decisive):
        return NO_GO
    if any(c.status == WARN for c in decisive):
        return CAUTION
    return GO


# ─── Formatting helpers ──────────────────────────────────────────────────────
def _fmt_usd(v: Optional[float]) -> str:
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
    try:
        if v >= 1:
            return f"${v:,.4f}".rstrip("0").rstrip(".")
        return f"${v:.8f}".rstrip("0").rstrip(".")
    except Exception:  # noqa: BLE001
        return "n/a"


def _fmt_pct_signed(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    return f"{'+' if v >= 0 else ''}{v:,.1f}%"


_STATUS_EMOJI = {PASS: "✅", WARN: "⚠️", FAIL: "❌"}
_VERDICT_EMOJI = {GO: "🟢", CAUTION: "⚠️", NO_GO: "🔴"}


def build_doc_line(
    ticker: str,
    baseline_funding: Optional[float],
    current_funding: Optional[float],
    pct_reverted: Optional[float],
    price: Optional[float],
    oi_usd: Optional[float],
    vol_24h: Optional[float],
    ts_utc: Optional[str] = None,
) -> str:
    """Check 5 — the audit one-liner that records the trade context.

    This line IS the documentation artifact; it is both logged and shown.
    """
    ts = ts_utc or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    base = f"{baseline_funding:,.0f}%" if baseline_funding is not None else "n/a"
    cur = f"{current_funding:,.0f}%" if current_funding is not None else "n/a"
    rev = f"{pct_reverted:,.0f}%" if pct_reverted is not None else "n/a"
    return (
        f"{ticker} | base {base} → cur {cur} (rev {rev}) | "
        f"px {_fmt_price(price)} | OI {_fmt_usd(oi_usd)} | "
        f"vol24h {_fmt_usd(vol_24h)} | {ts}"
    )


def format_checks_block(result: ChecksResult) -> str:
    """Render the market line + '5 CHECKS' + verdict block appended to the alert."""
    lines = [
        f"Price: {_fmt_price(result.price)} | 24h: {_fmt_pct_signed(result.chg_24h)} "
        f"| OI: {_fmt_usd(result.oi_usd)} | Vol24h: {_fmt_usd(result.vol_24h)}",
        "",
        "5 CHECKS (Farm the DUMP):",
    ]
    for c in result.checks:
        emoji = _STATUS_EMOJI.get(c.status, "")
        lines.append(f"{c.n}. {c.label}: {c.status} {emoji} ({c.detail})")
    v_emoji = _VERDICT_EMOJI.get(result.verdict, "")
    tally = f"{result.n_warn} warn, {result.n_fail} fail"
    lines.append("")
    lines.append(f"VEREDICTO: {result.verdict} {v_emoji}  ({tally})")
    lines.append("Decisión final tuya, BCD — el bot no abre el trade.")
    return "\n".join(lines)


# ─── Hyperliquid data (network, wrapped) ─────────────────────────────────────
def _hl_coin_aliases(ticker: str) -> list[str]:
    """Candidate Hyperliquid coin names for a Variational ticker.

    HL lists most majors under the bare symbol; some low-priced assets use a
    ``k`` prefix (kPEPE, kBONK …). We try the bare symbol first, then a couple
    of conservative variants. A miss simply yields ``None`` → WARN (never faked).
    """
    t = (ticker or "").strip().upper()
    out = [t]
    if t.startswith("K") and t[1:]:
        out.append(t[1:])          # kPEPE → PEPE
    else:
        out.append("k" + t)        # PEPE → kPEPE (HL stores some as kXXX)
    # de-dupe, preserve order
    seen: set[str] = set()
    uniq: list[str] = []
    for c in out:
        if c and c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


async def _hl_post(payload: dict[str, Any]) -> Any:
    if not _HTTPX_OK:
        raise RuntimeError("httpx unavailable")
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
        resp = await client.post(
            HL_INFO_URL,
            json=payload,
            headers={"User-Agent": _UA, "Accept": "application/json"},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"HL HTTP {resp.status_code}")
    return resp.json()


async def _hl_asset_ctxs() -> dict[str, dict[str, Any]]:
    """Return {COIN: ctx} from metaAndAssetCtxs, cached 60s. {} on failure."""
    async with _ctx_lock:
        if (
            _ctx_cache["by_coin"] is not None
            and (time.time() - _ctx_cache["ts"]) < _CTX_CACHE_TTL_S
        ):
            return _ctx_cache["by_coin"]  # type: ignore[return-value]
        try:
            data = await _hl_post({"type": "metaAndAssetCtxs"})
            meta, ctxs = data[0], data[1]
            universe = meta.get("universe", [])
            by_coin: dict[str, dict[str, Any]] = {}
            for asset, ctx in zip(universe, ctxs):
                name = str(asset.get("name", "")).upper()
                if name and isinstance(ctx, dict):
                    by_coin[name] = ctx
            _ctx_cache["by_coin"] = by_coin
            _ctx_cache["ts"] = time.time()
            return by_coin
        except Exception as exc:  # noqa: BLE001
            log.warning("farmdump: HL metaAndAssetCtxs n/a (%s)", exc)
            return {}


def _f(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


async def fetch_hl_market(ticker: str) -> dict[str, Optional[float]]:
    """24h change, mark, OI(usd), vol(usd) from Hyperliquid for ``ticker``.

    Returns a dict with keys chg_24h / mark / oi_usd / vol_24h, each possibly
    None. Never raises — a miss or outage simply yields all-None.
    """
    out: dict[str, Optional[float]] = {
        "chg_24h": None, "mark": None, "oi_usd": None, "vol_24h": None, "coin": None,
    }
    by_coin = await _hl_asset_ctxs()
    if not by_coin:
        return out
    ctx = None
    coin = None
    for cand in _hl_coin_aliases(ticker):
        if cand in by_coin:
            ctx, coin = by_coin[cand], cand
            break
    if ctx is None:
        return out
    out["coin"] = coin
    mark = _f(ctx.get("markPx"))
    prev = _f(ctx.get("prevDayPx"))
    out["mark"] = mark
    if mark is not None and prev is not None and prev != 0:
        out["chg_24h"] = (mark / prev - 1.0) * 100.0
    out["vol_24h"] = _f(ctx.get("dayNtlVlm"))
    oi_coins = _f(ctx.get("openInterest"))
    if oi_coins is not None and mark is not None:
        out["oi_usd"] = oi_coins * mark
    return out


async def fetch_hl_daily_closes(ticker: str, n_days: int) -> Optional[list[float]]:
    """Last ``n_days`` daily closes (oldest→newest) from HL candleSnapshot.

    Returns None if the asset/candles are unavailable. Never raises.
    """
    n_days = max(2, int(n_days))
    coin = None
    by_coin = await _hl_asset_ctxs()
    for cand in _hl_coin_aliases(ticker):
        if cand in by_coin:
            coin = cand
            break
    if coin is None:
        return None
    now_ms = int(time.time() * 1000)
    # Pad the window so we always cover ≥ n_days full candles.
    start_ms = now_ms - (n_days + 3) * 24 * 3600 * 1000
    try:
        candles = await _hl_post({
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": "1d", "startTime": start_ms, "endTime": now_ms},
        })
        closes = [_f(c.get("c")) for c in candles if isinstance(c, dict)]
        closes = [c for c in closes if c is not None]
        if len(closes) < 2:
            return None
        return closes[-n_days:]
    except Exception as exc:  # noqa: BLE001
        log.warning("farmdump: HL candleSnapshot n/a for %s (%s)", ticker, exc)
        return None


# ─── Orchestration ───────────────────────────────────────────────────────────
async def run_checks(
    ticker: str,
    baseline_funding: Optional[float],
    current_funding: Optional[float],
    *,
    var_price: Optional[float] = None,
    var_vol_24h: Optional[float] = None,
    var_oi_usd: Optional[float] = None,
    pct_reverted: Optional[float] = None,
) -> ChecksResult:
    """Run the 5 checks for ``ticker`` and return a ChecksResult.

    Variational values (already on the fired market) are passed in; Hyperliquid
    fills the 24h-change and daily-trend gaps. Any external failure degrades the
    affected check to WARN — this function NEVER raises.
    """
    th = thresholds()
    ticker = (ticker or "").strip().upper()

    # Hyperliquid enrichment (wrapped end-to-end).
    hl: dict[str, Optional[float]] = {"chg_24h": None, "mark": None, "oi_usd": None, "vol_24h": None}
    closes: Optional[list[float]] = None
    try:
        hl = await fetch_hl_market(ticker)
    except Exception as exc:  # noqa: BLE001 — belt & suspenders
        log.warning("farmdump: HL market enrichment failed for %s (%s)", ticker, exc)
    try:
        closes = await fetch_hl_daily_closes(ticker, int(th["trend_sma_days"]) + 1)
    except Exception as exc:  # noqa: BLE001
        log.warning("farmdump: HL trend enrichment failed for %s (%s)", ticker, exc)

    # Prefer Variational liquidity numbers; fall back to HL when absent.
    vol = var_vol_24h if var_vol_24h is not None else hl.get("vol_24h")
    oi = var_oi_usd if var_oi_usd is not None else hl.get("oi_usd")
    price = var_price if var_price is not None else hl.get("mark")

    checks = [
        eval_funding(current_funding, th),
        eval_price_action(hl.get("chg_24h"), th),
        eval_liquidity(vol, oi, th),
        eval_trend(closes, th),
    ]
    doc_line = build_doc_line(
        ticker, baseline_funding, current_funding, pct_reverted, price, oi, vol,
    )
    checks.append(Check(5, "Documentado", PASS, doc_line))

    verdict = aggregate_verdict(checks)
    result = ChecksResult(
        ticker=ticker,
        checks=checks,
        verdict=verdict,
        doc_line=doc_line,
        price=price,
        chg_24h=hl.get("chg_24h"),
        oi_usd=oi,
        vol_24h=vol,
    )
    # The documentation line is logged (check 5 is a process gate).
    log.info("farmdump checks %s → %s | %s", ticker, verdict, doc_line)
    return result


async def run_checks_safe(
    ticker: str,
    baseline_funding: Optional[float],
    current_funding: Optional[float],
    **kwargs: Any,
) -> Optional[ChecksResult]:
    """run_checks wrapped so a catastrophic failure returns None instead of
    raising — the caller then appends nothing and the bare reversion alert
    still fires. Used on the scheduler path where nothing may crash."""
    try:
        return await run_checks(ticker, baseline_funding, current_funding, **kwargs)
    except Exception:  # noqa: BLE001
        log.exception("farmdump: run_checks_safe swallowed a fatal error for %s", ticker)
        return None
