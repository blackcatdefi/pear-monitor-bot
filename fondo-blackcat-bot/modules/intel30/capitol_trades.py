"""Capitol Trades — US Congress trade disclosures (R-PERFECT Sub-4 #2).

Source: https://www.capitoltrades.com/trades  (Next.js SPA)
BFF: https://bff.capitoltrades.com/trades  (often blocked from server-side IPs by CF)

Strategy: try BFF (JSON) first; on 503 fallback to HTML page-data extraction
from the SSR'd <script id="__NEXT_DATA__"> blob.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from modules.intel30._intel_base import DEGRADED, LIVE, get_json, get_text, log_call, set_source_state

log = logging.getLogger(__name__)

SOURCE = "capitol_trades"
BFF_URL = "https://bff.capitoltrades.com/trades?page=1&pageSize=10&sortBy=-pubDate"
HTML_URL = "https://www.capitoltrades.com/trades"

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(?P<json>.*?)</script>',
    re.DOTALL,
)


async def _fetch_bff() -> tuple[bool, dict[str, Any]]:
    data, meta = await get_json(SOURCE, BFF_URL, timeout=10.0, retries=0)
    if data and isinstance(data, dict):
        rows = data.get("data") or []
        if isinstance(rows, list) and rows:
            log_call(SOURCE, LIVE, meta["latency_ms"], meta["bytes"], 200, "bff")
            return True, _normalise_rows(rows)
    return False, {}


async def _fetch_html() -> tuple[bool, dict[str, Any]]:
    text, meta = await get_text(SOURCE, HTML_URL, timeout=12.0)
    if not text:
        return False, {}
    m = NEXT_DATA_RE.search(text)
    if not m:
        # SPA without SSR — fallback to plain link
        log_call(SOURCE, DEGRADED, meta["latency_ms"], meta["bytes"], 200, "spa_no_ssr")
        set_source_state(SOURCE, DEGRADED)
        return True, {
            "_global_error": "spa_no_ssr",
            "_link": HTML_URL,
            "_status": DEGRADED,
            "series": [],
        }
    try:
        blob = json.loads(m.group("json"))
        # Walk the blob looking for trades list
        rows = _extract_trades(blob)
        log_call(SOURCE, LIVE, meta["latency_ms"], meta["bytes"], 200, f"html {len(rows)} rows")
        return True, _normalise_rows(rows)
    except Exception as e:  # noqa: BLE001
        log.debug("capitol_trades parse fail: %s", e)
        return True, {"_global_error": "parse_failed", "_link": HTML_URL, "series": []}


def _extract_trades(blob: dict) -> list[dict]:
    """Walk the nested __NEXT_DATA__ JSON and pluck a 'trades' or 'data' array."""
    queue = [blob]
    while queue:
        node = queue.pop()
        if isinstance(node, dict):
            for key in ("trades", "items", "data"):
                v = node.get(key)
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    if any(k in v[0] for k in ("politician", "issuer", "asset", "ticker")):
                        return v[:10]
            for v in node.values():
                if isinstance(v, (dict, list)):
                    queue.append(v)
        elif isinstance(node, list):
            for v in node:
                if isinstance(v, (dict, list)):
                    queue.append(v)
    return []


def _normalise_rows(rows: list[dict]) -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    for r in rows[:10]:
        if not isinstance(r, dict):
            continue
        pol = r.get("politician") or {}
        if isinstance(pol, dict):
            name = pol.get("fullName") or pol.get("name") or "?"
        else:
            name = str(pol)
        asset = r.get("asset") or r.get("issuer") or {}
        if isinstance(asset, dict):
            ticker = asset.get("ticker") or asset.get("name") or "?"
        else:
            ticker = str(asset)
        side = (r.get("type") or r.get("transactionType") or "?").lower()[:8]
        date = (r.get("pubDate") or r.get("transactionDate") or "")[:10]
        out.append({"label": ticker, "politician": name, "side": side, "date": date, "_error": None})
    return {"series": out, "_global_error": None}


async def fetch_all() -> dict[str, Any]:
    ok, payload = await _fetch_bff()
    if ok:
        return payload
    ok2, payload2 = await _fetch_html()
    if ok2:
        return payload2
    return {"_global_error": "all probes failed", "_link": HTML_URL, "series": []}


def format_for_telegram(data: dict[str, Any]) -> str:
    lines = ["🏛 *CapitolTrades — Congress disclosures*"]
    if data.get("_global_error"):
        lines.append(f"  ⚠️ {data['_global_error']}")
        if data.get("_link"):
            lines.append(f"  → {data['_link']}")
        return "\n".join(lines)
    rows = data.get("series", [])
    if not rows:
        lines.append("  ⚠️ no rows")
        return "\n".join(lines)
    for r in rows[:10]:
        if not isinstance(r, dict) or r.get("_error"):
            continue
        tk = r.get("label", "?")
        pol = r.get("politician", "?")[:20]
        side = r.get("side", "?")
        date = r.get("date", "")
        lines.append(f"  • {tk} {side} ({date}) — {pol}")
    return "\n".join(lines)
