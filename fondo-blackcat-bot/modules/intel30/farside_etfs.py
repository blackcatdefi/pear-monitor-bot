"""Farside Investors ETF Flows (R-INTEL30 Phase 1 #5).

De facto industry-standard daily BTC/ETH/SOL spot ETF flows. No formal API —
HTML scrape of the public table. Cited by Bloomberg/CoinDesk/Reuters.

URLs:
    https://farside.co.uk/btc/   — BTC spot ETFs
    https://farside.co.uk/eth/   — ETH spot ETFs
    https://farside.co.uk/sol/   — SOL spot ETFs (newer)

100% free, no login. Daily refresh ~9-11pm ET.

Implementation note: We avoid pandas dependency on Railway by using simple regex
parsing of the table summary row (last row = total flow for the latest day).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

log = logging.getLogger(__name__)

URLS = {
    "BTC": "https://farside.co.uk/btc/",
    "ETH": "https://farside.co.uk/eth/",
    "SOL": "https://farside.co.uk/sol/",
}
HTTP_TIMEOUT = 10.0

# Browser UA to bypass CF1010
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _parse_latest_row(html: str) -> dict[str, Any]:
    """Extract latest data row from Farside table.

    Farside HTML structure has <tr><td>DATE</td><td>...</td><td>TOTAL</td></tr>.
    We grab all rows, pick the last one with a date in DD MMM YYYY format,
    and return the date + total flow column (always last numeric column).
    """
    # rows
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.DOTALL | re.IGNORECASE)
    latest_date = None
    latest_total = None
    for row in reversed(rows):  # bottom-up = newest first in Farside layout
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, flags=re.DOTALL | re.IGNORECASE)
        if len(cells) < 2:
            continue
        # strip tags from cells
        clean = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        # Date format: "07 May 2026"
        if not re.match(r"^\d{1,2}\s+\w+\s+\d{4}$", clean[0]):
            continue
        # last numeric col = total
        for c in reversed(clean[1:]):
            # parse number with - or () for negatives, may include commas
            cleaned = c.replace(",", "").replace("(", "-").replace(")", "").strip()
            try:
                v = float(cleaned)
                latest_date = clean[0]
                latest_total = v
                break
            except (TypeError, ValueError):
                continue
        if latest_date:
            break
    return {"date": latest_date, "total_flow_musd": latest_total}


async def fetch_etf(asset: str) -> dict[str, Any]:
    url = URLS.get(asset.upper())
    if not url:
        return {"asset": asset, "_error": "unknown_asset"}
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers={"User-Agent": UA}) as client:
            r = await client.get(url)
            r.raise_for_status()
            html = r.text
        parsed = _parse_latest_row(html)
        if parsed.get("date") is None:
            return {"asset": asset, "_error": "parse_failed"}
        return {
            "asset": asset.upper(),
            "date": parsed["date"],
            "flow_musd": parsed["total_flow_musd"],
            "_error": None,
        }
    except Exception as e:
        log.warning("farside %s fail: %s", asset, e)
        return {"asset": asset, "_error": str(e)}


async def fetch_all() -> dict[str, Any]:
    tasks = [fetch_etf(a) for a in URLS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out: list[dict[str, Any]] = []
    for r in results:
        if isinstance(r, Exception):
            out.append({"_error": str(r)})
        else:
            out.append(r)
    return {"flows": out}


def format_for_telegram(data: dict[str, Any]) -> str:
    flows = data.get("flows") or []
    lines = ["💰 *Farside — Spot ETF Flows*"]
    if not flows:
        return "\n".join(lines + ["  ⚠️ sin datos"])
    rendered = 0
    for f in flows:
        if not isinstance(f, dict) or f.get("_error"):
            continue
        asset = f.get("asset", "?")
        d = f.get("date", "?")
        flow = f.get("flow_musd")
        if isinstance(flow, (int, float)):
            arrow = "📈" if flow > 0 else ("📉" if flow < 0 else "➖")
            lines.append(f"  {arrow} {asset}: ${flow:+,.1f}M ({d})")
            rendered += 1
    if rendered == 0:
        errs = [f.get("_error", "?")[:40] for f in flows if isinstance(f, dict) and f.get("_error")]
        lines.append(f"  ⚠️ scrape err: {errs[0] if errs else '?'}")
    return "\n".join(lines)
