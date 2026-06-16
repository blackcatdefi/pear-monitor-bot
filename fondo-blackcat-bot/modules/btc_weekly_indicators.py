"""R-LMEC-AUTOCOMPUTE (2026-06-16) — deterministic BTC weekly TA for LMEC.

Computes, from **real BTC weekly OHLC on CLOSED candles only**, the three
indicators that previously had to be entered by hand via ``/setlmec``:

    * Weekly **MACD** (standard 12 / 26 / 9 on weekly closes) → the leg
      consumes a single boolean: is the MACD line in positive territory
      (``macd_line > 0``).  This matches the pre-change leg-2 semantic
      ("MACD weekly > 0 (bull crossover)").
    * Weekly **RSI** (Wilder's 14 on weekly closes) → a float, consumed by
      leg 3 (``rsi > 70`` → VALIDA).
    * **MA50W** (50-week simple moving average of weekly closes) → a USD
      float, consumed by leg 4 together with the live BTC price and the
      auto-managed weeks-broken counter.

The **in-progress week is always excluded** so the values never repaint:
only candles whose close-time is already in the past are used.

These computed values become the DEFAULT source for LMEC legs 2/3/4.
``/setlmec`` remains a **manual OVERRIDE** that takes precedence when set.

NEVER FABRICATE
---------------
If the OHLC fetch fails, or there is insufficient history for a given
indicator, that indicator is returned as ``None`` → the evaluator renders
"n/d" and the trigger stays *unknown* (it neither fires nor false-clears).
We never substitute zero, and the freshness guard in :mod:`modules.lmec_state`
ensures a *stale* prior computed snapshot is also treated as unavailable.

Pure compute functions (``ema_series`` / ``compute_macd`` / ``compute_rsi`` /
``compute_ma50w`` / ``compute_all``) are deterministic and unit-tested against
fixtures.  Network I/O lives only in the async fetch/refresh helpers and is
driven by the scheduler — :func:`evaluate_lmec_triggers` never touches the
network; it only reads the persisted snapshot.

Data source
-----------
Primary: **Binance public klines** (``interval=1w``, ``symbol=BTCUSDT``,
keyless).  Weekly candles open Monday 00:00:00 UTC and close the following
Sunday 23:59:59.999 UTC — this is the weekly-close boundary recorded with the
snapshot and is the boundary that lines up with TradingView's BINANCE:BTCUSDT
weekly chart that BCD used to read by hand.

Fallback: **HyperLiquid** ``candleSnapshot`` (``interval=1w``, coin ``BTC``),
routed through the shared rate-limited ``modules.hl_client``.  Note HL weekly
candles use a Thursday-anchored boundary; the snapshot records which source
and boundary were actually used so the difference is always transparent.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

try:  # stay import-safe even without httpx
    import httpx  # type: ignore

    _HTTPX_OK = True
except Exception:  # noqa: BLE001
    httpx = None  # type: ignore
    _HTTPX_OK = False

# ── Indicator parameters ────────────────────────────────────────────────
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
RSI_PERIOD = 14
MA50W_WINDOW = 50

# Per-indicator minimum history (closed weekly closes) required before we are
# willing to emit a value.  Below the minimum → None (→ "n/d"), never a
# half-warmed number.
MIN_FOR_MACD = MACD_SLOW + MACD_SIGNAL  # 35 — slow EMA + signal warmup
MIN_FOR_RSI = RSI_PERIOD + 1            # 15
MIN_FOR_MA50W = MA50W_WINDOW            # 50

# How many weekly candles to request — comfortably above MA50W warmup.
FETCH_LIMIT = int(os.getenv("LMEC_WEEKLY_FETCH_LIMIT", "160") or "160")

_HTTP_TIMEOUT_S = float(os.getenv("LMEC_WEEKLY_HTTP_TIMEOUT_S", "20") or "20")
_UA = "Mozilla/5.0 (FondoBlackCat LMEC autocompute)"

BINANCE_KLINES_URL = os.getenv(
    "BINANCE_KLINES_URL", "https://api.binance.com/api/v3/klines"
)
HL_INFO_URL = os.getenv("HL_INFO_URL", "https://api.hyperliquid.xyz/info").rstrip("/")


# ── Pure compute (deterministic, network-free, unit-tested) ─────────────
def ema_series(values: list[float], period: int) -> list[float]:
    """Exponential moving average series, seeded with the first value.

    Standard recursive EMA: ``e_t = v_t * k + e_{t-1} * (1 - k)`` with
    ``k = 2 / (period + 1)``.  Returns one EMA point per input point.
    """
    if not values or period <= 0:
        return []
    k = 2.0 / (period + 1.0)
    e = float(values[0])
    out = [e]
    for v in values[1:]:
        e = float(v) * k + e * (1.0 - k)
        out.append(e)
    return out


def compute_macd(
    closes: list[float],
    fast: int = MACD_FAST,
    slow: int = MACD_SLOW,
    signal: int = MACD_SIGNAL,
) -> Optional[dict[str, float | bool]]:
    """Standard MACD on ``closes``. Returns the latest line/signal/histogram
    plus ``positive`` = ``macd_line > 0``.  ``None`` if insufficient history."""
    closes = [float(c) for c in closes]
    if len(closes) < max(slow + signal, MIN_FOR_MACD):
        return None
    ema_fast = ema_series(closes, fast)
    ema_slow = ema_series(closes, slow)
    macd_line = [a - b for a, b in zip(ema_fast, ema_slow)]
    signal_line = ema_series(macd_line, signal)
    line = macd_line[-1]
    sig = signal_line[-1]
    return {
        "macd_line": line,
        "signal_line": sig,
        "histogram": line - sig,
        "positive": bool(line > 0.0),
    }


def compute_rsi(closes: list[float], period: int = RSI_PERIOD) -> Optional[float]:
    """Wilder's RSI(period) of ``closes``. ``None`` if insufficient history."""
    closes = [float(c) for c in closes]
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def compute_ma50w(closes: list[float], window: int = MA50W_WINDOW) -> Optional[float]:
    """Simple moving average of the last ``window`` closes. ``None`` if short."""
    closes = [float(c) for c in closes]
    if len(closes) < window:
        return None
    return sum(closes[-window:]) / float(window)


