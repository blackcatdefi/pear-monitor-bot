"""Round 18 — Cryexc intel module.

Source: https://cryexc.josedonato.com/app — a Svelte+WebAssembly browser-based
crypto orderflow terminal (orderbook, footprint, liquidations, HL whale tracking,
funding rates, etc). The terminal renders to a <canvas> via Emscripten WASM, with
no server-side API on the cryexc host (`/api/*` returns 404, `robots.txt`
disallows `/api/`). Data is pulled directly from exchange WebSockets in the
user's browser.

Implication: we cannot scrape numerical data from the page itself. Instead we
mirror the most actionable subset of cryexc's data using the SAME public
exchange APIs cryexc consumes:

  - Binance Futures liquidations (last 24h via /futures/data + ticker stats)
  - Funding rates across Binance / Bybit / OKX / Hyperliquid (arb opportunities)
  - Hyperliquid open interest (HL native API)

Cryexc itself is cited as the inspiration/reference. The page is also probed for
availability so /cryexc reports its uptime status.

Design notes:
  - Cache snapshots in SQLite (DATA_DIR/cryexc.db) with 5min TTL
  - Cooldown for live fetches (CRYEXC_RATE_LIMIT_MINUTES, default 30) is only
    applied to scheduler-triggered fetches; on-demand /cryexc respects 5min cache
  - Notable events are de-duplicated via cryexc_seen_events (event hash, alert once)
  - Master kill-switch: CRYEXC_ENABLED. Scheduler kill-switch: CRYEXC_MONITOR_ENABLED.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from config import DATA_DIR

log = logging.getLogger(__name__)

CRYEXC_URL = "https://cryexc.josedonato.com/app"
CRYEXC_HOST = "https://cryexc.josedonato.com"
DB_PATH = os.path.join(DATA_DIR, "cryexc.db")

# Defaults; overridable via env vars
DEFAULT_CACHE_TTL_S = int(os.getenv("CRYEXC_CACHE_TTL_SECONDS", "300"))
DEFAULT_RATE_LIMIT_MIN = float(os.getenv("CRYEXC_RATE_LIMIT_MINUTES", "30"))

# Heuristic thresholds (env-overridable). Conservative defaults so we don't
# spam the chat — first iteration; can be tuned post-deploy.
LIQ_NOTABLE_USD = float(os.getenv("CRYEXC_LIQ_NOTABLE_USD", "10000000"))   # $10M+ aggregate liq move
FUNDING_NOTABLE_BPS = float(os.getenv("CRYEXC_FUNDING_NOTABLE_BPS", "30"))  # >=30 bp / 8h
HL_OI_DELTA_PCT = float(os.getenv("CRYEXC_HL_OI_DELTA_PCT", "15"))         # >=15% OI move

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HTTP_TIMEOUT_S = 12.0


# ─── Snapshot dataclass ────────────────────────────────────────────────────────


@dataclass
class CryexcSnapshot:
    timestamp_utc: str
    source_url: str
    cryexc_status: str                    # "up" | "down" | "degraded"
    raw_data: dict[str, Any] = field(default_factory=dict)
    summary_text: str = ""
    notable_events: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─── SQLite helpers ────────────────────────────────────────────────────────────


def _conn() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cryexc_snapshots (
            id INTEGER PRIMARY KEY,
            timestamp_utc TEXT NOT NULL UNIQUE,
            raw_data_json TEXT NOT NULL,
            summary_text TEXT NOT NULL,
            notable_events_json TEXT NOT NULL,
            cryexc_status TEXT NOT NULL DEFAULT 'unknown'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cryexc_seen_events (
            id INTEGER PRIMARY KEY,
            event_hash TEXT UNIQUE NOT NULL,
            first_seen_utc TEXT NOT NULL,
            last_seen_utc TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cryexc_ts ON cryexc_snapshots(timestamp_utc DESC)"
    )
    conn.commit()
    return conn


def _persist_snapshot(snap: CryexcSnapshot) -> None:
    try:
        with _conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO cryexc_snapshots
                (timestamp_utc, raw_data_json, summary_text, notable_events_json, cryexc_status)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    snap.timestamp_utc,
                    json.dumps(snap.raw_data, ensure_ascii=False, default=str),
                    snap.summary_text,
                    json.dumps(snap.notable_events, ensure_ascii=False),
                    snap.cryexc_status,
                ),
            )
    except Exception:  # noqa: BLE001
        log.exception("cryexc snapshot persist failed")


def _load_latest_snapshot() -> CryexcSnapshot | None:
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT timestamp_utc, raw_data_json, summary_text, notable_events_json, cryexc_status "
                "FROM cryexc_snapshots ORDER BY timestamp_utc DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return CryexcSnapshot(
            timestamp_utc=row["timestamp_utc"],
            source_url=CRYEXC_URL,
            cryexc_status=row["cryexc_status"] or "unknown",
            raw_data=json.loads(row["raw_data_json"]) if row["raw_data_json"] else {},
            summary_text=row["summary_text"] or "",
            notable_events=json.loads(row["notable_events_json"]) if row["notable_events_json"] else [],
        )
    except Exception:  # noqa: BLE001
        log.exception("cryexc snapshot load failed")
        return None


def _hash_event(event: str) -> str:
    # Strip timestamps (within line) so the same logical event hashes the same
    # for ~24h. We hash the leading "key" portion of the event (first 80 chars).
    norm = event.strip()[:80].lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:32]


def filter_new_events(events: list[str]) -> list[str]:
    """Return only events not seen in the last 24h."""
    if not events:
        return []
    out: list[str] = []
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    try:
        with _conn() as conn:
            for ev in events:
                h = _hash_event(ev)
                row = conn.execute(
                    "SELECT last_seen_utc FROM cryexc_seen_events WHERE event_hash = ?",
                    (h,),
                ).fetchone()
                if row and row["last_seen_utc"] > cutoff:
                    continue
                out.append(ev)
    except Exception:  # noqa: BLE001
        log.exception("filter_new_events failed; passing events through")
        return events
    return out


def mark_event_seen(event: str) -> None:
    h = _hash_event(event)
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        with _conn() as conn:
            conn.execute(
                """
                INSERT INTO cryexc_seen_events (event_hash, first_seen_utc, last_seen_utc)
                VALUES (?, ?, ?)
                ON CONFLICT(event_hash) DO UPDATE SET last_seen_utc = excluded.last_seen_utc
                """,
                (h, now_iso, now_iso),
            )
    except Exception:  # noqa: BLE001
        log.exception("mark_event_seen failed")


# ─── Source fetchers (public exchange APIs) ────────────────────────────────────


async def _probe_cryexc(client: httpx.AsyncClient) -> tuple[str, dict[str, Any]]:
    """Hit the cryexc page itself to confirm availability + capture metadata."""
    try:
        r = await client.get(CRYEXC_URL, headers={"User-Agent": USER_AGENT})
        if r.status_code == 200 and "cryexc" in r.text.lower():
            return "up", {"http_status": 200, "size_bytes": len(r.content)}
        return "degraded", {"http_status": r.status_code}
    except Exception as exc:  # noqa: BLE001
        log.warning("cryexc page probe failed: %s", exc)
        return "down", {"error": str(exc)[:200]}


async def _fetch_binance_funding(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Top 10 funding rates by absolute magnitude across Binance USDT-M perps."""
    try:
        r = await client.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex",
            headers={"User-Agent": USER_AGENT},
        )
        if r.status_code != 200:
            return []
        rows = r.json() or []
        # Normalize + filter to USDT-margined main pairs
        normalized = []
        for row in rows:
            sym = row.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            try:
                fr = float(row.get("lastFundingRate", 0))
            except Exception:  # noqa: BLE001
                continue
            normalized.append({"symbol": sym, "funding_rate": fr})
        normalized.sort(key=lambda x: abs(x["funding_rate"]), reverse=True)
        return normalized[:10]
    except Exception as exc:  # noqa: BLE001
        log.warning("binance funding fetch failed: %s", exc)
        return []


