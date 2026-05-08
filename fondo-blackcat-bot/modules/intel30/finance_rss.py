"""Finance newsletter aggregator (R-PERFECT Sub-4 #4 + #5).

Combines 4 free public RSS feeds into one module:
  • SemiAnalysis (Substack)
  • Money Stuff by Matt Levine (Bloomberg public RSS)
  • Net Interest by Marc Rubinstein (Substack)
  • The Diff by Byrne Hobart (Substack)

Outputs latest 3 items per feed, deduped by URL.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from modules.intel30._intel_base import LIVE, get_text, log_call

log = logging.getLogger(__name__)

SOURCE = "finance_rss"

FEEDS = [
    ("SemiAnalysis", "https://newsletter.semianalysis.com/feed"),
    ("Money Stuff", "https://www.bloomberg.com/account/newsletters/money-stuff/feed"),
    ("Net Interest", "https://www.netinterest.co/feed"),
    ("The Diff", "https://diff.substack.com/feed"),
]

ITEM_RE = re.compile(r"<item>(?P<body>.*?)</item>", re.DOTALL)
TITLE_RE = re.compile(r"<title[^>]*>(?:<!\[CDATA\[)?(?P<t>.*?)(?:\]\]>)?</title>", re.DOTALL)
LINK_RE = re.compile(r"<link[^>]*>(?P<l>https?://[^<]+)</link>")
DATE_RE = re.compile(r"<pubDate>(?P<d>[^<]+)</pubDate>")


async def fetch_one(name: str, url: str) -> list[dict]:
    text, meta = await get_text(SOURCE, url, timeout=12.0)
    if not text:
        return [{"label": name, "_error": meta.get("reason", "fetch_failed")}]
    items = ITEM_RE.findall(text)
    rows = []
    for body in items[:3]:
        tm = TITLE_RE.search(body)
        lm = LINK_RE.search(body)
        dm = DATE_RE.search(body)
        if not tm:
            continue
        rows.append({
            "label": name,
            "title": tm.group("t").strip()[:80],
            "link": lm.group("l").strip() if lm else "",
            "date": dm.group("d").strip()[:25] if dm else "",
            "_error": None,
        })
    if not rows:
        rows = [{"label": name, "_error": "no_items"}]
    return rows


async def fetch_all() -> dict[str, Any]:
    series: list[dict] = []
    live_count = 0
    for name, url in FEEDS:
        rows = await fetch_one(name, url)
        series.extend(rows)
        if rows and not rows[0].get("_error"):
            live_count += 1
    if live_count == 0:
        return {"_global_error": "all feeds failed", "series": series}
    log_call(SOURCE, LIVE, 0, 0, 200, f"{live_count}/{len(FEEDS)} feeds")
    return {"series": series, "_global_error": None}


def format_for_telegram(data: dict[str, Any]) -> str:
    lines = ["📰 *Finance RSS — newsletters*"]
    if data.get("_global_error"):
        lines.append(f"  ⚠️ {data['_global_error']}")
        return "\n".join(lines)
    grouped: dict[str, list[dict]] = {}
    for s in data.get("series", []):
        grouped.setdefault(s.get("label", "?"), []).append(s)
    for name, rows in grouped.items():
        ok = [r for r in rows if not r.get("_error")]
        if not ok:
            continue
        lines.append(f"  *{name}*")
        for r in ok[:3]:
            t = r.get("title", "?")
            lines.append(f"    • {t}")
    if len(lines) == 1:
        lines.append("  ⚠️ all feeds empty")
    return "\n".join(lines)