def compute_all(closes: list[float]) -> dict[str, Any]:
    """Compute the three LMEC indicators from a list of CLOSED weekly closes.

    Each indicator independently degrades to ``None`` when its own minimum
    history is not met — never fabricated.  The returned shapes match exactly
    what the LMEC evaluator consumes:

        macd_weekly_positive : bool | None
        rsi_weekly           : float | None
        ma50w_usd            : float | None
    """
    closes = [float(c) for c in closes if c is not None]
    macd = compute_macd(closes)
    rsi = compute_rsi(closes)
    ma50w = compute_ma50w(closes)
    return {
        "macd_weekly_positive": (None if macd is None else bool(macd["positive"])),
        "macd_detail": macd,
        "rsi_weekly": rsi,
        "ma50w_usd": ma50w,
        "last_close": closes[-1] if closes else None,
        "n_closes": len(closes),
    }


# ── Network fetch (async; CLOSED candles only) ──────────────────────────
def _f(x: Any) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


async def _fetch_binance_weekly(limit: int = FETCH_LIMIT) -> Optional[dict[str, Any]]:
    """Closed weekly closes from Binance klines (1w BTCUSDT). None on miss."""
    if not _HTTPX_OK:
        return None
    now_ms = int(time.time() * 1000)
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S, follow_redirects=True) as client:
            resp = await client.get(
                BINANCE_KLINES_URL,
                params={"symbol": "BTCUSDT", "interval": "1w", "limit": int(limit)},
                headers={"User-Agent": _UA, "Accept": "application/json"},
            )
        if resp.status_code != 200:
            log.warning("lmec.weekly: binance HTTP %s", resp.status_code)
            return None
        rows = resp.json()
        # kline row: [openTime, o, h, l, c, vol, closeTime, ...]
        closed = [r for r in rows if isinstance(r, (list, tuple)) and len(r) >= 7
                  and _f(r[6]) is not None and float(r[6]) <= now_ms]
        closes = [_f(r[4]) for r in closed]
        closes = [c for c in closes if c is not None]
        if not closed:
            return None
        last_close_ms = int(float(closed[-1][6]))
        return {
            "closes": closes,
            "source": "binance:BTCUSDT@1w",
            "weekly_close_ts_utc": datetime.fromtimestamp(
                last_close_ms / 1000.0, tz=timezone.utc
            ).isoformat(),
            "weekly_boundary": "Mon 00:00 UTC open / Sun 23:59:59.999 UTC close",
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("lmec.weekly: binance fetch failed (%s)", exc)
        return None


async def _hl_post(payload: dict[str, Any]) -> Any:
    """Route through the shared rate-limited HL client when pointing at the
    canonical endpoint; otherwise do a direct post (test/env override)."""
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


async def _fetch_hl_weekly(limit: int = FETCH_LIMIT) -> Optional[dict[str, Any]]:
    """Closed weekly closes from HyperLiquid candleSnapshot (1w BTC). None on miss."""
    now_ms = int(time.time() * 1000)
    interval_ms = 7 * 24 * 3600 * 1000
    start_ms = now_ms - (int(limit) + 4) * interval_ms
    try:
        candles = await _hl_post({
            "type": "candleSnapshot",
            "req": {"coin": "BTC", "interval": "1w", "startTime": start_ms, "endTime": now_ms},
        })
        if not isinstance(candles, list):
            return None
        closed = [c for c in candles if isinstance(c, dict)
                  and _f(c.get("T")) is not None and float(c["T"]) <= now_ms]
        closes = [_f(c.get("c")) for c in closed]
        closes = [c for c in closes if c is not None]
        if not closed:
            return None
        last_close_ms = int(float(closed[-1]["T"]))
        return {
            "closes": closes,
            "source": "hyperliquid:BTC@1w",
            "weekly_close_ts_utc": datetime.fromtimestamp(
                last_close_ms / 1000.0, tz=timezone.utc
            ).isoformat(),
            "weekly_boundary": "Thu 00:00 UTC open / Wed 23:59:59.999 UTC close",
        }
    except Exception as exc:  # noqa: BLE001
        log.warning("lmec.weekly: HL fetch failed (%s)", exc)
        return None


async def fetch_weekly_closes(limit: int = FETCH_LIMIT) -> Optional[dict[str, Any]]:
    """Fetch CLOSED weekly closes (Binance primary, HL fallback). None on total miss."""
    data = await _fetch_binance_weekly(limit)
    if data and len(data.get("closes") or []) >= MIN_FOR_RSI:
        return data
    fallback = await _fetch_hl_weekly(limit)
    if fallback and len(fallback.get("closes") or []) >= MIN_FOR_RSI:
        return fallback
    # Return whichever non-empty payload we have (may still be too short for
    # some indicators — compute_all degrades each independently to None).
    return data or fallback


async def refresh_and_persist() -> dict[str, Any]:
    """Fetch real weekly closes, compute the three indicators and persist the
    snapshot to lmec_state. Returns the persisted payload (or an error dict).

    NEVER FABRICATES: on fetch failure nothing is persisted as a value — the
    payload records ``ok=False`` and the previously persisted snapshot (if any)
    is left untouched, where the freshness guard will eventually expire it.
    """
    fetched = await fetch_weekly_closes()
    now_iso = datetime.now(timezone.utc).isoformat()
    if not fetched or not (fetched.get("closes")):
        log.warning("lmec.weekly: refresh got no data — leaving prior snapshot")
        return {"ok": False, "ts_utc": now_iso, "reason": "fetch_failed"}
    inds = compute_all(fetched["closes"])
    payload = {
        "ok": True,
        "computed_ts_utc": now_iso,
        "weekly_close_ts_utc": fetched.get("weekly_close_ts_utc"),
        "source": fetched.get("source"),
        "weekly_boundary": fetched.get("weekly_boundary"),
        "macd_weekly_positive": inds["macd_weekly_positive"],
        "rsi_weekly": inds["rsi_weekly"],
        "ma50w_usd": inds["ma50w_usd"],
        "macd_detail": inds["macd_detail"],
        "last_close": inds["last_close"],
        "n_closes": inds["n_closes"],
    }
    try:
        from modules.lmec_state import set_computed_inputs

        set_computed_inputs(payload)
    except Exception:  # noqa: BLE001
        log.exception("lmec.weekly: persist failed (non-fatal)")
    log.info(
        "lmec.weekly: refreshed macd_pos=%s rsi=%s ma50w=%s n=%s src=%s close=%s",
        payload["macd_weekly_positive"], payload["rsi_weekly"],
        payload["ma50w_usd"], payload["n_closes"], payload["source"],
        payload["weekly_close_ts_utc"],
    )
    return payload


def _smoke() -> int:  # pragma: no cover — manual/production smoke entrypoint
    import asyncio
    import json

    async def _run():
        fetched = await fetch_weekly_closes()
        if not fetched:
            print("FETCH FAILED — no weekly closes available")
            return 1
        inds = compute_all(fetched["closes"])
        out = {
            "source": fetched.get("source"),
            "weekly_close_ts_utc": fetched.get("weekly_close_ts_utc"),
            "weekly_boundary": fetched.get("weekly_boundary"),
            "n_closes": inds["n_closes"],
            "last_close": inds["last_close"],
            "macd_weekly_positive": inds["macd_weekly_positive"],
            "macd_detail": inds["macd_detail"],
            "rsi_weekly": inds["rsi_weekly"],
            "ma50w_usd": inds["ma50w_usd"],
        }
        print(json.dumps(out, indent=2, default=str))
        return 0

    return asyncio.run(_run())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_smoke())
