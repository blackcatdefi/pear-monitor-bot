"""SemiAnalysis Substack — semi/AI infra newsletter (R-PERFECT Sub-4 #5).

Source: https://newsletter.semianalysis.com/feed  (free public RSS)
Free items only — paywalled content not exposed via RSS.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from modules.intel30._intel_base import LIVE, get_text, log_call

log = logging.getLogger(__name__)

SOURCE = "semianalysis_rss"
URL = "https://newsletter.semianalysis.com/feed"

ITEM_RE = re.compile(r"<item>(?P<body>.*?)</item>", re.DOTALL)
TITLE_RE = re.compile(r"<title[^>]*>(?:<!\[CDATA\[)?(?P<t>.*?)(?:\]\]>)?</title>", re.DOTALL)
LINK_RE = re.compile(r"<link[^>]*>(?P<l>https?://[^<]+)</link>")
DATE_RE = re.compile(r"<pubDate>(?P<d>[^<]+)</pubDate>")


async def fetch_all() -> dict[str, Any]:
    text, meta = await get_text(SOURCE, URL, timeout=12.0)
    if not text:
        return {"_global_error": meta.get("reason", "fetch_failed"), "series": []}
    items = ITEM_RE.findall(text)
    series = []
    for body in items[:5]:
        tm = TITLE_RE.search(body)
        lm = LINK_RE.search(body)
        dm = DATE_RE.search(body)
        if not tm:
            continue
        series.append({
            "label": "SemiAnalysis",
            "title": tm.group("t").strip()[:100],
            "link": lm.group("l").strip() if lm else "",
            "date": dm.group("d").strip()[:25] if dm else "",
            "_error": None,
        })
    if not series:
        return {"_global_error": "no items parsed", "series": []}
    log_call(SOURCE, LIVE, meta["latency_ms"], meta["bytes"], 200, f"{len(series)} items")
    return {"series": series, "_global_error": None}


def format_for_telegram(data: dict[str, Any]) -> str:
    lines = ["⚡ *SemiAnalysis — latest*"]
    if data.get("_global_error"):
        lines.append(f"  ⚠️ {data['_global_error']}")
        return "\n".join(lines)
    for s in data.get("series", []):
        if not isinstance(s, dict) or s.get("_error"):
            continue
        t = s.get("title", "?")
        d = (s.get("date") or "")[:16]
        lines.append(f"  • {t}")
        if d:
            lines.append(f"      {d}")
    return "\n".join(lines)
