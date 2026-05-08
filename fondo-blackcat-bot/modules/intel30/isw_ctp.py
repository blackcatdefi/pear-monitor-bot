"""ISW + Critical Threats Project — Geopolitics RSS (R-INTEL30 Phase 1 #8).

Gold standard daily Russia/Ukraine + Iran Update. The reports markets actually
move on (Strait of Hormuz risk → oil → DXY ripples).

Sources:
    https://www.understandingwar.org/backgrounder/feed  (ISW Russian Offensive Campaign)
    https://www.criticalthreats.org/feed                (Iran Update Special Report)

100% free, no paywall. Daily (often 2x).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

log = logging.getLogger(__name__)

FEEDS = {
    "ISW (Russia/Ukraine)": "https://www.understandingwar.org/backgrounder/feed",
    "CTP (Iran Update)":    "https://www.criticalthreats.org/feed",
}
HTTP_TIMEOUT = 10.0
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"


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


async def fetch_feed(label: str, url: str) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers={"User-Agent": UA}) as client:
            r = await client.get(url)
            r.raise_for_status()
        items = _parse_rss(r.text, max_items=3)
        return {"label": label, "items": items, "_error": None}
    except Exception as e:
        log.warning("isw_ctp %s fail: %s", label, e)
        return {"label": label, "items": [], "_error": str(e)}


async def fetch_all() -> dict[str, Any]:
    tasks = [fetch_feed(lbl, url) for lbl, url in FEEDS.items()]
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
