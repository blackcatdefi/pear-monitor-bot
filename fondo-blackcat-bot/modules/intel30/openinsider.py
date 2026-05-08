"""OpenInsider Form 4 screener (R-PERFECT Sub-4 #1).

Source: http://openinsider.com/screener?... (HTML, no key)
Scrapes the top-of-table latest insider buys (CEO/CFO/Director).

Free, no auth. Throttle ~1 req/min recommended.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from modules.intel30._intel_base import LIVE, get_text, log_call

log = logging.getLogger(__name__)

SOURCE = "openinsider"
URL = (
    "http://openinsider.com/screener?"
    "s=&o=&pl=&ph=&ll=&lh=&fd=730&fdr=&td=0&tdr=&fdlyl=&fdlyh=&daysago=&"
    "xp=1&xs=1&vl=&vh=&ocl=&och=&sic1=-1&sicl=100&sich=9999&grp=0&nfl=&nfh=&"
    "nil=&nih=&nol=&noh=&v2l=&v2h=&oc2l=&oc2h=&sortcol=0&cnt=10&page=1"
)
# 2026-05-08 schema fix: rows now use `<tr style="background:#...">` (no class)
# and inner cells wrap content in <div>/<a>. Anchor on SEC.gov filing link.
TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL)
FILING_RE = re.compile(r">(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})<")
DATE_RE = re.compile(r">(\d{4}-\d{2}-\d{2})<")
TICKER_RE = re.compile(r'<a href="/([A-Z][A-Z0-9.\-]*?)"')
INSIDER_RE = re.compile(r'/insider/[^>]+>([^<]+)</a>')


async def fetch_all() -> dict[str, Any]:
    text, meta = await get_text(SOURCE, URL, timeout=12.0)
    if not text:
        return {"_global_error": meta.get("reason", "fetch_failed"), "series": []}
    # Slice from <tbody> to skip header table noise
    idx = text.find("<tbody>")
    body = text[idx:idx + 30000] if idx >= 0 else text
    rows = []
    for tr in TR_RE.finditer(body):
        block = tr.group(1)
        if "sec.gov/Archives" not in block and "SEC Form 4" not in block:
            continue  # not a data row
        filing = FILING_RE.search(block)
        dates = DATE_RE.findall(block)
        ticker = TICKER_RE.search(block)
        insider = INSIDER_RE.search(block)
        if not (ticker and (filing or dates)):
            continue
        # trade date is the second YYYY-MM-DD (first is the one inside filing timestamp)
        trade_date = dates[1] if len(dates) >= 2 else (dates[0] if dates else "")
        rows.append({
            "ticker": ticker.group(1).strip(),
            "company": ticker.group(1).strip(),
            "insider": (insider.group(1).strip() if insider else "?"),
            "trade_date": trade_date[:10],
            "_error": None,
        })
        if len(rows) >= 10:
            break
    log_call(SOURCE, LIVE, meta["latency_ms"], meta["bytes"], 200, f"{len(rows)} rows")
    return {"series": rows, "_global_error": None}


def format_for_telegram(data: dict[str, Any]) -> str:
    lines = ["🕵 *OpenInsider — Form 4 latest*"]
    if data.get("_global_error"):
        lines.append(f"  ⚠️ {data['_global_error']}")
        return "\n".join(lines)
    rows = data.get("series", [])
    if not rows:
        lines.append("  ⚠️ no rows parsed (table layout may have changed)")
        return "\n".join(lines)
    for r in rows[:10]:
        if not isinstance(r, dict) or r.get("_error"):
            continue
        ticker = r.get("ticker", "?")
        insider = r.get("insider", "?")[:22]
        date = r.get("trade_date", "")
        lines.append(f"  • {ticker} ({date}) {insider}")
    return "\n".join(lines)
