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
ROW_RE = re.compile(
    r"<tr[^>]*>\s*<td[^>]*>(?P<x>[^<]*)</td>\s*"  # X column
    r"<td[^>]*>(?P<filing>[^<]+)</td>\s*"
    r"<td[^>]*>(?P<trade>[^<]+)</td>\s*"
    r"<td[^>]*><a[^>]*>(?P<ticker>[^<]+)</a></td>\s*"
    r"<td[^>]*>(?P<company>[^<]+)</td>\s*"
    r"<td[^>]*><a[^>]*>(?P<insider>[^<]+)</a></td>",
    re.DOTALL,
)


async def fetch_all() -> dict[str, Any]:
    text, meta = await get_text(SOURCE, URL, timeout=12.0)
    if not text:
        return {"_global_error": meta.get("reason", "fetch_failed"), "series": []}
    rows = []
    for m in ROW_RE.finditer(text):
        rows.append({
            "ticker": m.group("ticker").strip(),
            "company": m.group("company").strip(),
            "insider": m.group("insider").strip(),
            "trade_date": m.group("trade").strip()[:10],
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