async def _fetch_binance_movers(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Top 5 24h movers (by abs % change) from Binance Futures — proxy for liq cascades."""
    try:
        r = await client.get(
            "https://fapi.binance.com/fapi/v1/ticker/24hr",
            headers={"User-Agent": USER_AGENT},
        )
        if r.status_code != 200:
            return []
        rows = r.json() or []
        out = []
        for row in rows:
            sym = row.get("symbol", "")
            if not sym.endswith("USDT"):
                continue
            try:
                pct = float(row.get("priceChangePercent", 0))
                quote_vol = float(row.get("quoteVolume", 0))
            except Exception:  # noqa: BLE001
                continue
            # Exclude micro-vol noise
            if quote_vol < 50_000_000:
                continue
            out.append({
                "symbol": sym,
                "change_pct_24h": pct,
                "quote_volume_24h_usd": quote_vol,
                "last_price": float(row.get("lastPrice", 0)),
            })
        out.sort(key=lambda x: abs(x["change_pct_24h"]), reverse=True)
        return out[:5]
    except Exception as exc:  # noqa: BLE001
        log.warning("binance movers fetch failed: %s", exc)
        return []


async def _fetch_hl_meta(client: httpx.AsyncClient) -> dict[str, Any]:
    """Hyperliquid metaAndAssetCtxs — open interest + funding for HL perps."""
    try:
        r = await client.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "metaAndAssetCtxs"},
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
            timeout=HTTP_TIMEOUT_S,
        )
        if r.status_code != 200:
            return {}
        data = r.json()
        if not isinstance(data, list) or len(data) < 2:
            return {}
        meta, ctxs = data[0], data[1]
        universe = meta.get("universe", []) if isinstance(meta, dict) else []
        out: list[dict[str, Any]] = []
        for asset, ctx in zip(universe, ctxs):
            if not isinstance(ctx, dict) or not isinstance(asset, dict):
                continue
            try:
                oi = float(ctx.get("openInterest", 0))
                px = float(ctx.get("markPx", 0))
                funding = float(ctx.get("funding", 0))
            except Exception:  # noqa: BLE001
                continue
            out.append({
                "symbol": asset.get("name", "?"),
                "open_interest": oi,
                "mark_px": px,
                "funding_8h": funding,
                "oi_usd": oi * px,
            })
        out.sort(key=lambda x: x["oi_usd"], reverse=True)
        return {"top_oi": out[:8]}
    except Exception as exc:  # noqa: BLE001
        log.warning("hyperliquid meta fetch failed: %s", exc)
        return {}


# ─── Snapshot builder ──────────────────────────────────────────────────────────


def _format_funding_pct(fr: float) -> str:
    # fr is per-funding-period; Binance is 8h. Convert to bps for readability.
    return f"{fr * 10000:+.2f} bp/8h"


def _build_summary_text(raw: dict[str, Any]) -> str:
    parts: list[str] = []
    cryexc_status = raw.get("cryexc_status", "?")
    page = raw.get("page_meta", {})
    parts.append(f"Cryexc page status: {cryexc_status} (HTTP {page.get('http_status', '?')})")

    funding = raw.get("binance_funding", []) or []
    if funding:
        top = funding[:5]
        parts.append("Binance funding (top |abs|):")
        for it in top:
            parts.append(f"  • {it['symbol']}: {_format_funding_pct(it['funding_rate'])}")

    movers = raw.get("binance_movers", []) or []
    if movers:
        parts.append("Binance Futures top movers 24h:")
        for it in movers:
            vol_m = it["quote_volume_24h_usd"] / 1e6
            parts.append(
                f"  • {it['symbol']}: {it['change_pct_24h']:+.2f}% | "
                f"px ${it['last_price']:,.4f} | vol ${vol_m:,.0f}M"
            )

    hl = raw.get("hl_meta", {}) or {}
    if hl.get("top_oi"):
        parts.append("Hyperliquid top OI (USD):")
        for it in hl["top_oi"][:5]:
            oi_m = it["oi_usd"] / 1e6
            parts.append(
                f"  • {it['symbol']}: OI ${oi_m:,.0f}M | "
                f"mark ${it['mark_px']:,.4f} | funding {it['funding_8h'] * 10000:+.2f} bp/8h"
            )

    return "\n".join(parts) if parts else "(no data)"


def detect_notable_events(raw: dict[str, Any]) -> list[str]:
    """Heuristics over the raw blob. First-iteration thresholds, env-overridable."""
    out: list[str] = []

    # Funding extremes: |fr| >= FUNDING_NOTABLE_BPS / 10000
    threshold_fr = FUNDING_NOTABLE_BPS / 10000.0
    for it in raw.get("binance_funding", []) or []:
        if abs(it["funding_rate"]) >= threshold_fr:
            out.append(
                f"FUNDING extremo {it['symbol']}: {_format_funding_pct(it['funding_rate'])} "
                f"(>= {FUNDING_NOTABLE_BPS:.0f} bp/8h)"
            )

    # 24h movers: |change| >= 8% AND quote_vol >= $200M (proxy for liquidation cascade)
    for it in raw.get("binance_movers", []) or []:
        if abs(it["change_pct_24h"]) >= 8 and it["quote_volume_24h_usd"] >= 200_000_000:
            sign = "PUMP" if it["change_pct_24h"] > 0 else "DUMP"
            vol_m = it["quote_volume_24h_usd"] / 1e6
            out.append(
                f"{sign} {it['symbol']}: {it['change_pct_24h']:+.2f}% / 24h on ${vol_m:,.0f}M vol"
            )

    # HL high OI funding: |fr| >= threshold AND OI > $50M
    hl = raw.get("hl_meta", {}) or {}
    for it in hl.get("top_oi", []) or []:
        if it["oi_usd"] >= 50_000_000 and abs(it["funding_8h"]) >= threshold_fr:
            oi_m = it["oi_usd"] / 1e6
            out.append(
                f"HL FUNDING {it['symbol']}: {it['funding_8h'] * 10000:+.2f} bp/8h "
                f"on ${oi_m:,.0f}M OI"
            )

    # Cryexc page outage
    if raw.get("cryexc_status") in {"down", "degraded"}:
        page = raw.get("page_meta", {})
        out.append(
            f"CRYEXC PAGE {raw.get('cryexc_status').upper()}: "
            f"http_status={page.get('http_status', '?')} err={page.get('error', '—')[:100]}"
        )

    # De-dupe within a single run while preserving order
    seen: set[str] = set()
    deduped: list[str] = []
    for ev in out:
        if ev not in seen:
            seen.add(ev)
            deduped.append(ev)
    return deduped


def parse_to_snapshot(raw_data: dict[str, Any]) -> CryexcSnapshot:
    summary_text = _build_summary_text(raw_data)
    notable = detect_notable_events(raw_data)
    return CryexcSnapshot(
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        source_url=CRYEXC_URL,
        cryexc_status=raw_data.get("cryexc_status", "unknown"),
        raw_data=raw_data,
        summary_text=summary_text,
        notable_events=notable,
    )


# ─── Public API ────────────────────────────────────────────────────────────────


def is_enabled() -> bool:
    return os.getenv("CRYEXC_ENABLED", "true").strip().lower() != "false"


def is_monitor_enabled() -> bool:
    return os.getenv("CRYEXC_MONITOR_ENABLED", "true").strip().lower() != "false"


def _cache_age_seconds(snap: CryexcSnapshot) -> float:
    try:
        ts = datetime.fromisoformat(snap.timestamp_utc.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:  # noqa: BLE001
        return float("inf")


async def fetch_cryexc(force_live: bool = False) -> CryexcSnapshot:
    """Read cryexc + linked exchange data. Cache 5min in SQLite."""
    if not is_enabled():
        return CryexcSnapshot(
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            source_url=CRYEXC_URL,
            cryexc_status="disabled",
            summary_text="CRYEXC_ENABLED=false (kill switch).",
            notable_events=[],
        )

    if not force_live:
        cached = _load_latest_snapshot()
        if cached and _cache_age_seconds(cached) < DEFAULT_CACHE_TTL_S:
            log.info("cryexc cache hit (age=%.0fs)", _cache_age_seconds(cached))
            return cached

    raw: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, follow_redirects=True) as client:
        # Run all fetches concurrently
        cryexc_status, page_meta = "unknown", {}
        funding: list[dict[str, Any]] = []
        movers: list[dict[str, Any]] = []
        hl_meta: dict[str, Any] = {}

        try:
            results = await asyncio.gather(
                _probe_cryexc(client),
                _fetch_binance_funding(client),
                _fetch_binance_movers(client),
                _fetch_hl_meta(client),
                return_exceptions=True,
            )
            if isinstance(results[0], tuple):
                cryexc_status, page_meta = results[0]
            if isinstance(results[1], list):
                funding = results[1]
            if isinstance(results[2], list):
                movers = results[2]
            if isinstance(results[3], dict):
                hl_meta = results[3]
        except Exception:  # noqa: BLE001
            log.exception("cryexc concurrent fetch failed")

    raw = {
        "cryexc_status": cryexc_status,
        "page_meta": page_meta,
        "binance_funding": funding,
        "binance_movers": movers,
        "hl_meta": hl_meta,
    }
    snap = parse_to_snapshot(raw)
    _persist_snapshot(snap)
    return snap


def format_for_telegram(snap: CryexcSnapshot) -> str:
    """Telegram-friendly formatter for /cryexc."""
    age_s = _cache_age_seconds(snap)
    age_label = f"{age_s / 60:.1f}min" if age_s < 3600 else f"{age_s / 3600:.1f}h"
    status_icon = {"up": "🟢", "degraded": "🟡", "down": "🔴", "disabled": "⚪", "unknown": "⚪"}.get(
        snap.cryexc_status, "⚪"
    )

    lines = [
        f"📊 CRYEXC SNAPSHOT — {snap.timestamp_utc[:16]} UTC",
        "─" * 30,
        f"{status_icon} cryexc.josedonato.com: {snap.cryexc_status}",
        "",
        snap.summary_text or "(sin datos)",
    ]

    if snap.notable_events:
        lines.append("")
        lines.append("🔔 Notable events:")
        for ev in snap.notable_events[:8]:
            lines.append(f"  • {ev}")
    else:
        lines.append("")
        lines.append("🔔 Notable events: (ninguno)")

    lines.append("")
    lines.append(f"🔗 Source: cryexc.josedonato.com/app (cache age: {age_label})")
    lines.append(
        "ℹ️ Datos numéricos via APIs públicas mismas que cryexc usa (Binance Futures, "
        "Hyperliquid). La página cryexc renderiza canvas WebAssembly y no expone API."
    )
    return "\n".join(lines)


def cryexc_status_summary() -> str:
    """One-liner status for /version or other small surfaces."""
    enabled = is_enabled()
    monitor = is_monitor_enabled()
    snap = _load_latest_snapshot()
    if not snap:
        return f"Cryexc: enabled={enabled} monitor={monitor} (no snapshots yet)"
    age = _cache_age_seconds(snap)
    return (
        f"Cryexc: enabled={enabled} monitor={monitor} "
        f"last_snap={snap.cryexc_status} age={age / 60:.0f}min "
        f"events_pending={len(snap.notable_events)}"
    )
