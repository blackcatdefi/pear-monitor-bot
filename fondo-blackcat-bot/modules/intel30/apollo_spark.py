"""Apollo Academy — Daily Spark by Torsten Slok (R-INTEL30 Phase 1 #11).

Daily 1-page chart from Apollo's Chief Economist. The single most-quoted
institutional macro chart on the buy-side (private credit, K-shape, AI capex
narratives).

Source RSS feed (Apollo Academy publishes a Substack-style feed):
    https://www.apolloacademy.com/feed/
    https://www.apolloacademy.com/the-daily-spark/feed/

Module returns the latest post title + link + summary excerpt.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

log = logging.getLogger(__name__)

FEEDS = [
    "https://www.apolloacademy.com/the-daily-spark/feed/",
    "https://www.apolloacademy.com/feed/",
]
HTTP_TIMEOUT = 10.0
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"


def _parse_rss(xml: str, max_items: int = 2) -> list[dict[str, str]]:
    items = []
    for m in re.finditer(r"<item>(.*?)</item>", xml, re.DOTALL):
        block = m.group(1)
        title_m = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", block, re.DOTALL)
        link_m = re.search(r"<link>(.*?)</link>", block, re.DOTALL)
        date_m = re.search(r"<pubDate>(.*?)</pubDate>", block, re.DOTALL)
        desc_m = re.search(r"<description>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>", block, re.DOTALL)
        title = (title_m.group(1) if title_m else "").strip()
        link = (link_m.group(1) if link_m else "").strip()
        date = (date_m.group(1) if date_m else "").strip()
        desc = (desc_m.group(1) if desc_m else "").strip()
        # strip HTML tags from description
        desc = re.sub(r"<[^>]+>", " ", desc)
        desc = re.sub(r"\s+", " ", desc).strip()
        if title:
            items.append({"title": title, "link": link, "date": date, "desc": desc[:200]})
        if len(items) >= max_items:
            break
    return items


async def fetch_latest() -> dict[str, Any]:
    last_err = None
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers={"User-Agent": UA}) as client:
        for url in FEEDS:
            try:
                r = await client.get(url)
                if r.status_code == 200:
                    items = _parse_rss(r.text, max_items=2)
                    if items:
                        return {"items": items, "source": url, "_error": None}
                last_err = f"http_{r.status_code}@{url}"
            except Exception as e:
                last_err = str(e)
                continue
    return {"items": [], "source": None, "_error": last_err or "no_feeds"}


async def fetch_all() -> dict[str, Any]:
    return await fetch_latest()


def format_for_telegram(data: dict[str, Any]) -> str:
    lines = ["📰 *Apollo — Daily Spark (Torsten Slok)*"]
    if data.get("_error"):
        lines.append(f"  ⚠️ {data['_error'][:80]}")
        return "\n".join(lines)
    items = data.get("items") or []
    if not items:
        lines.append("  • sin items recientes")
        return "\n".join(lines)
    for it in items[:2]:
        t = it.get("title", "?")[:120]
        d = it.get("date", "")[:16]
        desc = it.get("desc", "")[:160]
        lines.append(f"  📊 *{t}* ({d})")
        if desc:
            lines.append(f"    {desc}")
    return "\n".join(lines)
