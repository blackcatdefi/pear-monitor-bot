"""TraderMap.io BTC integration — R-BOT-FEEDS-EXPAND Task 1 (2026-05-07).

Source URL: https://tradermap.io/chart/BTC

TraderMap renders all technical indicators client-side via a JavaScript
chart library; there is no public REST endpoint that exposes RSI / MACD /
MA values as JSON. To deliver value without spinning up a headless
browser on Railway (cost / cold-start sensitive), this module operates in
**dual mode**:

1. **Live HTML scrape** of ``tradermap.io/chart/BTC`` to extract whatever
   numeric fields are present in the rendered HTML (current BTC price is
   served as plain text in the page header). Best-effort, never raises.
2. **Env-var override** for indicators that aren't surfaced in the static
   HTML. ``TRADERMAP_BTC_RSI``, ``TRADERMAP_BTC_MACD``,
   ``TRADERMAP_BTC_MA50W``, ``TRADERMAP_BTC_MA200W``,
   ``TRADERMAP_BTC_SUPPORT``, ``TRADERMAP_BTC_RESISTANCE``,
   ``TRADERMAP_BTC_TREND`` (string label).

The same env vars feed ``modules.lmec_triggers.evaluate_lmec_triggers()``
when TraderMap data is fresh, so updating values in Railway propagates
to the Bug #5 LMEC bear-invalidation block automatically.

Public API
----------
* ``fetch_tradermap_btc()`` — async, returns ``{status, data, source}``.
* ``format_tradermap_block(data)`` — Telegram-ready string for /reporte
  section 2 MERCADO.
* ``tradermap_indicator_overrides()`` — dict of env-var-derived indicator
  values for the LMEC evaluator to consume.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx

log = logging.getLogger(__name__)

TRADERMAP_URL = os.getenv("TRADERMAP_BTC_URL", "https://tradermap.io/chart/BTC")
TRADERMAP_TIMEOUT_S = float(os.getenv("TRADERMAP_TIMEOUT_S", "15"))
TRADERMAP_ENABLED = os.getenv("TRADERMAP_ENABLED", "true").strip().lower() not in (
    "false", "0", "no", "off",
)

# User-Agent — many sites 403 default httpx UA. Mimic a modern browser.
_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15"
)


def _coerce_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "").replace("$", "")
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _extract_btc_price_from_html(html: str) -> float | None:
    """Best-effort BTC price extraction.

    The page renders prices like ``BTC $80,155`` or ``$80155.42`` in
    several places; we accept the first 4–8 digit USD figure that looks
    like a BTC price (>$1,000 — sanity guard so we don't grab gas fees).
    """
    if not html:
        return None
    # Pattern 1: explicit "$xx,xxx" or "$xx,xxx.xx"
    candidates = re.findall(r"\$([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?)", html)
    for c in candidates:
        v = _coerce_float(c)
        if v and 1000 <= v <= 1_000_000:
            return v
    # Pattern 2: bare numbers >= 4 digits adjacent to BTC label
    for m in re.finditer(r"BTC[^0-9]{0,30}([0-9]{4,7}(?:\.[0-9]+)?)", html, re.IGNORECASE):
        v = _coerce_float(m.group(1))
        if v and 1000 <= v <= 1_000_000:
            return v
    return None


def tradermap_indicator_overrides() -> dict[str, Any]:
    """Read the TRADERMAP_BTC_* env vars and return a normalized dict.

    Keys (all optional):
        rsi_weekly: float
        macd_weekly_positive: bool
        ma50w: float
        ma200w: float
        support: float
        resistance: float
        trend: str (e.g. "bullish", "bearish", "neutral")

    Used by both the /reporte renderer and the LMEC evaluator. Missing
    values are simply omitted from the output dict (don't override).
    """
    out: dict[str, Any] = {}
    rsi = _coerce_float(os.getenv("TRADERMAP_BTC_RSI"))
    if rsi is not None:
        out["rsi_weekly"] = rsi
    macd_raw = os.getenv("TRADERMAP_BTC_MACD")
    if macd_raw is not None and macd_raw.strip():
        s = macd_raw.strip().lower()
        if s in ("positive", "pos", "true", "1", "yes", "bullish"):
            out["macd_weekly_positive"] = True
        elif s in ("negative", "neg", "false", "0", "no", "bearish"):
            out["macd_weekly_positive"] = False
        else:
            f = _coerce_float(macd_raw)
            if f is not None:
                out["macd_weekly_positive"] = f > 0
    ma50w = _coerce_float(os.getenv("TRADERMAP_BTC_MA50W"))
    if ma50w is not None:
        out["ma50w"] = ma50w
    ma200w = _coerce_float(os.getenv("TRADERMAP_BTC_MA200W"))
    if ma200w is not None:
        out["ma200w"] = ma200w
    support = _coerce_float(os.getenv("TRADERMAP_BTC_SUPPORT"))
    if support is not None:
        out["support"] = support
    resistance = _coerce_float(os.getenv("TRADERMAP_BTC_RESISTANCE"))
    if resistance is not None:
        out["resistance"] = resistance
    trend = (os.getenv("TRADERMAP_BTC_TREND") or "").strip()
    if trend:
        out["trend"] = trend
    return out


async def fetch_tradermap_btc() -> dict[str, Any]:
    """Fetch the TraderMap BTC chart page and merge with env-var indicators.

    Returns a status dict mirroring the rest of the bot's intel modules:

        {
          "status": "ok" | "error",
          "source": "tradermap.io/chart/BTC",
          "data": {
            "price_usd": float | None,
            "rsi_weekly": float | None,
            "macd_weekly_positive": bool | None,
            "ma50w": float | None,
            "ma200w": float | None,
            "support": float | None,
            "resistance": float | None,
            "trend": str | None,
            "scrape_ok": bool,
            "indicator_source": "env" | "scrape" | "mixed" | "none"
          },
          "error": str | None
        }
    """
    if not TRADERMAP_ENABLED:
        return {
            "status": "ok",
            "source": "tradermap.io/chart/BTC",
            "data": {
                "price_usd": None,
                "scrape_ok": False,
                "indicator_source": "none",
                **tradermap_indicator_overrides(),
            },
            "note": "TRADERMAP_ENABLED=false",
        }

    overrides = tradermap_indicator_overrides()
    price_usd: float | None = None
    scrape_ok = False
    err: str | None = None

    try:
        async with httpx.AsyncClient(
            timeout=TRADERMAP_TIMEOUT_S,
            follow_redirects=True,
            headers={"User-Agent": _DEFAULT_UA, "Accept": "text/html"},
        ) as c:
            resp = await c.get(TRADERMAP_URL)
            if resp.status_code == 200:
                price_usd = _extract_btc_price_from_html(resp.text)
                scrape_ok = price_usd is not None
            else:
                err = f"http_{resp.status_code}"
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}:{str(e)[:120]}"
        log.warning("[tradermap] fetch failed: %s", err)

    indicator_source = "none"
    if overrides and scrape_ok:
        indicator_source = "mixed"
    elif overrides:
        indicator_source = "env"
    elif scrape_ok:
        indicator_source = "scrape"

    data = {
        "price_usd": price_usd,
        "scrape_ok": scrape_ok,
        "indicator_source": indicator_source,
        **overrides,
    }

    return {
        "status": "ok" if (scrape_ok or overrides) else "error",
        "source": "tradermap.io/chart/BTC",
        "data": data,
        "error": err if not (scrape_ok or overrides) else None,
    }


def _fmt_usd(v: Any) -> str:
    f = _coerce_float(v) if not isinstance(v, (int, float)) else float(v)
    if f is None:
        return "—"
    return f"${f:,.0f}" if f >= 100 else f"${f:.2f}"


def format_tradermap_block(payload: dict[str, Any] | None) -> str:
    """Render a Telegram block for /reporte section 2 MERCADO.

    Designed to slot in next to the current price feeds. Always returns
    a non-empty string — degraded note is shown when no data is available.
    """
    if not payload or not isinstance(payload, dict):
        return "📊 TraderMap BTC: (no data)"
    if payload.get("status") != "ok":
        err = payload.get("error") or "unknown"
        return f"📊 TraderMap BTC: ❌ {err}"
    d = payload.get("data") or {}
    src = d.get("indicator_source") or "none"

    lines: list[str] = []
    lines.append(f"📊 TraderMap BTC ({src} src)")
    if d.get("price_usd") is not None:
        lines.append(f"  Price: {_fmt_usd(d['price_usd'])}")
    if d.get("rsi_weekly") is not None:
        lines.append(f"  RSI weekly: {float(d['rsi_weekly']):.1f}")
    if d.get("macd_weekly_positive") is not None:
        macd = "POSITIVE" if d["macd_weekly_positive"] else "NEGATIVE"
        lines.append(f"  MACD weekly: {macd}")
    if d.get("ma50w") is not None:
        lines.append(f"  MA50w: {_fmt_usd(d['ma50w'])}")
    if d.get("ma200w") is not None:
        lines.append(f"  MA200w: {_fmt_usd(d['ma200w'])}")
    if d.get("support") is not None or d.get("resistance") is not None:
        sup = _fmt_usd(d.get("support")) if d.get("support") is not None else "—"
        res = _fmt_usd(d.get("resistance")) if d.get("resistance") is not None else "—"
        lines.append(f"  S/R: {sup} / {res}")
    if d.get("trend"):
        lines.append(f"  Trend: {d['trend']}")
    if len(lines) == 1:
        lines.append("  (set TRADERMAP_BTC_RSI / _MACD / _MA50W / _TREND env vars in Railway)")
    return "\n".join(lines)
