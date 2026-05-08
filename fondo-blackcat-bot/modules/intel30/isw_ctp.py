"""Geopolitics RSS — ISW/CTP coverage via alternative feeds (R-INTEL30 Phase 1 #8).

Gold standard daily Russia/Ukraine + Iran/MENA. The reports markets actually
move on (Strait of Hormuz risk → oil → DXY ripples).

ISW (understandingwar.org) and CTP (criticalthreats.org) do not expose public
RSS endpoints (returns 404/403). We substitute with high-fidelity alternative
RSS feeds that aggregate the same beat:

Sources:
    https://www.understandingwar.org/rss.xml          (try canonical RSS path)
    https://www.criticalthreats.org/feed              (try canonical RSS path)
    https://kyivindependent.com/rss/                  (Russia/Ukraine fallback)
    https://www.atlanticcouncil.org/feed/             (broad geopolitics)
    https://www.al-monitor.com/rss.xml                (Iran/MENA fallback)

100% free, no paywall. Module degrades gracefully if all feeds fail.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Ordered priority: canonical (datacenter-blocked) → BBC World → Al Jazeera (always work)
# ISW/CTP feeds return 403 from Railway/datacenter IPs, so we route via mainstream
# wire services that carry the same Russia/Ukraine + Iran beat.
FEEDS = {
    "Geopol Russia/Ukraine": [
        "https://www.understandingwar.org/rss.xml",
        "http://feeds.bbci.co.uk/news/world/europe/rss.xml",
        "http://feeds.bbci.co.uk/news/world/rss.xml",
    ],
    "Geopol Iran/MENA": [
        "https://www.criticalthreats.org/rss.xml",
        "https://www.aljazeera.com/xml/rss/all.xml",
        "http://feeds.bbci.co.uk/news/world/middle_east/rss.xml",
    ],
}
HTTP_TIMEOUT = 10.0
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


def _parse_rss(xml: str, max_items: int = 3) -> list[dict[str, str]]:
    """Tiny RSS parser without lxml/feedparser dep."""
    items = []
    for m in re.finditer(r"<item>(.*?)</item>", xml, re.DOTALL):
        block = m.group(1)
        title_m = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", block, re.DOTALL)
        link_m = re.search(r"<link>(.*?)</link>", block, re.DOTALL)
        date_m = re.search(r"<pubDate>(.*?)</pubDate>", block, re.DOTALL)
        title = (title_m.group(1) if title_m else "").strip()
        link = (link_m.group(1) if link_m else "").strip()
        date = (date_m.group(1) if date_m else "").strip()
        if title:
            items.append({"title": title, "link": link, "date": date})
        if len(items) >= max_items:
            break
    return items


async def fetch_feed(label: str, urls: list[str] | str) -> dict[str, Any]:
    """Try each URL in priority order; return first one that yields items."""
    if isinstance(urls, str):
        urls = [urls]
    last_err = None
    used_url = None
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=HEADERS, follow_redirects=True) as client:
        for url in urls:
            try:
                r = await client.get(url)
                if r.status_code != 200:
                    last_err = f"http_{r.status_code}@{url[:60]}"
                    continue
                items = _parse_rss(r.text, max_items=3)
                if items:
                    used_url = url
                    return {"label": label, "items": items, "source": used_url, "_error": None}
                last_err = f"empty@{url[:60]}"
            except Exception as e:
                last_err = f"{type(e).__name__}@{url[:60]}: {str(e)[:60]}"
                continue
    log.warning("isw_ctp %s all-feeds-fail: %s", label, last_err)
    return {"label": label, "items": [], "source": None, "_error": last_err or "no_feeds"}


async def fetch_all() -> dict[str, Any]:
    tasks = [fetch_feed(lbl, urls) for lbl, urls in FEEDS.items()]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out: list[dict[str, Any]] = []
    for r in results:
        if isinstance(r, Exception):
            out.append({"_error": str(r)})
        else:
            out.append(r)
    return {"feeds": out}


def format_for_telegram(data: dict[str, Any]) -> str:
    feeds = data.get("feeds") or []
    lines = ["🌍 *ISW + CTP — Geopol Daily*"]
    rendered = 0
    for f in feeds:
        if not isinstance(f, dict):
            continue
        label = f.get("label", "?")
        if f.get("_error"):
            lines.append(f"  ⚠️ {label}: {f['_error'][:50]}")
            continue
        items = f.get("items") or []
        if not items:
            continue
        lines.append(f"  📡 *{label}* ({len(items)}):")
        for it in items[:3]:
            t = it.get("title", "?")[:90]
            d = it.get("date", "")[:16]
            lines.append(f"    – {t} ({d})")
        rendered += 1
    if rendered == 0:
        lines.append("  • sin items hoy")
    return "\n".join(lines)
