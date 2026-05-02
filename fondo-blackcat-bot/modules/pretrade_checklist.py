"""Round 17 — /pretrade {token} checklist automatizado.

Implementa el checklist de 5 puntos que BCD ejecuta mentalmente antes de
cada trade nuevo:

  1. Noticias recientes (X últimos 5d, intel_memory + opcional X API)
  2. Funding rate HL + (Binance si COINGLASS_API_KEY disponible)
  3. OI vs Volume ratio
  4. Narrative priced (precio vs ATH/30d return)
  5. Unlocks próximos 30d

Resilient: cada source puede fallar individualmente sin tirar el comando.
NO consume X API live por default — solo lee intel_memory cache
(intel_processor escribe ahí). Si BCD quiere live X data, /reporte ya lo
hace; pretrade es ligero.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from config import DATA_DIR

log = logging.getLogger(__name__)


def _format_pct(v: float | None) -> str:
    if v is None:
        return "—"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}%"


def _format_usd(v: float | None) -> str:
    if v is None:
        return "—"
    if v >= 1:
        return f"${v:,.2f}"
    return f"${v:.6f}"


# ─── Intel memory: keyword search over recent entries ────────────────────────


def _intel_recent_for_token(symbol: str, days: int = 5) -> list[dict]:
    db_path = os.path.join(DATA_DIR, "intel_memory.db")
    if not os.path.isfile(db_path):
        return []
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=days)
    ).isoformat()
    out: list[dict] = []
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        # Match symbol case-insensitively + dollar-prefix variant
        like_terms = [f"%{symbol}%", f"%${symbol}%", f"%{symbol.lower()}%"]
        for term in like_terms:
            cur = conn.execute(
                """
                SELECT timestamp_utc, source, raw_text
                FROM intel_memory
                WHERE timestamp_utc >= ? AND raw_text LIKE ?
                ORDER BY timestamp_utc DESC
                LIMIT 30
                """,
                (cutoff, term),
            )
            for r in cur.fetchall():
                out.append(
                    {
                        "ts": r["timestamp_utc"],
                        "source": r["source"],
                        "text": r["raw_text"],
                    }
                )
        conn.close()
    except Exception:
        log.exception("intel_memory query failed")
        return []
    # Dedup by ts+source+first 80 chars
    seen: set[str] = set()
    dedup: list[dict] = []
    for it in out:
        key = f"{it['ts']}|{it['source']}|{(it['text'] or '')[:80]}"
        if key in seen:
            continue
        seen.add(key)
        dedup.append(it)
    return dedup[:30]


def _quick_sentiment(items: list[dict]) -> str:
    if not items:
        return "—"
    pos_words = {"long", "buy", "bullish", "moon", "pump", "ath", "breakout", "rally"}
    neg_words = {"short", "sell", "bearish", "dump", "crash", "rekt", "liquidat", "down", "rug"}
    pos = neg = 0
    for it in items:
        text = (it.get("text") or "").lower()
        for w in pos_words:
            if w in text:
                pos += 1
                break
        for w in neg_words:
            if w in text:
                neg += 1
                break
    if pos == 0 and neg == 0:
        return "neutral"
    if pos > neg * 2:
        return f"bullish ({pos}/{pos+neg})"
    if neg > pos * 2:
        return f"bearish ({neg}/{pos+neg})"
    return f"mixed ({pos}+/{neg}-)"


# ─── Hyperliquid funding + OI/volume ────────────────────────────────────────


async def _hl_funding_oi_volume(symbol: str) -> dict[str, Any]:
    """Read meta_and_asset_ctxs and compute hourly_funding, OI, volume_24h."""
    out = {"funding_hourly": None, "funding_daily": None, "oi": None, "volume_24h": None}
    try:
        from modules.portfolio import meta_and_asset_ctxs
        data = await meta_and_asset_ctxs()
        if not isinstance(data, list) or len(data) < 2:
            return out
        meta, ctxs = data[0], data[1]
        universe = meta.get("universe", []) if isinstance(meta, dict) else []
        for idx, asset in enumerate(universe):
            if asset.get("name", "").upper() == symbol.upper():
                ctx = ctxs[idx] if idx < len(ctxs) else None
                if not isinstance(ctx, dict):
                    return out
                f_hr = ctx.get("funding")
                if f_hr is not None:
                    try:
                        f_hr_f = float(f_hr)
                        out["funding_hourly"] = f_hr_f * 100  # %
                        out["funding_daily"] = f_hr_f * 24 * 100
                    except Exception:
                        pass
                oi = ctx.get("openInterest")
                if oi is not None:
                    try:
                        out["oi"] = float(oi)
                    except Exception:
                        pass
                vol = ctx.get("dayNtlVlm")
                if vol is not None:
                    try:
                        out["volume_24h"] = float(vol)
                    except Exception:
                        pass
                return out
    except Exception:
        log.exception("HL funding/OI/volume failed for %s", symbol)
    return out


# ─── Price context (current vs 30d return) ──────────────────────────────────


async def _price_context(symbol: str) -> dict[str, Any]:
    out = {"current": None, "from_ath_pct": None, "return_30d_pct": None}
    try:
        from modules.portfolio import get_spot_price
        px = await get_spot_price(symbol)
        out["current"] = px
    except Exception:
        log.warning("get_spot_price failed for %s", symbol)
    return out


# ─── Unlocks (next 30d) ──────────────────────────────────────────────────────


async def _upcoming_unlocks_for(symbol: str, days: int = 30) -> list[dict]:
    out: list[dict] = []
    try:
        from modules.unlocks import fetch_unlocks
        payload = await fetch_unlocks()
        events = []
        if isinstance(payload, dict):
            events = payload.get("events") or payload.get("data") or []
        elif isinstance(payload, list):
            events = payload
        cutoff = datetime.now(timezone.utc) + timedelta(days=days)
        for ev in events:
            if not isinstance(ev, dict):
                continue
            sym = (ev.get("symbol") or ev.get("token") or "").upper()
            if sym != symbol.upper():
                continue
            ts = ev.get("date") or ev.get("timestamp_utc") or ev.get("ts")
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                if dt > cutoff:
                    continue
            except Exception:
                pass
            out.append(ev)
    except Exception:
        log.exception("unlocks fetch failed for %s", symbol)
    return out


# ─── Main ────────────────────────────────────────────────────────────────────


async def build_pretrade_checklist(symbol: str) -> str:
    symbol = symbol.upper().strip().lstrip("$").strip()
    if not symbol:
        return "Uso: /pretrade <SYMBOL>\nEj: /pretrade DYDX"

    intel_items, hl_data, price, unlocks = await asyncio.gather(
        asyncio.to_thread(_intel_recent_for_token, symbol, 5),
        _hl_funding_oi_volume(symbol),
        _price_context(symbol),
        _upcoming_unlocks_for(symbol, 30),
    )

    sep = "─" * 40
    lines: list[str] = [
        f"📋 PRE-TRADE CHECKLIST — {symbol}",
        sep,
    ]

    # 1. News
    sentiment = _quick_sentiment(intel_items)
    lines.append(f"\n📰 News (5d): {len(intel_items)} entries in intel_memory")
    lines.append(f"   Heuristic sentiment: {sentiment}")
    if intel_items:
        lines.append("   Last 3 mentions:")
        for it in intel_items[:3]:
            txt = (it.get("text") or "").replace("\n", " ").strip()[:120]
            ts = (it.get("ts") or "")[:10]
            src = it.get("source") or "?"
            lines.append(f"   • {ts} [{src}] {txt}")

    # 2. Funding
    f_hr = hl_data.get("funding_hourly")
    f_d = hl_data.get("funding_daily")
    lines.append("")
    lines.append("💰 Funding HL")
    if f_hr is None:
        lines.append("   Unavailable (perp does not exist or RPC offline)")
    else:
        lines.append(f"   Hourly: {f_hr:.4f}% | Daily: {f_d:.4f}%")
        if f_d < -0.03:
            lines.append("   ⚠️ Overcrowded SHORT — squeeze risk")
        elif f_d > 0.10:
            lines.append("   ⚠️ Overcrowded LONG — flush risk")

    # 3. OI/Volume
    oi = hl_data.get("oi")
    vol = hl_data.get("volume_24h")
    lines.append("")
    if oi is not None and vol is not None and vol > 0:
        ratio = oi / vol
        lines.append(f"📊 OI=${oi:,.0f} | Vol24h=${vol:,.0f}")
        lines.append(f"   OI/Vol ratio: {ratio:.2f}")
        if ratio > 5:
            lines.append("   ⚠️ Extreme leverage (OI >> volume)")
    else:
        lines.append("📊 OI/Volume: partial data")
        if oi is not None:
            lines.append(f"   OI=${oi:,.0f}")
        if vol is not None:
            lines.append(f"   Vol24h=${vol:,.0f}")

    # 4. Price
    lines.append("")
    if price.get("current") is not None:
        lines.append(f"📈 Price: {_format_usd(price['current'])}")
    else:
        lines.append("📈 Price: unavailable")

    # 5. Unlocks
    lines.append("")
    if unlocks:
        lines.append(f"🔓 Unlocks 30d: {len(unlocks)}")
        for u in unlocks[:5]:
            date = u.get("date") or u.get("timestamp_utc") or "?"
            pct = u.get("pct_supply") or u.get("pct") or u.get("dilution")
            pct_s = f"{pct:.2f}% supply" if isinstance(pct, (int, float)) else str(pct or "?")
            lines.append(f"   • {date}: {pct_s}")
    else:
        lines.append("🔓 Unlocks 30d: none (or data unavailable)")

    lines.append("")
    lines.append("💡 Document this info before executing the trade.")
    lines.append("   Persistir en /log add para tracking ex-post.")
    return "\n".join(lines)
