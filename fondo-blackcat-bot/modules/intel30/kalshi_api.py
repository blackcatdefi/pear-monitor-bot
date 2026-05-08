"""Kalshi prediction markets — public read (R-PERFECT Phase 3 #2).

Public market read (no auth needed) at api.elections.kalshi.com/trade-api/v2/markets.
Authenticated requests use RSA-PSS signature — gated behind KALSHI_PRIVATE_KEY +
KALSHI_KEY_ID env vars. We DON'T need them for public market reads, so this
module is LIVE without keys.

If KALSHI_PRIVATE_KEY is set, an authenticated probe is added to verify the
signed-request path is working (used to surface portfolio context in /reporte).
"""
from __future__ import annotations

import base64
import logging
import os
import time
from typing import Any

from modules.intel30._intel_base import LIVE, get_json, log_call

log = logging.getLogger(__name__)

PUBLIC_BASE = "https://api.elections.kalshi.com/trade-api/v2"
SOURCE = "kalshi_api"

KALSHI_KEY_ID = os.getenv("KALSHI_KEY_ID", "").strip()
KALSHI_PRIVATE_KEY = os.getenv("KALSHI_PRIVATE_KEY", "").strip()


async def fetch_public_markets() -> dict[str, Any]:
    data, meta = await get_json(
        SOURCE, f"{PUBLIC_BASE}/markets",
        params={"limit": 5, "status": "open"},
        timeout=10.0,
    )
    if not data or not isinstance(data, dict):
        return {"_error": meta.get("reason", "fetch_failed")}
    markets = data.get("markets") or []
    out = []
    for m in markets[:5]:
        if not isinstance(m, dict):
            continue
        out.append({
            "label": m.get("ticker") or m.get("event_ticker") or "?",
            "title": (m.get("title") or m.get("subtitle") or "")[:60],
            "yes_ask_cents": m.get("yes_ask"),
            "no_ask_cents": m.get("no_ask"),
            "_error": None,
        })
    return {"markets": out, "_error": None}


def _sign(method: str, path: str, ts_ms: int) -> tuple[bool, str]:
    """RSA-PSS sign. Returns (ok, signature_b64). Best-effort, no crash on missing crypto."""
    if not KALSHI_PRIVATE_KEY:
        return False, ""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
    except ImportError:
        log.debug("cryptography lib not present — Kalshi auth disabled")
        return False, ""
    try:
        priv = serialization.load_pem_private_key(
            KALSHI_PRIVATE_KEY.encode("utf-8"),
            password=None,
        )
        if not isinstance(priv, RSAPrivateKey):
            return False, ""
        msg = f"{ts_ms}{method}{path}".encode("utf-8")
        sig = priv.sign(
            msg,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return True, base64.b64encode(sig).decode("ascii")
    except Exception as e:  # noqa: BLE001
        log.debug("Kalshi sign fail: %s", e)
        return False, ""


async def fetch_authenticated_balance() -> dict[str, Any]:
    """Authenticated probe — only run if both KEY_ID and PRIVATE_KEY set."""
    if not (KALSHI_KEY_ID and KALSHI_PRIVATE_KEY):
        return {"_error": "KALSHI_KEY_ID + KALSHI_PRIVATE_KEY not set", "_skipped": True}
    ts_ms = int(time.time() * 1000)
    method = "GET"
    path = "/trade-api/v2/portfolio/balance"
    ok, sig = _sign(method, path, ts_ms)
    if not ok:
        return {"_error": "sign_failed_or_no_crypto_lib"}
    headers = {
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": str(ts_ms),
    }
    data, meta = await get_json(
        SOURCE, f"{PUBLIC_BASE}/portfolio/balance",
        headers=headers, timeout=10.0, retries=0,
    )
    if not data:
        return {"_error": meta.get("reason", "auth_fetch_failed")}
    return {"balance": data, "_error": None}


async def fetch_all() -> dict[str, Any]:
    public = await fetch_public_markets()
    auth = await fetch_authenticated_balance()
    series = []
    if public.get("_error"):
        series.append({"label": "public_markets", "_error": public["_error"]})
    else:
        markets = public.get("markets", [])
        for m in markets:
            series.append(m)
    if not auth.get("_skipped"):
        if auth.get("_error"):
            series.append({"label": "auth_balance", "_error": auth["_error"]})
        else:
            series.append({"label": "auth_balance", "_payload": auth.get("balance"), "_error": None})
    log_call(SOURCE, LIVE, 0, 0, 200, f"{len(series)} rows")
    return {"series": series, "_global_error": None if series else "no_data"}


def format_for_telegram(data: dict[str, Any]) -> str:
    lines = ["🎯 *Kalshi — public markets*"]
    if data.get("_global_error"):
        lines.append(f"  ⚠️ {data['_global_error']}")
        return "\n".join(lines)
    for s in data.get("series", []):
        if not isinstance(s, dict):
            continue
        if s.get("_error"):
            lines.append(f"  • {s.get('label', '?')}: ⚠️ {s['_error']}")
            continue
        lab = s.get("label", "?")
        title = s.get("title", "")
        ya = s.get("yes_ask_cents")
        na = s.get("no_ask_cents")
        if ya is not None or na is not None:
            lines.append(f"  • {lab}  yes:{ya}¢/no:{na}¢")
            if title:
                lines.append(f"      {title}")
        else:
            lines.append(f"  • {lab}: ✓")
    return "\n".join(lines)
